from __future__ import annotations

import argparse
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtCore import QByteArray, QCoreApplication
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from . import __version__, paths
from .config import load_config, save_config
from .daemon import SaverController
from .gui import MainWindow, load_app_icon
from .library import import_any
from .pe import NotScreensaverError

SINGLETON_NAME = "scrsaver-local-socket"
log = logging.getLogger("scrsaver")


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    _setup_logging()
    log.info("SCR Saver %s starting", __version__)
    if args.command == "import":
        return _import_cli(args.files)

    QCoreApplication.setApplicationName("SCR Saver")
    QCoreApplication.setOrganizationName("scrsaver")
    QCoreApplication.setApplicationVersion(__version__)

    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())
    app.setQuitOnLastWindowClosed(False)

    server = _take_singleton()
    if server is None:
        return 0

    cfg = load_config()
    save_config(cfg)
    controller = SaverController(cfg)
    window = MainWindow(controller, show_window=not args.background)
    server.newConnection.connect(lambda: _on_second_instance(server, window))

    def _handle_signal(_signum, _frame) -> None:
        window.quit_app()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.play:
        target = args.play
        entry = cfg.entry_by_id(target)
        if entry is None:
            for item in cfg.screensavers:
                if item.filename == target or item.name == target:
                    entry = item
                    break
        if entry is None:
            log.error("No screensaver matching %s", target)
            return 1
        window.show()
        window.controller.play_entry(entry.id, manual=True)

    code = app.exec()
    controller.shutdown()
    return int(code)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Play Windows .scr screensavers when idle.")
    parser.add_argument("--background", action="store_true", help="Start in the tray without showing the window.")
    parser.add_argument("--play", metavar="ID_OR_NAME", help="Play a library screensaver immediately.")
    sub = parser.add_subparsers(dest="command")
    import_cmd = sub.add_parser("import", help="Import .scr files, .exe installers, or archives.")
    import_cmd.add_argument("files", nargs="+", type=Path)
    return parser.parse_args(argv)


def _setup_logging() -> None:
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(paths.log_dir() / "scrsaver.log", maxBytes=1_000_000, backupCount=2)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stream)


def _import_cli(files: list[Path]) -> int:
    from .config import load_config, save_config as persist

    cfg = load_config()
    status = 0
    for path in files:
        try:
            entries = import_any(cfg, path)
        except (OSError, FileExistsError, NotScreensaverError) as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            status = 1
            continue
        for entry in entries:
            extra = f" ({entry.notes})" if entry.notes else ""
            print(f"imported {entry.name} -> {entry.filename}{extra}")
    persist(cfg)
    return status


def _take_singleton() -> QLocalServer | None:
    probe = QLocalSocket()
    probe.connectToServer(SINGLETON_NAME)
    if probe.waitForConnected(200):
        probe.write(QByteArray(b"show\n"))
        probe.flush()
        probe.waitForBytesWritten(200)
        probe.disconnectFromServer()
        return None
    QLocalServer.removeServer(SINGLETON_NAME)
    server = QLocalServer()
    if not server.listen(SINGLETON_NAME):
        log.warning("Could not listen on singleton socket: %s", server.errorString())
        return server
    return server


def _on_second_instance(server: QLocalServer, window: MainWindow) -> None:
    while server.hasPendingConnections():
        connection = server.nextPendingConnection()
        if connection is None:
            continue
        connection.waitForReadyRead(200)
        window._show_from_tray()
        connection.disconnectFromServer()


if __name__ == "__main__":
    raise SystemExit(main())
