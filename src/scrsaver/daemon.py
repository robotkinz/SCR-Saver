from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .audio import apply_screensaver_audio
from .config import AppConfig, save_config
from .fullscreen import fullscreen_outputs
from .idle import IdleMonitor
from .inhibit import IdleInhibit
from .player import GRACE_SECONDS, Player
from . import wineutil

log = logging.getLogger(__name__)


class SaverController(QObject):
    state_changed = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, cfg: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.idle = IdleMonitor(self)
        self.player = Player(self)
        self._inhibit = IdleInhibit()
        self._manual_play = False
        self.idle.set_timeout(cfg.timeout_seconds)
        self.idle.idle.connect(self._on_idle)
        self.idle.active.connect(self._on_active)
        self.idle.escape.connect(self._on_escape)
        self.idle.error.connect(self.error)
        self.player.started.connect(self._on_started)
        self.player.stopped.connect(self._on_stopped)
        self.player.failed.connect(self._on_failed)
        self._audio_timer = QTimer(self)
        self._audio_timer.setInterval(1000)
        self._audio_timer.timeout.connect(self._tick_audio)

    def start_watching(self) -> None:
        self.idle.set_timeout(self.cfg.timeout_seconds)
        self.idle.set_exit_key(self.cfg.interactive_exit_key)
        self.idle.start()
        self.state_changed.emit()

    def apply_config(self) -> None:
        self.idle.set_timeout(self.cfg.timeout_seconds)
        self.idle.set_exit_key(self.cfg.interactive_exit_key)
        save_config(self.cfg)
        if not self.cfg.enabled and self.player.is_playing() and not self._manual_play:
            self.player.stop()
        self.state_changed.emit()

    def play_entry(self, sid: str | None = None, manual: bool = True) -> None:
        if sid:
            entry = self.cfg.entry_by_id(sid)
        else:
            entry = self.cfg.pick_playable()
        if entry is None:
            self.error.emit("No screensaver is available to play. Import a .scr file first.")
            return
        self._manual_play = manual
        skip: set[str] = set()
        if self.cfg.skip_fullscreen_video:
            skip = fullscreen_outputs()
            if skip:
                log.info("Skipping displays with fullscreen video: %s", ", ".join(sorted(skip)))
        extra = 0.0
        if entry is not None:
            strokes = getattr(entry, "keystrokes", None) or []
            if strokes:
                if getattr(entry, "keystroke_loop", False):
                    extra = 24 * 3600.0
                else:
                    extra = max(float(item.get("t") or 0) for item in strokes) + 2.0
        self.idle.ignore_for(GRACE_SECONDS + 0.4 + extra)
        audio_source = ""
        if self.cfg.one_audio_source and self.cfg.display_mode == "all":
            audio_source = self.cfg.audio_source_display or "primary"
        self.player.play(
            entry,
            self.cfg.display_mode,
            skip_outputs=skip,
            require_screen=manual,
            audio_source=audio_source,
        )

    def stop_playback(self) -> None:
        self._manual_play = False
        self.player.stop()
        self.idle.bump()

    def shutdown(self) -> None:
        self.idle.stop()
        self.player.stop()
        self._inhibit.release()

    def _on_idle(self) -> None:
        if not self.cfg.enabled:
            return
        if self.player.is_playing():
            return
        log.info("Idle timeout reached, starting screensaver")
        self.play_entry(manual=False)

    def _on_active(self) -> None:
        if self.player.grace_active():
            return
        if self.player.is_playing() and self.player.is_interactive():
            return
        if self.player.is_playing():
            log.info("Input detected, stopping screensaver")
            self.stop_playback()
        else:
            self.state_changed.emit()

    def _on_escape(self) -> None:
        if self.player.is_playing() and self.player.is_interactive():
            log.info("Escape pressed, stopping interactive demo")
            self.stop_playback()

    def _on_started(self, name: str) -> None:
        log.info("Playing %s", name)
        self._inhibit.acquire()
        self._tick_audio()
        self._audio_timer.start()
        self.state_changed.emit()

    def _on_stopped(self) -> None:
        self._audio_timer.stop()
        self._inhibit.release()
        self._manual_play = False
        self.idle.bump()
        self.state_changed.emit()

    def _tick_audio(self) -> None:
        if not self.player.is_playing():
            return
        pids = set(wineutil.list_prefix_pids(self.player.wine_session()))
        apply_screensaver_audio(
            mute=self.cfg.mute_screensaver_audio or int(self.cfg.fps_limit or 0) > 0,
            duck_percent=self.cfg.duck_percent,
            wine_pids=pids,
            muted_pids=self.player.muted_audio_pids(),
        )

    def _on_failed(self, message: str) -> None:
        self._inhibit.release()
        self._manual_play = False
        self.error.emit(message)
        self.state_changed.emit()
