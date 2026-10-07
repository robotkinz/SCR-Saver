from __future__ import annotations

import logging
import time
from PyQt6.QtCore import QObject, QSocketNotifier, QTimer, pyqtSignal

log = logging.getLogger(__name__)

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:  # pragma: no cover - environment-specific
    InputDevice = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]
    list_devices = None  # type: ignore[assignment]


def evdev_available() -> bool:
    return InputDevice is not None


class IdleMonitor(QObject):
    """Track keyboard/mouse idle time via evdev (needs membership in the input group)."""

    idle = pyqtSignal()
    active = pyqtSignal()
    escape = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timeout_seconds = 300
        self._last_activity = time.monotonic()
        self._idle_emitted = False
        self._ignore_until = 0.0
        self._devices: dict[str, InputDevice] = {}
        self._notifiers: dict[str, QSocketNotifier] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._check_idle)
        self._rescan = QTimer(self)
        self._rescan.setInterval(8000)
        self._rescan.timeout.connect(self._scan_devices)
        self._exit_code = 1
        self._paused = False

    def set_exit_key(self, name: str) -> None:
        from .keys import evdev_code_for_name

        code = evdev_code_for_name(name)
        self._exit_code = int(code) if code is not None else 1

    def set_timeout(self, seconds: int) -> None:
        self._timeout_seconds = max(5, int(seconds))

    def timeout_seconds(self) -> int:
        return self._timeout_seconds

    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_activity)

    def seconds_until_idle(self) -> float:
        return max(0.0, self._timeout_seconds - self.idle_seconds())

    def bump(self) -> None:
        self._note_activity()

    def ignore_for(self, seconds: float) -> None:
        self._ignore_until = time.monotonic() + max(0.0, seconds)

    def start(self) -> None:
        if not evdev_available():
            self.error.emit("python-evdev is not installed.")
            return
        self._paused = False
        self._last_activity = time.monotonic()
        self._idle_emitted = False
        self._scan_devices()
        if not self._devices:
            self.error.emit(
                "No keyboard or mouse devices could be opened. "
                "Add this user to the 'input' group and log in again."
            )
        self._poll.start()
        self._rescan.start()

    def pause(self) -> None:
        self._paused = True
        self._poll.stop()
        self._rescan.stop()

    def resume(self) -> None:
        if not evdev_available():
            return
        self._paused = False
        self.bump()
        self._idle_emitted = False
        if not self._devices:
            self._scan_devices()
        self._poll.start()
        self._rescan.start()

    def stop(self) -> None:
        self._paused = False
        self._poll.stop()
        self._rescan.stop()
        self._close_all()

    def _close_all(self) -> None:
        for notifier in self._notifiers.values():
            notifier.setEnabled(False)
            notifier.deleteLater()
        self._notifiers.clear()
        for device in self._devices.values():
            try:
                device.close()
            except OSError:
                pass
        self._devices.clear()

    def _scan_devices(self) -> None:
        if not evdev_available():
            return
        try:
            present = set(list_devices())
        except OSError as exc:
            log.warning("Could not list input devices: %s", exc)
            return

        for path in list(self._devices):
            if path not in present:
                self._drop_device(path)

        for path in present:
            if path in self._devices:
                continue
            try:
                device = InputDevice(path)
            except OSError:
                continue
            if (device.name or "").startswith("SCR Saver "):
                try:
                    device.close()
                except OSError:
                    pass
                continue
            if not _is_pointer_or_keyboard(device):
                try:
                    device.close()
                except OSError:
                    pass
                continue
            try:
                fd = device.fd
            except OSError:
                continue
            notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
            notifier.activated.connect(lambda *_args, p=path: self._on_device_readable(p))
            self._devices[path] = device
            self._notifiers[path] = notifier
            log.debug("Watching input device %s (%s)", path, device.name)

    def _drop_device(self, path: str) -> None:
        notifier = self._notifiers.pop(path, None)
        if notifier is not None:
            notifier.setEnabled(False)
            notifier.deleteLater()
        device = self._devices.pop(path, None)
        if device is not None:
            try:
                device.close()
            except OSError:
                pass

    def _on_device_readable(self, path: str) -> None:
        device = self._devices.get(path)
        if device is None:
            return
        try:
            events = list(device.read())
        except (OSError, BlockingIOError):
            self._drop_device(path)
            return
        if self._paused:
            return
        escape_pressed = False
        activity = False
        for event in events:
            if event.type == ecodes.EV_KEY and event.code == self._exit_code and event.value == 1:
                escape_pressed = True
            if _is_user_activity(event):
                activity = True
        if activity:
            self._note_activity()
        if escape_pressed:
            self.escape.emit()

    def _note_activity(self) -> None:
        if time.monotonic() < self._ignore_until:
            return
        self._last_activity = time.monotonic()
        if self._idle_emitted:
            self._idle_emitted = False
            self.active.emit()

    def _check_idle(self) -> None:
        if self._paused or self._idle_emitted:
            return
        if self.idle_seconds() >= self._timeout_seconds:
            self._idle_emitted = True
            self.idle.emit()


def _is_pointer_or_keyboard(device: InputDevice) -> bool:
    try:
        caps = device.capabilities()
    except OSError:
        return False
    try:
        props = set(device.input_props())
    except (OSError, AttributeError):
        props = set()
    if getattr(ecodes, "INPUT_PROP_ACCELEROMETER", None) in props:
        return False
    keys = set(caps.get(ecodes.EV_KEY, []))
    rels = set(caps.get(ecodes.EV_REL, []))
    abs_codes = {
        item[0] if isinstance(item, tuple) else item for item in caps.get(ecodes.EV_ABS, [])
    }
    markers = {
        ecodes.KEY_A,
        ecodes.KEY_SPACE,
        ecodes.KEY_ENTER,
        ecodes.KEY_ESC,
        ecodes.BTN_LEFT,
        ecodes.BTN_RIGHT,
        ecodes.BTN_MIDDLE,
        ecodes.BTN_TOUCH,
        ecodes.BTN_MOUSE,
    }
    if keys & markers:
        return True
    if {ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL} & rels:
        return True
    if {ecodes.ABS_MT_POSITION_X, ecodes.ABS_MT_POSITION_Y} & abs_codes:
        return True
    if ecodes.ABS_X in abs_codes and ecodes.BTN_TOOL_FINGER in keys:
        return True
    return False


def _is_user_activity(event) -> bool:
    if event.type in (ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS):
        return True
    return False
