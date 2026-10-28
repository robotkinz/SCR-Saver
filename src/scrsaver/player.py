from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication

from . import htmlplay, steam, wineutil
from .config import LAUNCH_PREFIX_EXE, LAUNCH_STEAM, ScreensaverEntry
from .keystrokes import InputEmulator, KeystrokePlayback

log = logging.getLogger(__name__)

WINDOW_PLACE_ATTEMPTS = 40
WINDOW_PLACE_INTERVAL_MS = 250
GRACE_SECONDS = 2.0
GAMESCOPE_WMCLASS = "scrsaver-gs"


@dataclass
class ScreenTarget:
    name: str
    x: int
    y: int
    width: int
    height: int
    desktop: str
    tiled: bool = False
    undecorated: bool = False
    scale_content: bool = False
    content_width: int = 0
    content_height: int = 0


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
        self._playing_id = ""
        self._started_at = 0.0
        self._session_active = False
        self._wine_session = None
        self._wine_sessions: list = []
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

    def playing_id(self) -> str:
        return self._playing_id if self.is_playing() else ""

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

    def wine_sessions(self) -> list:
        sessions = list(self._wine_sessions)
        if self._wine_session is not None and all(
            getattr(item, "prefix", None) != self._wine_session.prefix for item in sessions
        ):
            sessions.insert(0, self._wine_session)
        return sessions

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
        tile: bool = False,
        hide_decorations: bool = False,
        scale_content: bool = False,
    ) -> None:
        kind = getattr(entry, "launch_kind", "") or ""
        if kind == LAUNCH_STEAM or entry.is_steam():
            if skip_outputs:
                targets = screen_targets(display_mode)
                targets = [target for target in targets if target.name not in skip_outputs]
                if not _have_targets(targets, require_screen, tile, self.failed.emit):
                    return
            self._play_steam(entry)
            return
        source = entry.linux_path()
        if source.is_file() and htmlplay.is_html_wrapper(source):
            targets = screen_targets(display_mode)
            if skip_outputs:
                targets = [target for target in targets if target.name not in skip_outputs]
            if tile:
                targets = _apply_tiling(
                    targets, hide_decorations=hide_decorations, scale_content=scale_content
                )
            if not _have_targets(targets, require_screen, tile, self.failed.emit):
                return
            self._play_html(entry, source, targets)
            return
        try:
            session = wineutil.session_for(entry)
        except FileNotFoundError as exc:
            self.failed.emit(str(exc))
            return
        targets = screen_targets(display_mode)
        if skip_outputs:
            targets = [target for target in targets if target.name not in skip_outputs]
        if tile:
            targets = _apply_tiling(
                targets, hide_decorations=hide_decorations, scale_content=scale_content
            )
        if not _have_targets(targets, require_screen, tile, self.failed.emit):
            return
        if any(target.scale_content for target in targets) and shutil.which("gamescope") is None:
            self.failed.emit(
                "Scaling a tiled screensaver needs gamescope. "
                "Install the gamescope package, or turn off Scale content."
            )
            return
        if self.is_playing():
            self.stop()
        else:
            wineutil.kill_prefix(session)
            wineutil.kill_display_clones(session)
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
        ad_module = getattr(entry, "ad_module", "") or ""
        select_ad = None
        if ad_module:
            from .afterdark import select_module as select_ad

            try:
                select_ad(session, entry.ad_folder, entry.ad_module)
            except Exception as exc:
                self.failed.emit(f"Could not select After Dark module {entry.ad_module}: {exc}")
                return

        env = wineutil.wine_env(session)
        procs: list[subprocess.Popen[bytes]] = []
        muted_leaders: list[int] = []
        sessions_used = [session]
        audio_target = _resolve_audio_target(targets, audio_source) if audio_source and len(targets) > 1 else None
        if audio_target is not None:
            targets = [audio_target] + [target for target in targets if target is not audio_target]
        isolate = bool(ad_module) and len(targets) > 1
        try:
            from .audio import ensure_silent_sink

            silent_sink = ""
            for index, target in enumerate(targets):
                instance_session = session
                instance_env = dict(env)
                if isolate and index > 0:
                    instance_session = wineutil.clone_session_for_display(session, index)
                    sessions_used.append(instance_session)
                    instance_env = wineutil.wine_env(instance_session)
                    if select_ad is not None:
                        try:
                            select_ad(instance_session, entry.ad_folder, entry.ad_module)
                        except Exception as exc:
                            log.warning("After Dark clone could not select %s: %s", ad_module, exc)
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
                desk_w, desk_h = _desktop_size(target)
                play_path = wineutil.screentime_play_path(entry, instance_session) or wine_path
                cmd = [
                    wine,
                    "explorer",
                    f"/desktop={target.desktop},{desk_w}x{desk_h}",
                    play_path,
                ]
                # ScreenTime for Flash: the .scr is only a launcher. The movie is
                # the companion projector EXE, which is not a /s screensaver.
                if play_path == wine_path:
                    cmd.append("/s")
                cmd = _wrap_gamescope(cmd, target, instance_env)
                log.info("Launching %s%s", " ".join(cmd), " (audio muted)" if muted else "")
                proc = wineutil.popen_wine(instance_session, cmd, env=instance_env)
                procs.append(proc)
                if muted:
                    muted_leaders.append(proc.pid)
        except OSError as exc:
            for proc in procs:
                _terminate_group(proc)
            wineutil.kill_display_clones(session)
            self.failed.emit(f"Could not start Wine: {exc}")
            return

        self._procs = procs
        self._targets = targets
        self._playing_name = entry.name
        self._playing_id = entry.id
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = session
        self._wine_sessions = sessions_used
        self._interactive = bool(getattr(entry, "interactive", False))
        self._muted_leaders = set(muted_leaders)
        self._place_tries = 0
        self._place_by_pid = False
        self._place_timer.start()
        self._watch_timer.start()
        self.started.emit(entry.name)
        self._arm_macro(entry)

    def _play_html(self, entry: ScreensaverEntry, source: Path, targets: list[ScreenTarget]) -> None:
        try:
            html_dir = htmlplay.prepare(source)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        if self.is_playing():
            self.stop()
        procs: list[subprocess.Popen[bytes]] = []
        try:
            for target in targets:
                cmd = htmlplay.browser_cmd(
                    html_dir,
                    x=target.x,
                    y=target.y,
                    width=target.width,
                    height=target.height,
                    desktop=target.desktop,
                    kiosk=not target.tiled,
                )
                log.info("Launching HTML screensaver %s", " ".join(cmd[:8]))
                env = dict(os.environ)
                procs.append(
                    subprocess.Popen(
                        cmd,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                )
        except FileNotFoundError as exc:
            for proc in procs:
                _terminate_group(proc)
            self.failed.emit(str(exc))
            return
        except OSError as exc:
            for proc in procs:
                _terminate_group(proc)
            self.failed.emit(f"Could not start the HTML screensaver: {exc}")
            return
        self._procs = procs
        self._targets = targets
        self._playing_name = entry.name
        self._playing_id = entry.id
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = None
        self._wine_sessions = []
        self._interactive = bool(getattr(entry, "interactive", False))
        self._muted_leaders = set()
        self._place_tries = 0
        self._place_by_pid = True
        self._place_timer.start()
        self._watch_timer.start()
        self.started.emit(entry.name)

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
        self._playing_id = entry.id
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = session
        self._wine_sessions = [session] if session is not None else []
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
                    desk_w, desk_h = _desktop_size(target)
                    cmd = [
                        wine,
                        "explorer",
                        f"/desktop={target.desktop},{desk_w}x{desk_h}",
                        wine_path,
                    ]
                cmd = _wrap_gamescope(cmd, target, instance_env)
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
        self._playing_id = entry.id
        self._started_at = time.monotonic()
        self._session_active = True
        self._wine_session = session
        self._wine_sessions = [session]
        self._interactive = bool(getattr(entry, "interactive", False))
        self._muted_leaders = set(muted_leaders)
        self._place_tries = 0
        self._place_by_pid = bool(getattr(entry, "keystrokes", None)) and not any(
            target.scale_content for target in targets
        )
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
        if getattr(entry, "ad_module", ""):
            from .afterdark import open_settings, select_module

            try:
                select_module(session, entry.ad_folder, entry.ad_module)
                open_settings(session)
            except Exception as exc:
                self.failed.emit(f"Could not open After Dark settings: {exc}")
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
        self._playing_id = ""
        self._session_active = False
        self._interactive = False
        self._muted_leaders = set()
        if self._steam_appid:
            prefix = self._wine_session.prefix if self._wine_session is not None else None
            steam.stop_app(self._steam_appid, prefix)
        else:
            seen: set[str] = set()
            for sess in self.wine_sessions():
                key = str(getattr(sess, "prefix", "") or "")
                if key in seen:
                    continue
                seen.add(key)
                wineutil.kill_prefix(sess)
        self._wine_session = None
        self._wine_sessions = []
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
        prefix_alive = any(wineutil.list_prefix_pids(sess) for sess in self.wine_sessions())
        if wrappers_alive or prefix_alive:
            return
        log.info("Wine screensaver process exited")
        self.stop()

    def _try_place_windows(self) -> None:
        self._place_tries += 1
        tiled = any(target.tiled for target in self._targets)
        html = self._place_by_pid and self._wine_session is None
        stop_after = 12 if tiled or html else 1
        if self._place_by_pid and self._targets:
            if self._wine_session is not None:
                placed = _place_prefix_windows(
                    self._wine_session, self._targets[0], self._playing_name
                )
            else:
                placed = _place_html_windows(self._procs, self._targets)
            if (placed and self._place_tries >= stop_after) or self._place_tries >= WINDOW_PLACE_ATTEMPTS:
                self._place_timer.stop()
            return
        if tiled:
            missing = False
            for index, target in enumerate(self._targets):
                if target.scale_content:
                    proc = None
                    if index < len(self._procs):
                        proc = self._procs[index]
                    elif self._procs:
                        proc = self._procs[0]
                    if not _place_scaled_window(target, proc):
                        missing = True
                elif not _place_window(target):
                    missing = True
            if (not missing and self._place_tries >= stop_after) or self._place_tries >= WINDOW_PLACE_ATTEMPTS:
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
    output = _run_capture(["wmctrl", "-lx"])
    if not output:
        output = _run_capture(["wmctrl", "-l"])
    if not output:
        return
    marker = wineutil.WINE_DESKTOP_PREFIX.lower()
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        blob = line.lower()
        if marker not in blob and GAMESCOPE_WMCLASS not in blob:
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
        _apply_window_geometry(wid, target)
        _run_quiet(["wmctrl", "-i", "-a", wid])
        placed = True
    return placed


def _place_html_windows(procs: list[subprocess.Popen[bytes]], targets: list[ScreenTarget]) -> bool:
    placed = True
    missing: list[tuple[int, ScreenTarget]] = []
    for index, target in enumerate(targets):
        wid = ""
        if index < len(procs) and procs[index].pid:
            wid = _find_window_for_pids(_descendant_pids(procs[index].pid))
        if not wid:
            wid = _find_window_by_class(target.desktop)
        if not wid:
            placed = False
            if index < len(procs) and procs[index].pid:
                missing.append((procs[index].pid, target))
            continue
        log.info(
            "Placing HTML screensaver on %s at %d,%d (%dx%d)",
            target.name,
            target.x,
            target.y,
            target.width,
            target.height,
        )
        _apply_window_geometry(wid, target)
    if missing:
        from .tile import place_windows_by_pid

        jobs: list[tuple[set[int], int, int, int, int, bool, bool]] = []
        for pid, target in missing:
            jobs.append(
                (
                    _descendant_pids(pid) | {pid},
                    target.x,
                    target.y,
                    target.width,
                    target.height,
                    not target.tiled,
                    (not target.tiled) or target.undecorated,
                )
            )
        if place_windows_by_pid(jobs):
            log.info("Placed %d HTML screensaver window(s) with KWin", len(jobs))
            placed = True
    return placed


def _place_window(target: ScreenTarget) -> bool:
    wid = _find_window_id(target.desktop)
    if not wid:
        return False
    kind = "tiled" if target.tiled else "fullscreen"
    log.info(
        "Placing Wine desktop %s on %s at %d,%d (%dx%d, %s)",
        target.desktop,
        target.name,
        target.x,
        target.y,
        target.width,
        target.height,
        kind,
    )
    _apply_window_geometry(wid, target)
    return True


def _apply_window_geometry(wid: str, target: ScreenTarget) -> None:
    if target.tiled:
        _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "remove,fullscreen"])
        _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"])
        if target.undecorated:
            from .tile import strip_window_decorations

            strip_window_decorations(wid)
        _run_quiet(["wmctrl", "-i", "-r", wid, "-e", f"0,{target.x},{target.y},{target.width},{target.height}"])
        _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,above"])
        _run_quiet(["xdotool", "windowmove", wid, str(target.x), str(target.y)])
        _run_quiet(["xdotool", "windowsize", wid, str(target.width), str(target.height)])
        _run_quiet(["wmctrl", "-i", "-r", wid, "-e", f"0,{target.x},{target.y},{target.width},{target.height}"])
        return
    _run_quiet(["wmctrl", "-i", "-r", wid, "-e", f"0,{target.x},{target.y},{target.width},{target.height}"])
    _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,above"])
    _run_quiet(["wmctrl", "-i", "-r", wid, "-b", "add,fullscreen"])
    _run_quiet(["xdotool", "windowmove", wid, str(target.x), str(target.y)])
    _run_quiet(["xdotool", "windowsize", wid, str(target.width), str(target.height)])


def _apply_tiling(
    targets: list[ScreenTarget],
    hide_decorations: bool = False,
    scale_content: bool = False,
) -> list[ScreenTarget]:
    from .tile import Rect, tile_rect_for_screen

    tiled: list[ScreenTarget] = []
    for target in targets:
        bounds = Rect(target.x, target.y, target.width, target.height)
        gap = tile_rect_for_screen(target.name, bounds)
        if gap is None:
            log.info("No empty space on %s for a tiled screensaver", target.name)
            continue
        extra = []
        if hide_decorations:
            extra.append("undecorated")
        if scale_content:
            extra.append("scaled")
        log.info(
            "Tiling screensaver on %s at %d,%d (%dx%d)%s",
            target.name,
            gap.x,
            gap.y,
            gap.w,
            gap.h,
            f" ({', '.join(extra)})" if extra else "",
        )
        tiled.append(
            ScreenTarget(
                name=target.name,
                x=gap.x,
                y=gap.y,
                width=max(320, gap.w),
                height=max(240, gap.h),
                desktop=target.desktop,
                tiled=True,
                undecorated=bool(hide_decorations),
                scale_content=bool(scale_content),
                content_width=max(320, target.width),
                content_height=max(240, target.height),
            )
        )
    return tiled


def _have_targets(targets: list[ScreenTarget], require_screen: bool, tile: bool, fail) -> bool:
    if targets:
        return True
    if require_screen:
        if tile:
            fail("No empty space for a tiled screensaver on the selected display.")
        else:
            fail("No available display. A fullscreen video may be using the selected screen.")
    else:
        log.info("Not starting screensaver; no free display")
    return False


def _desktop_size(target: ScreenTarget) -> tuple[int, int]:
    if target.scale_content and target.content_width > 0 and target.content_height > 0:
        return target.content_width, target.content_height
    return target.width, target.height


def _wrap_gamescope(cmd: list[str], target: ScreenTarget, env: dict[str, str]) -> list[str]:
    if not target.scale_content:
        return cmd
    gs = shutil.which("gamescope")
    if gs is None:
        return cmd
    inner_w, inner_h = _desktop_size(target)
    env["SDL_VIDEODRIVER"] = "x11"
    env["SDL_VIDEO_X11_WMCLASS"] = GAMESCOPE_WMCLASS
    env["ENABLE_GAMESCOPE_WSI"] = "0"
    env["WINE_DISABLE_FULLSCREEN_HACK"] = "1"
    return [
        gs,
        "--backend",
        "sdl",
        "-b",
        "-w",
        str(inner_w),
        "-h",
        str(inner_h),
        "-W",
        str(max(320, target.width)),
        "-H",
        str(max(240, target.height)),
        "-S",
        "fit",
        "-F",
        "linear",
        "--force-windows-fullscreen",
        "--",
        *cmd,
    ]


def _place_scaled_window(target: ScreenTarget, proc: subprocess.Popen[bytes] | None) -> bool:
    wid = ""
    if proc is not None and proc.pid:
        wid = _find_window_for_pids(_descendant_pids(proc.pid))
    if not wid:
        wid = _find_window_id(target.desktop)
    if not wid:
        wid = _find_window_by_class(GAMESCOPE_WMCLASS)
    if not wid:
        return False
    log.info(
        "Placing scaled saver on %s at %d,%d (%dx%d from %dx%d)",
        target.name,
        target.x,
        target.y,
        target.width,
        target.height,
        target.content_width or target.width,
        target.content_height or target.height,
    )
    _apply_window_geometry(wid, target)
    return True


def _find_window_by_class(klass: str) -> str:
    output = _run_capture(["wmctrl", "-lx"])
    if not output:
        return ""
    needle = klass.lower()
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        if needle in parts[2].lower():
            return parts[0]
    return ""


def _find_window_for_pids(pids: set[int]) -> str:
    if not pids:
        return ""
    output = _run_capture(["wmctrl", "-lp"])
    if not output:
        return ""
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        if pid in pids:
            return parts[0]
    return ""


def _descendant_pids(root: int) -> set[int]:
    found = {root}
    children: dict[int, list[int]] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            ppid = _ppid_of(pid)
            if ppid is None:
                continue
            children.setdefault(ppid, []).append(pid)
    except OSError:
        return found
    stack = [root]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _ppid_of(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


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
