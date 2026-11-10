from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .audio import apply_screensaver_audio
from .config import AppConfig, save_config
from .fullscreen import fullscreen_outputs
from .idle import IdleMonitor
from .inhibit import IdleInhibit
from .player import GRACE_SECONDS, Player, macro_seconds

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
        self._rotating = False
        self._rotate_timer = QTimer(self)
        self._rotate_timer.setSingleShot(True)
        self._rotate_timer.timeout.connect(self._on_rotate)
        self.idle.set_timeout(cfg.timeout_seconds)
        self.idle.idle.connect(self._on_idle)
        self.idle.active.connect(self._on_active)
        self.idle.activity.connect(self._on_input)
        self.idle.escape.connect(self._on_escape)
        self.idle.error.connect(self.error)
        self.player.started.connect(self._on_started)
        self.player.stopped.connect(self._on_stopped)
        self.player.failed.connect(self._on_failed)
        self.player.macro_interrupted.connect(self._on_macro_interrupted)
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
        from . import debug as debuglog

        debuglog.apply_debug_logging(bool(self.cfg.debug_mode))
        save_config(self.cfg)
        if not self.cfg.enabled and self.player.is_playing() and not self._manual_play:
            self.player.stop()
        if self.player.is_playing() and self.cfg.rotate_savers:
            self._arm_rotate()
        else:
            self._rotate_timer.stop()
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
        extra = macro_seconds(entry.keystrokes, entry.keystroke_loop)
        log.debug(
            "Play %s id=%s prefix=%r runner=%r interactive=%s manual=%s display=%s",
            entry.name,
            entry.id,
            getattr(entry, "wine_prefix", "") or "",
            getattr(entry, "wine_runner", "") or "",
            bool(getattr(entry, "interactive", False)),
            manual,
            self.cfg.display_mode,
        )
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
            tile=bool(self.cfg.tile_into_free_space),
            hide_decorations=bool(
                self.cfg.tile_into_free_space and self.cfg.tile_hide_decorations
            ),
            scale_content=bool(
                self.cfg.tile_into_free_space and self.cfg.tile_scale_content
            ),
        )

    def stop_playback(self, background: bool = False) -> None:
        self._rotate_timer.stop()
        self._rotating = False
        self._manual_play = False
        self.player.stop(background=background)
        self.idle.ignore_for(0)
        self.idle.bump()

    def shutdown(self) -> None:
        self._rotate_timer.stop()
        self._rotating = False
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
        self._on_input()
        if not self.player.is_playing():
            self.state_changed.emit()

    def _on_input(self) -> None:
        if self.player.grace_active():
            return
        if self.player.is_playing() and self.player.is_interactive():
            return
        if self.player.is_playing():
            log.info("Input detected, stopping screensaver")
            self.stop_playback(background=True)

    def _on_macro_interrupted(self) -> None:
        # Idle input is ignored while autoplay runs; the macro's own input check is the dismiss.
        if self.player.is_playing() and not self.player.is_interactive():
            log.info("Input during autoplay, stopping screensaver")
            self.stop_playback(background=True)

    def _on_escape(self) -> None:
        # Autoplay ignores mouse/keyboard while it runs, so the exit key must stop it.
        if self.player.is_playing() and (
            self.player.is_interactive() or self.player.macro_duration() > 0
        ):
            log.info("Exit key pressed, stopping screensaver")
            self.stop_playback(background=True)

    def _on_started(self, name: str) -> None:
        log.info("Playing %s", name)
        self._inhibit.acquire()
        self._tick_audio()
        self._audio_timer.start()
        self._arm_rotate()
        self.state_changed.emit()

    def _on_stopped(self) -> None:
        self._audio_timer.stop()
        if self._rotating:
            self.state_changed.emit()
            return
        self._rotate_timer.stop()
        self._inhibit.release()
        self._manual_play = False
        self.idle.ignore_for(0)
        self.idle.bump()
        self.state_changed.emit()

    def _arm_rotate(self) -> None:
        self._rotate_timer.stop()
        if not self.cfg.rotate_savers or not self.player.is_playing():
            return
        seconds = max(60, int(self.cfg.rotate_seconds or 60))
        self._rotate_timer.start(seconds * 1000)
        log.info("Next screensaver in %s minute(s)", max(1, round(seconds / 60)))

    def _on_rotate(self) -> None:
        if not self.cfg.rotate_savers or not self.player.is_playing():
            return
        current = self.player.playing_id()
        nxt = self.cfg.pick_next_playable(current, self.cfg.rotate_mode)
        if nxt is None or nxt.id == current:
            self._arm_rotate()
            return
        log.info("Switching screensaver to %s", nxt.name)
        manual = self._manual_play
        self._rotating = True
        self.player.stop()
        self._rotating = False
        self.play_entry(sid=nxt.id, manual=manual)
        if not self.player.is_playing():
            self._inhibit.release()
            self.idle.bump()

    def _tick_audio(self) -> None:
        if not self.player.is_playing():
            return
        apply_screensaver_audio(
            mute=self.cfg.mute_screensaver_audio or int(self.cfg.fps_limit or 0) > 0,
            duck_percent=self.cfg.duck_percent,
            wine_pids=self.player.prefix_pids(),
            muted_pids=self.player.muted_audio_pids(),
        )

    def _on_failed(self, message: str) -> None:
        self._rotate_timer.stop()
        self._rotating = False
        self.idle.ignore_for(0)
        self._inhibit.release()
        self._manual_play = False
        self.error.emit(message)
        self.state_changed.emit()
