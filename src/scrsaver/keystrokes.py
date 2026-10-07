from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QSocketNotifier, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

try:
    from evdev import InputDevice, UInput, ecodes, list_devices
except ImportError:  # pragma: no cover
    InputDevice = None  # type: ignore[assignment]
    UInput = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]
    list_devices = None  # type: ignore[assignment]

VIRTUAL_INPUT_NAMES = {"SCR Saver keys", "SCR Saver mouse", "SCR Saver input"}
VIRTUAL_DEVICE_NAME = "SCR Saver input"


def evdev_ready() -> bool:
    return InputDevice is not None and list_devices is not None


@dataclass
class RecordedMacro:
    events: list[dict] = field(default_factory=list)
    loop: bool = False
    stop_on_input: bool = True


class KeystrokeRecorder(QObject):
    """Listen for keyboard and mouse events until Escape, without grabbing devices."""

    finished = pyqtSignal(list)
    cancelled = pyqtSignal()
    progress = pyqtSignal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._devices: dict[str, InputDevice] = {}
        self._notifiers: dict[str, QSocketNotifier] = {}
        self._uses_rel: dict[str, bool] = {}
        self._abs_pos: dict[tuple[str, int], int] = {}
        self._events: list[dict] = []
        self._started = 0.0
        self._active = False

    def start(self) -> None:
        if not evdev_ready():
            raise RuntimeError("python-evdev is not installed.")
        self.stop()
        self._events = []
        self._abs_pos = {}
        self._started = time.monotonic()
        self._active = True
        self._scan()
        if not self._devices:
            self._active = False
            raise RuntimeError(
                "No keyboard or mouse could be opened. Add this user to the 'input' group and log in again."
            )

    def stop(self) -> None:
        self._active = False
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
        self._uses_rel.clear()

    def events(self) -> list[dict]:
        return list(self._events)

    def _scan(self) -> None:
        try:
            present = list(list_devices())
        except OSError as exc:
            log.warning("Could not list input devices: %s", exc)
            return
        for path in present:
            if path in self._devices:
                continue
            try:
                device = InputDevice(path)
            except OSError:
                continue
            if (device.name or "") in VIRTUAL_INPUT_NAMES:
                try:
                    device.close()
                except OSError:
                    pass
                continue
            if not _is_keyboard_or_pointer(device):
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
            notifier.activated.connect(lambda *_args, p=path: self._on_readable(p))
            self._devices[path] = device
            self._notifiers[path] = notifier
            self._uses_rel[path] = _device_has_rel_xy(device)

    def _on_readable(self, path: str) -> None:
        if not self._active:
            return
        device = self._devices.get(path)
        if device is None:
            return
        try:
            incoming = list(device.read())
        except (OSError, BlockingIOError):
            return
        escape = getattr(ecodes, "KEY_ESC", 1)
        for event in incoming:
            recorded = self._record_event(path, event, escape)
            if recorded == "escape":
                self._active = False
                self.finished.emit(list(self._events))
                return
            if recorded:
                self.progress.emit(len(self._events))

    def _record_event(self, path: str, event, escape: int) -> bool | str:
        if event.type == ecodes.EV_KEY:
            if event.value not in (0, 1):
                return False
            if event.code in _mouse_buttons():
                self._events.append(
                    _event("btn", event.code, _btn_name(event.code), int(event.value), self._started)
                )
                return True
            if event.code == escape and event.value == 1:
                device = self._devices.get(path)
                if device is not None and _is_pointer(device) and not _is_typing_keyboard(device):
                    return False
                return "escape"
            name = _code_name(event.code, "KEY")
            if name.startswith("KEY_"):
                self._events.append(_event("key", event.code, name, int(event.value), self._started))
                return True
            return False
        if event.type == ecodes.EV_REL:
            if event.code not in _rel_codes() or event.value == 0:
                return False
            name = _code_name(event.code, "REL")
            self._events.append(_event("rel", event.code, name or f"REL_{event.code}", int(event.value), self._started))
            return True
        if event.type == ecodes.EV_ABS:
            if self._uses_rel.get(path):
                return False
            abs_x = getattr(ecodes, "ABS_X", 0)
            abs_y = getattr(ecodes, "ABS_Y", 1)
            if event.code not in (abs_x, abs_y):
                return False
            key = (path, int(event.code))
            prev = self._abs_pos.get(key)
            self._abs_pos[key] = int(event.value)
            if prev is None:
                return False
            delta = int(event.value) - prev
            if delta == 0:
                return False
            rel_code = getattr(ecodes, "REL_X", 0) if event.code == abs_x else getattr(ecodes, "REL_Y", 1)
            name = "REL_X" if event.code == abs_x else "REL_Y"
            self._events.append(_event("rel", rel_code, name, delta, self._started))
            return True
        return False


class RecordKeystrokesDialog(QDialog):
    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        on_start: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_start = on_start
        self._on_stop = on_stop
        self._recorder = KeystrokeRecorder(self)
        self._recorder.finished.connect(self._on_finished)
        self._recorder.progress.connect(self._on_progress)
        self._events: list[dict] | None = None
        self._started = False
        self._cleaned = False
        self.setWindowTitle("Record autoplay input")
        self.setModal(True)
        self.resize(520, 220)
        layout = QVBoxLayout(self)
        self.blurb = QLabel(
            f"SCR Saver will launch {title} and record the keys you press, plus mouse "
            "movement and clicks, with timing.\n\n"
            "Get the program to the point where it plays itself, then press Escape "
            "to leave the program and finish recording. Escape itself is not replayed."
        )
        self.blurb.setWordWrap(True)
        layout.addWidget(self.blurb)
        self.status = QLabel("Press Start recording to launch the program.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.start_btn = QPushButton("Start recording")
        self.start_btn.clicked.connect(self._begin)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        row.addWidget(self.start_btn)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

    def recorded_events(self) -> list[dict] | None:
        return self._events

    def _begin(self) -> None:
        try:
            self._recorder.start()
        except Exception as exc:
            QMessageBox.warning(self, "Record autoplay input", str(exc))
            return
        self._started = True
        self.start_btn.setEnabled(False)
        self.status.setText("Recording. Launching the program…")
        try:
            if self._on_start:
                self._on_start()
        except Exception as exc:
            self._recorder.stop()
            QMessageBox.warning(self, "Record autoplay input", str(exc))
            self.reject()
            return
        self.status.setText("Recording keys and mouse. Press Escape when autoplay is running.")

    def _on_progress(self, count: int) -> None:
        self.status.setText(
            f"Recording… {count} input event{'s' if count != 1 else ''}. "
            "Press Escape when autoplay is running."
        )

    def _on_finished(self, events: list) -> None:
        self._events = list(events)
        self._cleanup_program()
        self._recorder.stop()
        self.accept()

    def _cancel(self) -> None:
        self._events = None
        self._cleanup_program()
        self._recorder.stop()
        self.reject()

    def _cleanup_program(self) -> None:
        if not self._started or self._cleaned:
            return
        self._cleaned = True
        try:
            if self._on_stop:
                self._on_stop()
        except Exception as exc:
            log.warning("Could not stop program after autoplay recording: %s", exc)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._events is None:
            self._cleanup_program()
            self._recorder.stop()
        super().closeEvent(event)


class LoopKeystrokesDialog(QDialog):
    def __init__(
        self,
        title: str,
        events: list[dict],
        parent: QWidget | None = None,
        *,
        loop_default: bool = False,
        stop_on_input_default: bool = True,
    ) -> None:
        super().__init__(parent)
        keys, mouse = _count_kinds(events)
        self.setWindowTitle("Autoplay recording")
        self.setModal(True)
        self.resize(500, 280)
        layout = QVBoxLayout(self)
        parts = []
        if keys:
            parts.append(f"{keys} key event{'s' if keys != 1 else ''}")
        if mouse:
            parts.append(f"{mouse} mouse event{'s' if mouse != 1 else ''}")
        summary = QLabel(
            f"Recorded {' and '.join(parts) or 'input'} for {title}.\n\n"
            "This sequence plays back when the game starts as a screensaver or interactive demo. "
            "SCR Saver takes control of the mouse and presses the recorded keys until you "
            "step in or exit the game."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        self.loop_box = QCheckBox("Loop this recording until you exit the game")
        self.loop_box.setChecked(bool(loop_default))
        layout.addWidget(self.loop_box)
        self.stop_box = QCheckBox("Stop playback when I press a key, move the mouse, or click")
        self.stop_box.setChecked(bool(stop_on_input_default))
        layout.addWidget(self.stop_box)
        hint = QLabel(
            "If the second box is checked, the first key, mouse move, or click you make "
            "stops autoplay and returns the mouse to you. The game stays open until the "
            "interactive exit key. Uncheck it to keep holding the mouse until you exit."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(save)
        layout.addLayout(row)

    def loop_enabled(self) -> bool:
        return self.loop_box.isChecked()

    def stop_on_input_enabled(self) -> bool:
        return self.stop_box.isChecked()


class InputEmulator:
    """Virtual keyboard plus mouse, created before the game so it can see them."""

    def __init__(self) -> None:
        self.keyboard = None
        self.mouse = None

    def start(self, events: list[dict] | None = None) -> None:
        if UInput is None or ecodes is None:
            raise RuntimeError("python-evdev is not installed.")
        self.close()
        key_codes = {code for code in range(1, 248)}
        for item in events or []:
            if str(item.get("kind") or "key") == "key":
                try:
                    code = int(item["code"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 < code < 256:
                    key_codes.add(code)
        props = [ecodes.INPUT_PROP_POINTER] if hasattr(ecodes, "INPUT_PROP_POINTER") else None
        self.keyboard = UInput(
            {ecodes.EV_KEY: sorted(key_codes)},
            name="SCR Saver keys",
            bustype=0x03,
            vendor=0x5253,
            product=0x0001,
            phys="scrsaver/keys",
        )
        self.mouse = UInput(
            {
                ecodes.EV_KEY: sorted(_mouse_buttons() or {0x110, 0x111, 0x112}),
                ecodes.EV_REL: sorted(_rel_codes() or {0, 1}),
            },
            name="SCR Saver mouse",
            bustype=0x03,
            vendor=0x5253,
            product=0x0002,
            phys="scrsaver/mouse",
            input_props=props,
        )
        time.sleep(0.25)

    def close(self) -> None:
        for device in (self.keyboard, self.mouse):
            if device is None:
                continue
            try:
                device.close()
            except OSError:
                pass
        self.keyboard = None
        self.mouse = None


class KeystrokePlayback(QThread):
    def __init__(
        self,
        events: list[dict],
        parent: QObject | None = None,
        *,
        loop: bool = False,
        stop_on_input: bool = False,
        emulator: InputEmulator | None = None,
        pointer: str = "auto",
    ) -> None:
        super().__init__(parent)
        self._events = normalize_autoplay_events(events)
        self._loop = bool(loop)
        self._stop_on_input = bool(stop_on_input)
        self._emulator = emulator
        self._pointer = pointer
        self._stop = threading.Event()
        self._ignore_input_until = 0.0

    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self._events:
            return
        if UInput is None or ecodes is None:
            log.warning("Cannot replay autoplay input: python-evdev is missing")
            return
        owns = self._emulator is None
        emu = self._emulator
        if emu is None:
            emu = InputEmulator()
            try:
                emu.start(self._events)
            except OSError as exc:
                log.warning("Cannot open uinput for autoplay playback: %s", exc)
                return
        xtest = _XTestPointer() if self._pointer in ("auto", "xtest") else None
        if xtest is not None and not xtest.ok:
            xtest.close()
            xtest = None
        if self._pointer == "xtest" and xtest is None:
            log.warning("X11 mouse playback was requested but DISPLAY/XTest is unavailable")
        use_uinput_mouse = self._pointer == "uinput" or xtest is None
        log.info(
            "Autoplay pointer via %s",
            "X11 XTest" if xtest is not None and not use_uinput_mouse else "uinput mouse",
        )
        watch, grabbed = _open_physical_devices()
        if grabbed:
            log.info("Took control of %s mouse device(s) for autoplay", len(grabbed))
        else:
            log.warning(
                "Could not take exclusive control of a mouse; injected events will still play"
            )
        held: set[int] = set()
        self._ignore_input_until = time.monotonic() + 0.8
        try:
            first = True
            while not self._stop.is_set():
                self._play_cycle(
                    emu, xtest, use_uinput_mouse, held, watch, skip_lead=not first
                )
                if not self._loop or self._stop.is_set():
                    break
                first = False
                pause_until = time.monotonic() + 0.2
                while time.monotonic() < pause_until and not self._stop.is_set():
                    if self._user_interrupted(watch):
                        log.info("Stopping autoplay playback because of keyboard or mouse input")
                        self._stop.set()
                        break
                    time.sleep(0.02)
            _release_held(emu, held)
        finally:
            _release_physical_devices(watch, grabbed)
            if xtest is not None:
                xtest.close()
            if owns:
                emu.close()

    def _play_cycle(
        self,
        emu: InputEmulator,
        xtest,
        use_uinput_mouse: bool,
        held: set[int],
        watch: list,
        *,
        skip_lead: bool,
    ) -> None:
        origin = 0.0
        if skip_lead:
            origin = float(self._events[0].get("t") or 0)
        t0 = time.monotonic()
        for target, frame in _iter_frames(self._events, origin):
            if self._stop.is_set():
                return
            remaining = target - (time.monotonic() - t0)
            while remaining > 0 and not self._stop.is_set():
                if self._user_interrupted(watch):
                    log.info("Stopping autoplay playback because of keyboard or mouse input")
                    self._stop.set()
                    return
                time.sleep(min(0.02, remaining))
                remaining = target - (time.monotonic() - t0)
            if self._stop.is_set():
                return
            if self._user_interrupted(watch):
                log.info("Stopping autoplay playback because of keyboard or mouse input")
                self._stop.set()
                return
            if not _emit_frame(emu, xtest, use_uinput_mouse, frame, held):
                self._stop.set()
                return

    def _user_interrupted(self, watch: list) -> bool:
        if not watch:
            return False
        if time.monotonic() < self._ignore_input_until or not self._stop_on_input:
            _drain_devices(watch)
            return False
        return _physical_activity(watch)


def record_autoplay_keys(
    parent: QWidget | None,
    title: str,
    *,
    on_start: Callable[[], None],
    on_stop: Callable[[], None],
    loop_default: bool = False,
    stop_on_input_default: bool = True,
) -> RecordedMacro | None:
    dialog = RecordKeystrokesDialog(title, parent, on_start=on_start, on_stop=on_stop)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    events = dialog.recorded_events() or []
    loop = False
    stop_on_input = True
    if events:
        options = LoopKeystrokesDialog(
            title,
            events,
            parent,
            loop_default=loop_default,
            stop_on_input_default=stop_on_input_default,
        )
        options.exec()
        loop = options.loop_enabled()
        stop_on_input = options.stop_on_input_enabled()
    return RecordedMacro(events=events, loop=loop, stop_on_input=stop_on_input)


def _event(kind: str, code: int, name: str, value: int, started: float) -> dict:
    return {
        "t": round(time.monotonic() - started, 4),
        "kind": kind,
        "code": int(code),
        "name": name,
        "value": int(value),
    }


def normalize_autoplay_events(events: list[dict]) -> list[dict]:
    buttons = _mouse_buttons() or set(range(0x110, 0x118))
    cleaned: list[dict] = []
    for item in events:
        kind = str(item.get("kind") or "key")
        try:
            code = int(item["code"])
            value = int(item.get("value") or 0)
            t = float(item.get("t") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("name") or "")
        if kind == "key" and code in buttons:
            kind = "btn"
            if not name or name.startswith("KEY_"):
                name = _btn_name(code)
        cleaned.append({"t": t, "kind": kind, "code": code, "name": name, "value": value})
    return cleaned


def _iter_frames(events: list[dict], origin: float):
    index = 0
    count = len(events)
    while index < count:
        target = max(0.0, float(events[index].get("t") or 0) - origin)
        frame = [events[index]]
        index += 1
        while index < count:
            other = max(0.0, float(events[index].get("t") or 0) - origin)
            if other - target >= 0.001:
                break
            frame.append(events[index])
            index += 1
        yield target, frame


def _emit_frame(emu: InputEmulator, xtest, use_uinput_mouse: bool, frame: list[dict], held: set[int]) -> bool:
    mouse_wrote = False
    key_wrote = False
    rel_x = 0
    rel_y = 0
    for item in frame:
        kind = str(item.get("kind") or "key")
        code = int(item["code"])
        value = int(item.get("value") or 0)
        try:
            if kind == "rel":
                rel_x, rel_y, mouse_wrote = _emit_rel(
                    emu, xtest, use_uinput_mouse, code, value, rel_x, rel_y, mouse_wrote
                )
            elif kind == "btn":
                if xtest is not None and xtest.ok and not use_uinput_mouse:
                    xtest.button(code, value)
                elif emu.mouse is not None:
                    emu.mouse.write(ecodes.EV_KEY, code, value)
                    mouse_wrote = True
                if value == 1:
                    held.add(code)
                elif value == 0:
                    held.discard(code)
            else:
                if emu.keyboard is not None:
                    emu.keyboard.write(ecodes.EV_KEY, code, value)
                    key_wrote = True
                if value == 1:
                    held.add(code)
                elif value == 0:
                    held.discard(code)
        except OSError as exc:
            log.warning("Autoplay playback write failed: %s", exc)
            return False
    if xtest is not None and xtest.ok and not use_uinput_mouse and (rel_x or rel_y):
        xtest.rel(rel_x, rel_y)
    try:
        if mouse_wrote and emu.mouse is not None:
            emu.mouse.syn()
        if key_wrote and emu.keyboard is not None:
            emu.keyboard.syn()
    except OSError as exc:
        log.warning("Autoplay playback syn failed: %s", exc)
        return False
    return True


def _emit_rel(emu, xtest, use_uinput_mouse: bool, code: int, value: int, rel_x: int, rel_y: int, mouse_wrote: bool):
    rel_x_code = getattr(ecodes, "REL_X", 0)
    rel_y_code = getattr(ecodes, "REL_Y", 1)
    wheel = getattr(ecodes, "REL_WHEEL", 8)
    hwheel = getattr(ecodes, "REL_HWHEEL", 6)
    if xtest is not None and xtest.ok and not use_uinput_mouse:
        if code == rel_x_code:
            return rel_x + value, rel_y, mouse_wrote
        if code == rel_y_code:
            return rel_x, rel_y + value, mouse_wrote
        if code == wheel:
            xtest.wheel(value, horizontal=False)
            return rel_x, rel_y, mouse_wrote
        if code == hwheel:
            xtest.wheel(value, horizontal=True)
            return rel_x, rel_y, mouse_wrote
    if emu.mouse is not None:
        emu.mouse.write(ecodes.EV_REL, code, value)
        mouse_wrote = True
    return rel_x, rel_y, mouse_wrote


class _XTestPointer:
    def __init__(self) -> None:
        self._dpy = None
        self._x11 = None
        self._xtst = None
        try:
            import ctypes
            import ctypes.util
            import os

            if not os.environ.get("DISPLAY"):
                return
            x11_name = ctypes.util.find_library("X11")
            xtst_name = ctypes.util.find_library("Xtst")
            if not x11_name or not xtst_name:
                return
            self._x11 = ctypes.cdll.LoadLibrary(x11_name)
            self._xtst = ctypes.cdll.LoadLibrary(xtst_name)
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            dpy = self._x11.XOpenDisplay(None)
            if not dpy:
                return
            self._dpy = dpy
            self._xtst.XTestFakeRelativeMotionEvent.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            self._xtst.XTestFakeMotionEvent.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            self._xtst.XTestFakeButtonEvent.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            self._x11.XFlush.argtypes = [ctypes.c_void_p]
            self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        except Exception as exc:
            log.debug("XTest pointer unavailable: %s", exc)
            self._dpy = None

    @property
    def ok(self) -> bool:
        return bool(self._dpy)

    def move_abs(self, x: int, y: int) -> None:
        if not self._dpy:
            return
        self._xtst.XTestFakeMotionEvent(self._dpy, -1, int(x), int(y), 0)
        self._x11.XFlush(self._dpy)

    def rel(self, dx: int, dy: int) -> None:
        if not self._dpy or (not dx and not dy):
            return
        self._xtst.XTestFakeRelativeMotionEvent(self._dpy, int(dx), int(dy), 0)
        self._x11.XFlush(self._dpy)

    def button(self, code: int, value: int) -> None:
        if not self._dpy:
            return
        mapping = {0x110: 1, 0x111: 3, 0x112: 2, 0x113: 8, 0x114: 9, 0x115: 9, 0x116: 8}
        button = mapping.get(int(code))
        if not button:
            return
        self._xtst.XTestFakeButtonEvent(self._dpy, button, 1 if value else 0, 0)
        self._x11.XFlush(self._dpy)

    def wheel(self, value: int, *, horizontal: bool) -> None:
        if not self._dpy or not value:
            return
        if horizontal:
            button = 7 if value < 0 else 6
        else:
            button = 5 if value < 0 else 4
        for _ in range(min(20, abs(int(value)))):
            self._xtst.XTestFakeButtonEvent(self._dpy, button, 1, 0)
            self._xtst.XTestFakeButtonEvent(self._dpy, button, 0, 0)
        self._x11.XFlush(self._dpy)

    def close(self) -> None:
        if self._dpy and self._x11 is not None:
            try:
                self._x11.XCloseDisplay(self._dpy)
            except Exception:
                pass
        self._dpy = None


def _btn_name(code: int) -> str:
    raw = None
    if ecodes is not None:
        table = getattr(ecodes, "BTN", {})
        if isinstance(table, dict):
            raw = table.get(code)
        if raw is None:
            raw = getattr(ecodes, "bytype", {}).get(getattr(ecodes, "EV_KEY", 1), {}).get(code)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if isinstance(raw, str) and raw:
        return raw
    return f"BTN_{code}"


def _count_kinds(events: list[dict]) -> tuple[int, int]:
    keys = 0
    mouse = 0
    for item in events:
        kind = str(item.get("kind") or "key")
        if kind == "key":
            keys += 1
        else:
            mouse += 1
    return keys, mouse


def _mouse_buttons() -> set[int]:
    names = ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE", "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK")
    found: set[int] = set()
    if ecodes is None:
        return found
    for name in names:
        value = getattr(ecodes, name, None)
        if isinstance(value, int):
            found.add(value)
    return found


def _rel_codes() -> set[int]:
    names = ("REL_X", "REL_Y", "REL_WHEEL", "REL_HWHEEL", "REL_Z")
    found: set[int] = set()
    if ecodes is None:
        return found
    for name in names:
        value = getattr(ecodes, name, None)
        if isinstance(value, int):
            found.add(value)
    return found


def _is_keyboard_or_pointer(device: InputDevice) -> bool:
    return _is_typing_keyboard(device) or _is_pointer(device)


def _is_pointer(device: InputDevice) -> bool:
    try:
        caps = device.capabilities()
    except OSError:
        return False
    keys = set(caps.get(ecodes.EV_KEY, []))
    rels = set(caps.get(ecodes.EV_REL, []))
    buttons = {
        getattr(ecodes, "BTN_LEFT", 0x110),
        getattr(ecodes, "BTN_RIGHT", 0x111),
        getattr(ecodes, "BTN_MIDDLE", 0x112),
    }
    if keys & buttons:
        return True
    if {getattr(ecodes, "REL_X", 0), getattr(ecodes, "REL_Y", 1)} & rels:
        return True
    return False


def _is_typing_keyboard(device: InputDevice) -> bool:
    try:
        keys = set(device.capabilities().get(ecodes.EV_KEY, []))
    except OSError:
        return False
    needed = {getattr(ecodes, "KEY_A", 30), getattr(ecodes, "KEY_Z", 44)}
    return needed <= keys


def _device_has_rel_xy(device: InputDevice) -> bool:
    try:
        rels = set(device.capabilities().get(ecodes.EV_REL, []))
    except OSError:
        return False
    return bool({getattr(ecodes, "REL_X", 0), getattr(ecodes, "REL_Y", 1)} & rels)


def _open_physical_devices() -> tuple[list, list]:
    if not evdev_ready():
        return [], []
    found = []
    grabbed = []
    try:
        paths = list(list_devices())
    except OSError:
        return [], []
    for path in paths:
        try:
            device = InputDevice(path)
        except OSError:
            continue
        if (device.name or "") in VIRTUAL_INPUT_NAMES:
            try:
                device.close()
            except OSError:
                pass
            continue
        if not _is_keyboard_or_pointer(device):
            try:
                device.close()
            except OSError:
                pass
            continue
        try:
            import os as _os

            _os.set_blocking(device.fd, False)
        except OSError:
            pass
        found.append(device)
        if _is_pointer(device) and not _is_typing_keyboard(device):
            try:
                device.grab()
                grabbed.append(device)
            except OSError as exc:
                log.warning("Could not take control of %s: %s", device.name, exc)
    return found, grabbed


def _release_physical_devices(watch: list, grabbed: list) -> None:
    for device in grabbed:
        try:
            device.ungrab()
        except OSError:
            pass
    for device in watch:
        try:
            device.close()
        except OSError:
            pass


def _drain_devices(devices: list) -> None:
    for device in devices:
        try:
            list(device.read())
        except (OSError, BlockingIOError):
            pass


def _physical_activity(devices: list) -> bool:
    for device in devices:
        try:
            incoming = list(device.read())
        except BlockingIOError:
            continue
        except OSError:
            continue
        for event in incoming:
            if event.type == ecodes.EV_KEY and event.value in (0, 1):
                return True
            if event.type == ecodes.EV_REL and event.value:
                return True
            if event.type == ecodes.EV_ABS:
                return True
    return False


def _release_held(emu: InputEmulator, held: set[int]) -> None:
    if not held or ecodes is None:
        return
    buttons = _mouse_buttons() or set(range(0x110, 0x118))
    for code in list(held):
        target = emu.mouse if code in buttons else emu.keyboard
        if target is None:
            continue
        try:
            target.write(ecodes.EV_KEY, code, 0)
        except OSError:
            pass
    for target in (emu.mouse, emu.keyboard):
        if target is None:
            continue
        try:
            target.syn()
        except OSError:
            pass
    held.clear()


def _code_name(code: int, prefix: str) -> str:
    table = getattr(ecodes, prefix, {}) if ecodes is not None else {}
    name = table.get(code) if isinstance(table, dict) else None
    if isinstance(name, (list, tuple)):
        name = name[0] if name else ""
    if isinstance(name, str) and name:
        return name
    return f"{prefix}_{code}"
