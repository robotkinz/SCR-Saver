from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from PyQt6.QtGui import QGuiApplication

from . import paths

log = logging.getLogger(__name__)

_PLUGIN = "scrsaver-fullscreen"
_CACHE: tuple[float, set[str]] = (0.0, set())
_CACHE_TTL = 1.25
_SCRIPT = """
var names = [];
try {
    var list = workspace.windowList();
    for (var i = 0; i < list.length; i++) {
        var w = list[i];
        var isFs = false;
        try { isFs = (w.fullScreen === true); } catch (e1) {}
        if (!isFs) {
            try { isFs = (w.fullscreen === true); } catch (e2) {}
        }
        if (!isFs) continue;
        var n = "";
        try { n = w.output.name; } catch (e3) {}
        names.push(n);
    }
} catch (e) {}
print("SCRSAVER_FS=" + names.join("|"));
"""


def fullscreen_outputs() -> set[str]:
    global _CACHE
    now = time.monotonic()
    stamp, cached = _CACHE
    if now - stamp < _CACHE_TTL:
        return set(cached)
    found = _kwin_fullscreen() or _wmctrl_fullscreen()
    _CACHE = (now, found)
    return set(found)


def _kwin_fullscreen() -> set[str] | None:
    script = paths.data_dir() / "kwin-fullscreen.js"
    try:
        script.write_text(_SCRIPT, encoding="utf-8")
    except OSError:
        return None
    if not _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", str(script), _PLUGIN]):
        return None
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.start"])
    time.sleep(0.15)
    names = _read_journal_marker()
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", _PLUGIN])
    return names


def _read_journal_marker() -> set[str]:
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-n", "80", "--since", "3 seconds ago", "-o", "cat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    found: set[str] = set()
    for line in reversed((result.stdout or "").splitlines()):
        if "SCRSAVER_FS=" not in line:
            continue
        payload = line.split("SCRSAVER_FS=", 1)[1].strip()
        if payload:
            found.update(part for part in payload.split("|") if part)
        return found
    return set()


def _wmctrl_fullscreen() -> set[str]:
    try:
        listing = subprocess.run(
            ["wmctrl", "-lG"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    app = QGuiApplication.instance()
    screens = list(app.screens()) if app is not None else []
    busy: set[str] = set()
    for line in listing.stdout.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 6:
            continue
        wid = parts[0]
        try:
            x, y, w, h = (int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))
        except ValueError:
            continue
        if not _window_is_fullscreen(wid):
            continue
        for screen in screens:
            geo = screen.geometry()
            if abs(geo.x() - x) <= 16 and abs(geo.y() - y) <= 16:
                if abs(geo.width() - w) <= 32 and abs(geo.height() - h) <= 32:
                    busy.add(screen.name())
    return busy


def _window_is_fullscreen(wid: str) -> bool:
    try:
        result = subprocess.run(
            ["xprop", "-id", wid, "_NET_WM_STATE"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "FULLSCREEN" in (result.stdout or "").upper()


def _qdbus(args: list[str]) -> bool:
    binary = "qdbus6" if os.path.exists("/usr/bin/qdbus6") else "qdbus"
    try:
        result = subprocess.run(
            [binary, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
