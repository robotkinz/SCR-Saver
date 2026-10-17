from __future__ import annotations

import faulthandler
import json
import logging
import os
import platform
import signal
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, paths

log = logging.getLogger(__name__)

MAX_CRASH_FILES = 25
_SESSION_NAME = "session.json"
_FAULT_NAME = "faulthandler.log"
_SEEN_NAME = "seen.txt"
_fault_file = None
_qt_handler_ref = None
_shutdown_watch_ref = None
_installed = False
_clean_exit = False

_orig_excepthook = sys.excepthook
_orig_thread_hook = getattr(threading, "excepthook", None)
_orig_unraisable = getattr(sys, "unraisablehook", None)


def crash_dir() -> Path:
    return paths.crash_dir()


def harvest_previous_crash() -> Path | None:
    """Turn an unclean previous run into a timestamped report. Call before install."""
    folder = crash_dir()
    session = _read_session()
    fault_text = _read_text(folder / _FAULT_NAME)
    pid = int(session.get("pid") or 0) if session else 0
    if pid and _pid_is_scrsaver(pid):
        return None
    skip = _harvest_skip_reason(session, fault_text)
    if skip:
        if skip != "no previous session":
            log.info("Not treating the previous session as a crash (%s)", skip)
        _clear_session()
        _truncate_fault_file()
        return None
    if session and session.get("reported"):
        _clear_session()
        _truncate_fault_file()
        return None
    started = str(session.get("started") or "") if session else ""
    chunks = [
        "Previous SCR Saver session did not exit cleanly.",
        f"Previous PID: {pid or 'unknown'}",
        f"Previous start: {started or 'unknown'}",
        f"Previous version: {session.get('version') if session else 'unknown'}",
    ]
    if fault_text.strip():
        chunks.append("")
        chunks.append("Fatal-signal traceback (faulthandler):")
        chunks.append(fault_text.strip())
    else:
        chunks.append("")
        chunks.append(
            "No Python traceback was captured. This is typical of a native crash "
            "(segmentation fault), an OOM kill, or a SIGKILL. "
            "On systemd: journalctl --user -u scrsaver.service -b"
        )
    report = write_crash_report("unclean-exit", "\n".join(chunks))
    _clear_session()
    _truncate_fault_file()
    _notify_user(report)
    return report


def install_crash_handlers() -> None:
    global _installed, _clean_exit, _fault_file
    if _installed:
        return
    _installed = True
    _clean_exit = False
    folder = crash_dir()
    try:
        _fault_file = open(folder / _FAULT_NAME, "w", encoding="utf-8")
        faulthandler.enable(_fault_file, all_threads=True)
    except OSError as exc:
        log.warning("Could not enable faulthandler: %s", exc)
    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook
    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = _unraisable_hook
    _write_session()
    try:
        from PyQt6.QtCore import qInstallMessageHandler

        global _qt_handler_ref
        _qt_handler_ref = _qt_message
        qInstallMessageHandler(_qt_handler_ref)
    except Exception:
        pass
    try:
        _install_session_end_watchers()
    except Exception as exc:
        log.warning("Could not watch for system shutdown: %s", exc)
    log.info("Crash reports go in %s", folder)


def mark_clean_exit() -> None:
    global _clean_exit
    _clean_exit = True
    _clear_session()
    try:
        faulthandler.disable()
    except Exception:
        pass


def write_crash_report(kind: str, body: str, *, exc: BaseException | None = None) -> Path | None:
    folder = crash_dir()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = folder / f"crash-{stamp}.txt"
    index = 1
    while path.exists():
        path = folder / f"crash-{stamp}-{index}.txt"
        index += 1
    try:
        path.write_text(_format_report(kind, body, exc), encoding="utf-8")
    except OSError as err:
        log.error("Could not write crash report: %s", err)
        return None
    _remember_unread(path)
    _prune_old_reports()
    log.error("Crash report written to %s", path)
    return path


def unread_reports() -> list[Path]:
    folder = crash_dir()
    seen = _seen_names()
    reports = sorted(folder.glob("crash-*.txt"), key=lambda item: item.stat().st_mtime)
    return [path for path in reports if path.name not in seen]


def mark_reports_seen(reports: list[Path]) -> None:
    if not reports:
        return
    seen = _seen_names()
    for path in reports:
        seen.add(path.name)
    try:
        (crash_dir() / _SEEN_NAME).write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
    except OSError:
        pass


def prompt_pending_crashes(parent=None) -> None:
    reports = unread_reports()
    if not reports:
        return
    newest = reports[-1]
    folder = crash_dir()
    extra = ""
    if len(reports) > 1:
        extra = f"\nThere are {len(reports) - 1} older unread report(s) in the same folder."
    try:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtWidgets import QMessageBox, QSystemTrayIcon
    except Exception:
        _notify_user(newest)
        return
    tray = getattr(parent, "tray", None)
    if isinstance(tray, QSystemTrayIcon):
        try:
            tray.showMessage(
                "SCR Saver crash report",
                f"A crash report was saved to {newest.name}",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )
        except Exception:
            pass
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("SCR Saver crash report")
    try:
        from PyQt6.QtCore import Qt

        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    except Exception:
        pass
    box.setText("SCR Saver closed unexpectedly.")
    box.setInformativeText(
        "A crash report was saved to:\n"
        f"{newest}\n"
        f"{extra}\n\n"
        f"Folder:\n{folder}\n\n"
        "You can open that folder and share the report if you want help troubleshooting."
    )
    open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("OK", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(open_btn)
    box.exec()
    if box.clickedButton() is open_btn:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    mark_reports_seen(reports)


def _excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    report = write_crash_report("exception", text, exc=exc if isinstance(exc, BaseException) else None)
    _notify_user(report)
    if _orig_excepthook:
        _orig_excepthook(exc_type, exc, tb)


def _thread_excepthook(args) -> None:
    exc_type = getattr(args, "exc_type", None)
    exc = getattr(args, "exc_value", None)
    tb = getattr(args, "exc_traceback", None)
    name = getattr(getattr(args, "thread", None), "name", "thread")
    text = f"Thread: {name}\n" + "".join(traceback.format_exception(exc_type, exc, tb))
    report = write_crash_report("thread-exception", text, exc=exc if isinstance(exc, BaseException) else None)
    _notify_user(report)
    if _orig_thread_hook:
        _orig_thread_hook(args)


def _unraisable_hook(unraisable) -> None:
    exc = getattr(unraisable, "exc_value", None)
    tb = getattr(unraisable, "exc_traceback", None)
    err_type = getattr(unraisable, "exc_type", type(exc) if exc else Exception)
    where = getattr(unraisable, "object", None)
    text = f"Unraisable in {where!r}\n" + "".join(traceback.format_exception(err_type, exc, tb))
    write_crash_report("unraisable", text, exc=exc if isinstance(exc, BaseException) else None)
    if _orig_unraisable:
        _orig_unraisable(unraisable)


def _qt_message(mode, context, message) -> None:
    try:
        from PyQt6.QtCore import QtMsgType
    except Exception:
        return
    text = str(message or "")
    if mode == QtMsgType.QtFatalMsg:
        write_crash_report("qt-fatal", text)
        _notify_user(None)
    elif mode == QtMsgType.QtCriticalMsg:
        log.error("Qt: %s", text)
    elif mode == QtMsgType.QtWarningMsg:
        log.warning("Qt: %s", text)


def _format_report(kind: str, body: str, exc: BaseException | None) -> str:
    now = datetime.now().astimezone()
    lines = [
        "SCR Saver crash report",
        "======================",
        f"Time: {now.isoformat(timespec='seconds')}",
        f"Kind: {kind}",
        f"Version: {__version__}",
        f"PID: {os.getpid()}",
        f"Executable: {sys.executable}",
        f"Argv: {sys.argv!r}",
        f"Python: {sys.version.replace(chr(10), ' ')}",
        f"Platform: {platform.platform()}",
        f"Session type: {os.environ.get('XDG_SESSION_TYPE', '')}",
        f"Wayland display: {os.environ.get('WAYLAND_DISPLAY', '')}",
        f"X11 display: {os.environ.get('DISPLAY', '')}",
        f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '')}",
        "",
        "Details",
        "-------",
        (body or "").rstrip(),
        "",
    ]
    if exc is not None:
        lines.append("Exception repr")
        lines.append("--------------")
        lines.append(repr(exc))
        lines.append("")
    lines.append("Recent application log")
    lines.append("----------------------")
    lines.append(_tail_log(paths.log_dir() / "scrsaver.log", 180))
    lines.append("")
    lines.append("Recent service journal")
    lines.append("----------------------")
    lines.append(_journal_tail())
    lines.append("")
    return "\n".join(lines) + "\n"


def _tail_log(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log file)"
    lines = text.splitlines()
    if not lines:
        return "(empty)"
    return "\n".join(lines[-limit:])


def _journal_tail() -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", "scrsaver.service", "-n", "40", "-o", "short-iso", "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "(journalctl unavailable)"
    return (result.stdout or result.stderr or "(empty)").strip() or "(empty)"


def _notify_user(report: Path | None) -> None:
    folder = crash_dir()
    body = f"A crash report was saved in {folder}"
    if report is not None:
        body = f"A crash report was saved to {report}"
    try:
        import subprocess

        subprocess.Popen(
            ["notify-send", "--app-name=SCR Saver", "--urgency=critical", "SCR Saver crashed", body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _write_session() -> None:
    payload = {
        "pid": os.getpid(),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __version__,
        "boot_id": _current_boot_id(),
        "reported": False,
    }
    try:
        (crash_dir() / _SESSION_NAME).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _read_session() -> dict:
    path = crash_dir() / _SESSION_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _clear_session() -> None:
    try:
        (crash_dir() / _SESSION_NAME).unlink()
    except OSError:
        pass


def _truncate_fault_file() -> None:
    path = crash_dir() / _FAULT_NAME
    try:
        path.write_text("", encoding="utf-8")
    except OSError:
        pass


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _harvest_skip_reason(session: dict, fault_text: str) -> str:
    """Why leftover crash state should not be shown as a crash, or empty to keep it."""
    fault = (fault_text or "").strip()
    if not session and not fault:
        return "no previous session"
    if not session:
        # mark_clean_exit already ran (logout/reboot SIGTERM), then the tray
        # often SIGSEGVs as the session bus dies. That leftover dump is not a crash.
        return "clean stop leftover"
    if _session_from_previous_boot(session):
        return "machine rebooted"
    if _is_teardown_dump(fault):
        return "session teardown"
    return ""


def _is_teardown_dump(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    markers = (
        "libkf6statusnotifieritem",
        "statusnotifieritem",
        "qdbusservicewatcher",
        "sni unavailable",
    )
    return any(marker in lowered for marker in markers)


def _session_from_previous_boot(session: dict) -> bool:
    prev_boot = str(session.get("boot_id") or "")
    current = _current_boot_id()
    if prev_boot and current and prev_boot != current:
        return True
    started = str(session.get("started") or "")
    if not started:
        return False
    try:
        started_ts = datetime.fromisoformat(started).timestamp()
    except ValueError:
        return False
    btime = _system_boot_time()
    return bool(btime) and started_ts < (btime - 2)


def _current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _system_boot_time() -> float:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def _install_session_end_watchers() -> None:
    global _shutdown_watch_ref
    try:
        from PyQt6.QtCore import QObject, pyqtSlot
        from PyQt6.QtDBus import QDBusConnection
        from PyQt6.QtWidgets import QApplication
    except Exception:
        return

    class _ShutdownWatch(QObject):
        @pyqtSlot(bool)
        def on_prepare_for_shutdown(self, active: bool) -> None:
            if not active:
                if _installed and not _clean_exit:
                    try:
                        if _fault_file is not None:
                            faulthandler.enable(_fault_file, all_threads=True)
                    except Exception:
                        pass
                    _write_session()
                return
            log.info("System is shutting down; treating this as a clean stop")
            mark_clean_exit()
            app = QApplication.instance()
            if app is not None:
                app.quit()

    watch = _ShutdownWatch()
    bus = QDBusConnection.systemBus()
    if not bus.isConnected():
        return
    connected = bus.connect(
        "org.freedesktop.login1",
        "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager",
        "PrepareForShutdown",
        "b",
        watch.on_prepare_for_shutdown,
    )
    if not connected:
        log.debug("Could not subscribe to PrepareForShutdown")
        return
    _shutdown_watch_ref = watch
    log.info("Watching for system shutdown so a reboot is not treated as a crash")


def _pid_is_scrsaver(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "scrsaver" in cmdline.lower()


def _seen_names() -> set[str]:
    try:
        text = (crash_dir() / _SEEN_NAME).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _remember_unread(path: Path) -> None:
    # New reports are unread until prompt_pending_crashes marks them seen.
    _ = path


def _prune_old_reports() -> None:
    reports = sorted(crash_dir().glob("crash-*.txt"), key=lambda item: item.stat().st_mtime)
    extra = reports[:-MAX_CRASH_FILES]
    if not extra:
        return
    seen = _seen_names()
    for path in extra:
        try:
            path.unlink()
        except OSError:
            continue
        seen.discard(path.name)
    try:
        (crash_dir() / _SEEN_NAME).write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
    except OSError:
        pass


# Keep signal module imported so faulthandler and future hooks can use it.
_ = signal.SIGTERM
