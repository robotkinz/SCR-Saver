from __future__ import annotations

import logging
import shutil
import subprocess

from . import paths

log = logging.getLogger(__name__)

UNIT_NAME = "scrsaver.service"


def launcher_path() -> str:
    return str(paths.project_root() / "scrsaver")


def desktop_entry(*, background: bool) -> str:
    exec_line = launcher_path()
    if background:
        exec_line += " --background"
    icon = str(paths.icon_path())
    return f"""[Desktop Entry]
Type=Application
Name=SCR Saver
Comment=Play Windows .scr screensavers when idle
Exec={exec_line}
Icon={icon}
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-UsesNotifications=false
"""


def unit_text() -> str:
    exec_start = launcher_path()
    return f"""[Unit]
Description=SCR Saver
Documentation=file://{paths.project_root() / "README.md"}
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start} --background
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
KillSignal=SIGTERM
Environment=PYTHONUNBUFFERED=1
SyslogIdentifier=scrsaver

[Install]
WantedBy=graphical-session.target
"""


def systemd_available() -> bool:
    return shutil.which("systemctl") is not None


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )


def write_user_unit() -> None:
    path = paths.systemd_unit_path()
    path.write_text(unit_text(), encoding="utf-8")
    path.chmod(0o644)
    log.info("Wrote systemd user unit %s", path)


def service_is_enabled() -> bool:
    if not systemd_available():
        return paths.autostart_desktop().is_file()
    result = _systemctl("is-enabled", UNIT_NAME)
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def service_is_active() -> bool:
    if not systemd_available():
        return False
    result = _systemctl("is-active", UNIT_NAME)
    return result.returncode == 0 and result.stdout.strip() == "active"


def _remove_xdg_autostart() -> None:
    desktop = paths.autostart_desktop()
    if desktop.exists():
        desktop.unlink()


def _enable_xdg_autostart() -> None:
    path = paths.autostart_desktop()
    path.write_text(desktop_entry(background=True), encoding="utf-8")
    path.chmod(0o644)


def set_autostart(enabled: bool) -> None:
    """Enable or disable start-on-login via a systemd user service.

    Falls back to an XDG autostart desktop file if systemd is unavailable.
    Does not start or stop a currently running instance.
    """
    if enabled:
        install_application_launcher()
        if systemd_available():
            write_user_unit()
            reloaded = _systemctl("daemon-reload")
            enabled_unit = _systemctl("enable", UNIT_NAME)
            if reloaded.returncode == 0 and enabled_unit.returncode == 0:
                _remove_xdg_autostart()
                log.info("Enabled %s", UNIT_NAME)
                return
            detail = (enabled_unit.stderr or reloaded.stderr or "").strip()
            log.warning("systemd enable failed (%s); using XDG autostart", detail or "unknown error")
        _enable_xdg_autostart()
        return

    if systemd_available() and paths.systemd_unit_path().is_file():
        _systemctl("disable", UNIT_NAME)
        _systemctl("daemon-reload")
    _remove_xdg_autostart()
    log.info("Disabled start on login")


def start_service() -> None:
    if not systemd_available():
        raise RuntimeError("systemctl is not available")
    write_user_unit()
    _systemctl("daemon-reload", check=True)
    started = _systemctl("start", UNIT_NAME)
    if started.returncode != 0:
        raise RuntimeError(started.stderr.strip() or "systemctl start failed")


def install_application_launcher() -> None:
    dest = paths.applications_dir() / "scrsaver.desktop"
    dest.write_text(desktop_entry(background=False), encoding="utf-8")
    dest.chmod(0o644)
