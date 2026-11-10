from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

WINE_DEBUG_OFF = "-all"
WINE_DEBUG_ON = "+timestamp,+pid,+debugstr,err+all,warn+all,fixme+x11drv,fixme+winex11,fixme+win,fixme+explorer"

_MAX_RECENT = 800
_WINE_LOG_BYTES = 2_000_000

_enabled = False
_recent: deque[str] = deque(maxlen=_MAX_RECENT)
_emitter: Any = None
_qt_handler: logging.Handler | None = None
_live_dialog: Any = None
_wine_file: Any = None
_wine_read_fd: int | None = None
_wine_write_fd: int | None = None
_wine_thread: threading.Thread | None = None
_wine_lock = threading.Lock()


def is_enabled() -> bool:
    return _enabled


def wine_debug_env() -> str:
    return WINE_DEBUG_ON if _enabled else WINE_DEBUG_OFF


def wine_log_path() -> Path:
    return paths.log_dir() / "wine.log"


def recent_lines() -> list[str]:
    return list(_recent)


def apply_debug_logging(enabled: bool) -> None:
    """Raise or lower the process log level. Safe to call more than once."""
    global _enabled
    wanted = bool(enabled)
    was = _enabled
    _enabled = wanted
    logging.getLogger().setLevel(logging.DEBUG if wanted else logging.INFO)
    if wanted and not was:
        try:
            start_wine_capture()
        except OSError as exc:
            log.warning("Could not capture Wine output: %s", exc)
        logging.getLogger("scrsaver").info("Debug mode on. Watching %s", paths.log_dir())
    elif was and not wanted:
        logging.getLogger("scrsaver").info("Debug mode off")


def start_wine_capture() -> None:
    """Start a reader thread that copies Wine stderr into wine.log and the live log."""
    global _wine_file, _wine_read_fd, _wine_write_fd, _wine_thread
    with _wine_lock:
        if _wine_write_fd is not None:
            return
        folder = paths.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = wine_log_path()
        if target.is_file() and target.stat().st_size > _WINE_LOG_BYTES:
            backup = folder / "wine.log.1"
            try:
                backup.unlink(missing_ok=True)
                target.replace(backup)
            except OSError:
                pass
        _wine_file = open(target, "a", encoding="utf-8", errors="replace", buffering=1)
        _wine_read_fd, _wine_write_fd = os.pipe()
        os.set_inheritable(_wine_write_fd, True)
        _wine_thread = threading.Thread(target=_wine_reader, name="scrsaver-wine-log", daemon=True)
        _wine_thread.start()


def wine_popen_streams() -> dict[str, Any]:
    """stderr kwargs for Wine when debug mode is on."""
    if not _enabled:
        return {}
    try:
        start_wine_capture()
    except OSError as exc:
        log.warning("Could not capture Wine output: %s", exc)
        return {}
    if _wine_write_fd is None:
        return {}
    return {"stderr": _wine_write_fd}


def install_qt_handler() -> None:
    """Attach a Qt-safe handler so the live log window can show new lines."""
    global _emitter, _qt_handler
    if _qt_handler is not None:
        return
    from PyQt6.QtCore import QObject, pyqtSignal

    class LogEmitter(QObject):
        message = pyqtSignal(str)

    _emitter = LogEmitter()
    handler = _QtLogHandler()
    handler.setFormatter(_LiveFormatter())
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    _qt_handler = handler


def show_live_log(parent=None) -> None:
    global _live_dialog
    install_qt_handler()
    if _live_dialog is None:
        _live_dialog = _make_live_dialog(parent)
        _live_dialog.finished.connect(_on_live_closed)
    _live_dialog.show()
    _live_dialog.raise_()
    _live_dialog.activateWindow()


def open_log_folder() -> None:
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    folder = paths.log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _on_live_closed() -> None:
    global _live_dialog
    dialog = _live_dialog
    _live_dialog = None
    if dialog is not None and _emitter is not None:
        try:
            _emitter.message.disconnect(dialog.append_line)
        except TypeError:
            pass


def _wine_reader() -> None:
    wine_log = logging.getLogger("scrsaver.wine")
    read_fd = _wine_read_fd
    if read_fd is None:
        return
    with os.fdopen(read_fd, "r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            text = line.rstrip("\r\n")
            if not text:
                continue
            handle = _wine_file
            if handle is not None:
                try:
                    handle.write(text + "\n")
                    handle.flush()
                except OSError:
                    pass
            wine_log.info("%s", text)


def _tail_file(path: Path, limit: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    if len(lines) <= limit:
        return lines
    return lines[-limit:]


class _LiveFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        stamp = time.strftime("%H:%M:%S", self.converter(record.created))
        return f"{stamp}.{int(record.msecs):03d}"

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith("scrsaver."):
            name = name[9:]
        record = logging.makeLogRecord(record.__dict__)
        record.name = name
        return super().format(record)


class _QtLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        _recent.append(msg)
        emitter = _emitter
        if emitter is not None:
            emitter.message.emit(msg)


def _make_live_dialog(parent):
    from PyQt6.QtGui import QFont, QTextCursor
    from PyQt6.QtWidgets import (
        QDialog,
        QHBoxLayout,
        QLabel,
        QPlainTextEdit,
        QPushButton,
        QVBoxLayout,
    )

    class LiveLogDialog(QDialog):
        def __init__(self) -> None:
            super().__init__(parent)
            self.setWindowTitle("SCR Saver — live log")
            self.setMinimumSize(760, 440)
            self.resize(860, 520)
            layout = QVBoxLayout(self)
            hint = QLabel(
                "New lines appear as SCR Saver and Wine run. "
                "Leave this open on one display and use Test now on the other."
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.view = QPlainTextEdit()
            self.view.setReadOnly(True)
            self.view.setMaximumBlockCount(4000)
            self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            font = QFont("monospace")
            font.setStyleHint(QFont.StyleHint.TypeWriter)
            font.setPointSize(10)
            self.view.setFont(font)
            layout.addWidget(self.view, 1)
            buttons = QHBoxLayout()
            clear_btn = QPushButton("Clear")
            clear_btn.clicked.connect(self.view.clear)
            copy_btn = QPushButton("Copy")
            copy_btn.clicked.connect(self._copy)
            folder_btn = QPushButton("Open log folder")
            folder_btn.clicked.connect(open_log_folder)
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.accept)
            buttons.addWidget(clear_btn)
            buttons.addWidget(copy_btn)
            buttons.addWidget(folder_btn)
            buttons.addStretch(1)
            buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            self._seed()
            if _emitter is not None:
                _emitter.message.connect(self.append_line)

        def _seed(self) -> None:
            lines = recent_lines()
            if not lines:
                lines = _tail_file(paths.log_dir() / "scrsaver.log", 120)
                wine_lines = _tail_file(wine_log_path(), 40)
                if wine_lines:
                    lines.extend(["", "--- wine.log ---", *wine_lines])
            if lines:
                self.view.setPlainText("\n".join(lines))
                self.view.moveCursor(QTextCursor.MoveOperation.End)

        def append_line(self, text: str) -> None:
            bar = self.view.verticalScrollBar()
            follow = bar.value() >= bar.maximum() - 8
            self.view.appendPlainText(text)
            if follow:
                bar.setValue(bar.maximum())

        def _copy(self) -> None:
            self.view.selectAll()
            self.view.copy()
            cursor = self.view.textCursor()
            cursor.clearSelection()
            self.view.setTextCursor(cursor)

    return LiveLogDialog()
