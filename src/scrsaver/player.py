from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication

from . import steam, wineutil
from .config import LAUNCH_PREFIX_EXE, LAUNCH_STEAM, ScreensaverEntry
from .keystrokes import InputEmulator, KeystrokePlayback

log = logging.getLogger(__name__)

WINDOW_PLACE_ATTEMPTS = 40
WINDOW_PLACE_INTERVAL_MS = 250
GRACE_SECONDS = 2.0


@dataclass
class ScreenTarget:
    name: str
    x: int
    y: int
    width: int
    height: int
    desktop: str


class Player(QObject):
    started = pyqtSignal(str)
    stopped = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._procs: list[subprocess.Popen[bytes]] = []
        self._place_timer = QTimer(self)
        self._place_timer.setInterval(WINDOW_PLACE_INTERVAL_MS)
        self._place_timer.timeout.connect(self._try_place_windows)
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(1000)
        self._watch_timer.timeout.connect(self._watch_processes)
        self._place_tries = 0
        self._targets: list[ScreenTarget] = []
        self._playing_name = ""
        self._started_at = 0.0
        self._session_active = False
        self._wine_session = None
        self._interactive = False
        self._muted_leaders: set[int] = set()
        self._steam_appid = ""
        self._steam_seen = False
        self._keystrokes: list[dict] = []
        self._keystroke_loop = False
        self._stop_on_input = False
        self._macro: KeystrokePlayback | None = None
        self._emulator: InputEmulator | None = None
        self._place_by_pid = False
        self._macro_timer = QTimer(self)
        self._macro_timer.setInterval(250)
        self._macro_timer.timeout.connect(self._maybe_start_macro)

    def is_playing(self) -> bool:
        if self._session_active:
            return True
        return any(_alive(proc) for proc in self._procs)

    def playing_name(self) -> str:
        return self._playing_name if self.is_playing() else ""

    def grace_active(self) -> bool:
        return self.is_playing() and (time.monotonic() - self._started_at) < GRACE_SECONDS

    def is_interactive(self) -> bool:
        return self._interactive and self.is_playing()

    def macro_duration(self) -> float:
        if not self._keystrokes:
            return 0.0
        if self._keystroke_loop:
            return 24 * 3600.0
        return max(float(item.get("t") or 0) for item in self._keystrokes) + 2.0

    def wine_session(self):
        return self._wine_session

    def muted_audio_pids(self) -> set[int]:
        if not self._muted_leaders:
            return set()
        from .audio import pids_in_trees

        return pids_in_trees(self._muted_leaders)

    def play(
        self,
        entry: ScreensaverEntry,
        display_mode: str,
        skip_outputs: set[str] | None = None,
        require_screen: bool = False,
        audio_source: str = "",
    ) -> None:
        kind = getattr(entry, "launch_kind", "") or ""
        if kind == LAUNCH_STEAM or entry.is_steam():
            if skip_outputs:
                targets = screen_targets(display_mode)
                targets = [target for target in targets if target.name not in skip_outputs]
                if not targets:
                    if require_screen:
                        self.failed.emit(
                            "No available display. A fullscreen video may be using the selected screen."
                        )
                    else:
                        log.info("Not starting screensaver; no free display")
                    return
            self._play_steam(entry)
            return
        try:
            session = wineutil.session_for(entry)
        except FileNotFoundError as exc:
            self.failed.emit(str(exc))
            return
        targets = screen_targets(display_mode)
        if skip_outputs:
            targets = [target for target in targets if target.name not in skip_outputs]
        if not targets:
            if require_screen:
                self.failed.emit("No available display. A fullscreen video may be using the selected screen.")
            else:
                log.info("Not starting screensaver; no free display")
            return
        if self.is_playing():
            self.stop()
        else:
            wineutil.kill_prefix(session)
        self._ensure_emulator(entry)
        if kind == LAUNCH_PREFIX_EXE or entry.is_prefix_exe():
            self._play_prefix_exe(entry, session, targets, audio_source)
            return
        path = entry.linux_path()
        if not path.is_file():
            self.failed.emit(f"Screensaver file is missing: {path}")
            return
        try:
            wineutil.setup_prefix(session)
        except Exception as exc:
            self.failed.emit(f"Wine prefix setup failed: {exc}")
            return

        try:
            wine_path = wineutil.stage_entry(entry, session)
            wine = wineutil.wine_binary(session)
        except FileNotFoundError as exc:
            self.failed.emit(str(exc))
            return
        except OSError as exc:
            self.failed.emit(f"Could not stage screensaver for Wine: {exc}")
            return

        env = wineutil.wine_env(session)
        procs: list[subprocess.Popen[bytes]] = []
        muted_leaders: list[int] = []
        audio_target = _resolve_audio_target(targets, audio_source) if audio_source and len(targets) > 1 else None
        if audio_target is not None:
            targets = [audio_target] + [target for target in targets if target is not audio_target]
        try:
            from .audio import ensure_silent_sink

            silent_sink = ""
            for target in targets:
                instance_env = dict(env)
                muted = audio_target is not None and target is not audio_target
                if muted:
                    if not silent_sink:
                        silent_sink = ensure_silent_sink()
                    instance_env["PULSE_SINK"] = silent_sink
                    overrides = instance_env.get("WINEDLLOVERRIDES", "")
                    silent_dlls = "winepulse.drv=d;winealsa.drv=d;mmdevapi=d;dsound=d"
                    instance_env["WINEDLLOVERRIDES"] = (
                        f"{overrides};{silent_dlls}" if overrides else silent_dlls
                    )
                cmd = [
                    wine,
                    "explorer",
                    f"/desktop={target.desktop},{target.width}x{target.height}",
                    wine_path,
                    "/s",
                ]
                log.info("Launching %s%s", " ".join(cmd), " (audio muted)" if muted else "")
                proc = wineutil.popen_wine(session, cmd, env=instance_env)
                procs.append(proc)
                if muted:
                    muted_leaders.append(proc.pid)
        except OSError as exc:
            for proc in procs:
                _terminate_group(proc)
            self.failed.emit(f"Could not start Wine: {exc}")
            return

        self._procs = procs
        self._targets = targets
        self._playing_name = entry.name
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = session
        self._interactive = bool(getattr(entry, "interactive", False))
        self._muted_leaders = set(muted_leaders)
        self._place_tries = 0
        self._place_by_pid = False
        self._place_timer.start()
        self._watch_timer.start()
        self.started.emit(entry.name)
        self._arm_macro(entry)

    def _play_steam(self, entry: ScreensaverEntry) -> None:
        appid = str(entry.steam_appid or "").strip()
        if not appid:
            self.failed.emit("This Steam game has no app ID.")
            return
        if steam.steam_binary() is None:
            self.failed.emit("Steam was not found. Install Steam or add it to PATH.")
            return
        if self.is_playing():
            self.stop()
        self._ensure_emulator(entry)
        try:
            steam.launch_app(appid)
        except OSError as exc:
            self.failed.emit(f"Could not launch Steam: {exc}")
            return
        session = None
        if entry.wine_prefix:
            try:
                session = wineutil.session_for(entry)
            except FileNotFoundError:
                session = None
        self._procs = []
        self._targets = []
        self._playing_name = entry.name
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = session
        self._interactive = bool(getattr(entry, "interactive", True))
        self._muted_leaders = set()
        self._steam_appid = appid
        self._steam_seen = False
        self._watch_timer.start()
        self.started.emit(entry.name)
        self._arm_macro(entry)

    def _play_prefix_exe(
        self,
        entry: ScreensaverEntry,
        session,
        targets: list[ScreenTarget],
        audio_source: str,
    ) -> None:
        exe = Path(entry.exe_path).expanduser() if entry.exe_path else Path()
        if not exe.is_file():
            self.failed.emit(f"Program file is missing: {entry.exe_path}")
            return
        try:
            wine = wineutil.wine_binary(session)
            wine_path = wineutil.linux_to_wine_path(exe)
        except FileNotFoundError as exc:
            self.failed.emit(str(exc))
            return
        env = wineutil.wine_env(session)
        procs: list[subprocess.Popen[bytes]] = []
        muted_leaders: list[int] = []
        audio_target = _resolve_audio_target(targets, audio_source) if audio_source and len(targets) > 1 else None
        if audio_target is not None:
            targets = [audio_target] + [target for target in targets if target is not audio_target]
        try:
            from .audio import ensure_silent_sink

            silent_sink = ""
            has_macro = bool(getattr(entry, "keystrokes", None))
            if has_macro and targets:
                _warp_pointer(targets[0])
            for target in targets:
                instance_env = dict(env)
                muted = audio_target is not None and target is not audio_target
                if muted:
                    if not silent_sink:
                        silent_sink = ensure_silent_sink()
                    instance_env["PULSE_SINK"] = silent_sink
                if has_macro:
                    cmd = [wine, wine_path]
                else:
                    cmd = [
                        wine,
                        "explorer",
                        f"/desktop={target.desktop},{target.width}x{target.height}",
                        wine_path,
                    ]
                log.info("Launching prefix program %s", " ".join(cmd))
                proc = wineutil.popen_wine(session, cmd, env=instance_env)
                procs.append(proc)
                if muted:
                    muted_leaders.append(proc.pid)
                break  # games should not be cloned per monitor
        except OSError as exc:
            for proc in procs:
                _terminate_group(proc)
            self.failed.emit(f"Could not start Wine: {exc}")
            return
        self._procs = procs
        self._targets = targets[:1]
        self._playing_name = entry.name
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = session
        self._interactive = bool(getattr(entry, "interactive", False))
        self._muted_leaders = set(muted_leaders)
        self._place_tries = 0
        self._place_by_pid = bool(getattr(entry, "keystrokes", None))
        self._place_timer.start()
        self._watch_timer.start()
        self.started.emit(entry.name)
        self._arm_macro(entry)

    def _arm_macro(self, entry: ScreensaverEntry) -> None:
        self._stop_macro()
        events = list(getattr(entry, "keystrokes", None) or [])
        self._keystrokes = events
        self._keystroke_loop = bool(getattr(entry, "keystroke_loop", False))
        self._stop_on_input = bool(getattr(entry, "stop_on_input", False))
        if not events:
            return
        self._macro_timer.start()

    def _maybe_start_macro(self) -> None:
        if not self._keystrokes or not self._session_active:
            self._macro_timer.stop()
            return
        if self._steam_appid:
            if not steam.app_is_running(self._steam_appid):
                if time.monotonic() - self._started_at < 90:
                    return
                self._macro_timer.stop()
                return
            if time.monotonic() - self._started_at < 2.0:
                return
        elif time.monotonic() - self._started_at < (3.0 if self._place_by_pid else 1.5):
            return
        self._macro_timer.stop()
        extra = " (looping until exit)" if self._keystroke_loop else ""
        if self._stop_on_input:
            extra += " (stop on keyboard or mouse)"
        log.info("Replaying %s recorded input event(s)%s", len(self._keystrokes), extra)
        pointer = "uinput" if self._steam_appid else "xtest"
        self._macro = KeystrokePlayback(
            self._keystrokes,
            self,
            loop=self._keystroke_loop,
            stop_on_input=self._stop_on_input,
            emulator=self._emulator,
            pointer=pointer,
        )
        self._macro.start()

    def _stop_macro(self) -> None:
        self._macro_timer.stop()
        self._keystrokes = []
        self._keystroke_loop = False
        self._stop_on_input = False
        if self._macro is not None:
            self._macro.cancel()
            self._macro.wait(2000)
            self._macro = None

    def _ensure_emulator(self, entry: ScreensaverEntry) -> None:
        self._close_emulator()
        if not getattr(entry, "keystrokes", None):
            return
        emu = InputEmulator()
        try:
            emu.start(list(entry.keystrokes))
        except Exception as exc:
            log.warning("Could not create virtual keyboard/mouse for autoplay: %s", exc)
            return
        self._emulator = emu
        log.info("Virtual keyboard and mouse ready for autoplay")

    def _close_emulator(self) -> None:
        if self._emulator is None:
            return
        try:
            self._emulator.close()
        except Exception:
            pass
        self._emulator = None

    def configure(self, entry: ScreensaverEntry) -> None:
        path = entry.linux_path()
        if not path.is_file():
            self.failed.emit(f"Screensaver file is missing: {path}")
            return
        try:
            session = wineutil.session_for(entry)
            wine_path = wineutil.stage_entry(entry, session)
            wine = wineutil.wine_binary(session)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        cmd = [wine, wine_path, "/c"]
        log.info("Configure %s", " ".join(cmd))
        try:
            wineutil.popen_wine(session, cmd)
        except OSError as exc:
            self.failed.emit(f"Could not open configuration dialog: {exc}")

    def stop(self) -> None:
        was_playing = self._session_active or bool(self._procs) or bool(self._steam_appid)
        self._place_timer.stop()
        self._watch_timer.stop()
        self._stop_macro()
        self._close_emulator()
        self._place_by_pid = False
        _close_saver_windows()
        for proc in self._procs:
            _terminate_group(proc)
        self._procs = []
        self._targets = []
        self._playing_name = ""
        self._session_active = False
        self._interactive = False
        self._muted_leaders = set()
        if self._steam_appid:
            prefix = self._wine_session.prefix if self._wine_session is not None else None
            steam.stop_app(self._steam_appid, prefix)
        elif self._wine_session is not None:
            wineutil.kill_prefix(self._wine_session)
        self._wine_session = None
        self._steam_appid = ""
        self._steam_seen = False
        if was_playing:
            self.stopped.emit()

    def _watch_processes(self) -> None:
        if not self._session_active:
            self._watch_timer.stop()
            return
        if self._steam_appid:
            running = steam.app_is_running(self._steam_appid)
            if running:
                self._steam_seen = True
                return
            if not self._steam_seen and (time.monotonic() - self._started_at) < 90:
                return
            log.info("Steam game %s exited", self._steam_appid)
            self.stop()
            return
        wrappers_alive = any(_alive(proc) for proc in self._procs)
        prefix_alive = bool(self._wine_session and wineutil.list_prefix_pids(self._wine_session))
        if wrappers_alive or prefix_alive:
            return
        log.info("Wine screensaver process exited")
        self.stop()

    def _try_place_windows(self) -> None:
        self._place_tries += 1
        if self._place_by_pid and self._wine_session is not None and self._targets:
            if _place_prefix_windows(self._wine_session, self._targets[0], self._playing_name):
                self._place_timer.stop()
                return
            if self._place_tries >= WINDOW_PLACE_ATTEMPTS:
                self._place_timer.stop()
            return
        remaining = [target for target in self._targets if not _place_window(target)]
        self._targets = remaining
        if not remaining or self._place_tries >= WINDOW_PLACE_ATTEMPTS:
            self._place_timer.stop()


def screen_targets(display_mode: str) -> list[ScreenTarget]:
    app = QGuiApplication.instance()
    if app is None:
        return []
    screens = list(app.screens())
    if not screens:
        return []
    primary = app.primaryScreen() or screens[0]
    if display_mode == "primary":
        screens = [primary]
    elif display_mode.startswith("output:"):
        wanted = display_mode.split(":", 1)[1]
        named = [screen for screen in screens if screen.name() == wanted]
        screens = named or [primary]
    elif display_mode != "all":
        named = [screen for screen in screens if screen.name() == display_mode]
        screens = named or screens
    targets: list[ScreenTarget] = []
    for index, screen in enumerate(screens):
        geo = screen.geometry()
        targets.append(
            ScreenTarget(
                name=screen.name() or f"screen-{index}",
                x=geo.x(),
                y=geo.y(),
                width=max(640, geo.width()),
                height=max(480, geo.height()),
                desktop=f"{wineutil.WINE_DESKTOP_PREFIX}-{index}",
            )
        )
    return targets


def _resolve_audio_target(targets: list[ScreenTarget], audio_source: str) -> ScreenTarget | None:
    if not targets:
        return None
    app = QGuiApplication.instance()
    primary_name = ""
    if app is not None and app.primaryScreen() is not None:
        primary_name = app.primaryScreen().name()
    if audio_source in ("", "primary"):
        for target in targets:
            if target.name == primary_name:
                return target
        return targets[0]
    wanted = audio_source.split(":", 1)[1] if audio_source.startswith("output:") else audio_source
    for target in targets:
        if target.name == wanted:
            return target
    if primary_name:
        for target in targets:
            if target.name == primary_name:
                return target
    return targets[0]


def _alive(proc: subprocess.Popen[bytes]) -> bool:
    return proc.poll() is None


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _close_saver_windows() -> None:
    output = _run_capture(["wmctrl", "-l"])
    if not output:
        return
    marker = wineutil.WINE_DESKTOP_PREFIX.lower()
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        if marker not in parts[3].lower():
            continue
        _run_quiet(["wmctrl", "-i", "-c", parts[0]])


def _warp_pointer(target: ScreenTarget) -> None:
    from .keystrokes import _XTestPointer

    pointer = _XTestPointer()
    if not pointer.ok:
        pointer.close()
        return
    try:
        pointer.move_abs(target.x + target.width // 2, target.y + target.height // 2)
    finally:
        pointer.close()


def _place_prefix_windows(session, target: ScreenTarget, title: str) -> bool:
    pids = set(wineutil.list_prefix_pids(session))
    if not pids:
        return False
    output = _run_capture(["wmctrl", "-lp"])
    if not output:
        return False
    ignored = (
        "wine system tray",
        "winedevice",
        "services.exe",
        "plugplay",
        "rpcss",
        "explorer.exe",
        "wineboot",
        "start.exe",
        "conhost",
        "tabtip",
    )
    matches: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        if pid not in pids:
            continue
        win_title = parts[4] if len(parts) > 4 else ""
        lowered = win_title.lower()
        if not lowered or any(token in lowered for token in ignored):
            continue
        matches.append((parts[0], win_title))
    if not matches:
        return False
    wanted = (title or "").lower()
    chosen = [item for item in matches if wanted and wanted in item[1].lower()] or matches
    placed = False
    for wid, win_title in chosen:
        log.info("Placing %s on %s at %d,%d", win_title or wid, target.name, target.x, target.y)
        _run_quiet(["wmctrl", "-i", "-r", wid, "-e", f"0,{target.x},{target.y},{target.width},{target.height}"])
        _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,above"])
        _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,fullscreen"])
        _run_quiet(["xdotool", "windowmove", wid, str(target.x), str(target.y)])
        _run_quiet(["xdotool", "windowsize", wid, str(target.width), str(target.height)])
        _run_quiet(["wmctrl", "-i", "-a", wid])
        placed = True
    return placed


def _place_window(target: ScreenTarget) -> bool:
    wid = _find_window_id(target.desktop)
    if not wid:
        return False
    log.info("Placing Wine desktop %s on %s at %d,%d", target.desktop, target.name, target.x, target.y)
    _run_quiet(["wmctrl", "-i", "-r", wid, "-e", f"0,{target.x},{target.y},{target.width},{target.height}"])
    _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,above"])
    _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,fullscreen"])
    # If fullscreen snapped to the wrong output, move again then re-assert.
    _run_quiet(["xdotool", "windowmove", wid, str(target.x), str(target.y)])
    _run_quiet(["xdotool", "windowsize", wid, str(target.width), str(target.height)])
    return True


def _find_window_id(title: str) -> str:
    output = _run_capture(["wmctrl", "-l"])
    if output:
        for line in output.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 4 and title.lower() in parts[3].lower():
                return parts[0]
    output = _run_capture(["xdotool", "search", "--name", title])
    if output:
        first = output.split()
        if first:
            return first[0]
    return ""


def _run_quiet(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_capture(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()
