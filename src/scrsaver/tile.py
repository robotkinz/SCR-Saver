from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass

from PyQt6.QtGui import QGuiApplication

from . import paths, wineutil

log = logging.getLogger(__name__)

MIN_TILE_WIDTH = 400
MIN_TILE_HEIGHT = 300
GAP = 4
_PLUGIN = "scrsaver-tile"
_CACHE: tuple[float, list["Rect"]] = (0.0, [])
_CACHE_TTL = 0.6
_SCRIPT = """
var parts = [];
try {
    var list = workspace.windowList();
    for (var i = 0; i < list.length; i++) {
        var w = list[i];
        try { if (w.minimized) continue; } catch (e0) {}
        var isDock = false;
        try { isDock = !!w.dock; } catch (ed) {}
        try { if (w.desktopWindow) continue; } catch (e1) {}
        try { if (w.specialWindow && !isDock) continue; } catch (e2) {}
        var skip = false;
        try { if (w.splash || w.menu || w.tooltip) skip = true; } catch (e3) {}
        try { if (w.dropdownMenu || w.popupMenu || w.notification || w.criticalNotification) skip = true; } catch (e4) {}
        try { if (w.osd || w.appletPopup || w.popupWindow) skip = true; } catch (e5) {}
        if (skip && !isDock) continue;
        var cap = "";
        try { cap = String(w.caption || ""); } catch (e6) {}
        if (cap.toLowerCase().indexOf("scrsaver-") !== -1) continue;
        var g = null;
        try { g = w.frameGeometry; } catch (e7) {}
        if (!g) {
            try { g = w.geometry; } catch (e8) {}
        }
        if (!g) continue;
        var gx = 0, gy = 0, gw = 0, gh = 0;
        try {
            gx = Math.round(Number(g.x));
            gy = Math.round(Number(g.y));
            gw = Math.round(Number(g.width));
            gh = Math.round(Number(g.height));
        } catch (e9) { continue; }
        if (isDock) {
            if (gw < 8 || gh < 8) continue;
        } else if (gw < 32 || gh < 32) continue;
        var n = "";
        try { n = w.output.name; } catch (e10) {}
        parts.push([n, gx, gy, gw, gh].join(","));
    }
} catch (e) {}
print("SCRSAVER_TILE=" + parts.join("|"));
"""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def clip(self, other: "Rect") -> "Rect":
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        x2 = min(self.x2, other.x2)
        y2 = min(self.y2, other.y2)
        return Rect(x, y, x2 - x, y2 - y)


def largest_empty_rect(bounds: Rect, obstacles: list[Rect], *, min_w: int = MIN_TILE_WIDTH, min_h: int = MIN_TILE_HEIGHT) -> Rect | None:
    if bounds.w < min_w or bounds.h < min_h:
        return None
    clipped: list[Rect] = []
    for item in obstacles:
        piece = item.clip(bounds)
        if piece.w <= 0 or piece.h <= 0:
            continue
        grown = Rect(piece.x - GAP, piece.y - GAP, piece.w + GAP * 2, piece.h + GAP * 2)
        piece = grown.clip(bounds)
        if piece.w > 0 and piece.h > 0:
            clipped.append(piece)
    xs = sorted({bounds.x, bounds.x2, *[item.x for item in clipped], *[item.x2 for item in clipped]})
    best: Rect | None = None
    best_area = 0
    for i, left in enumerate(xs):
        for right in xs[i + 1 :]:
            width = right - left
            if width < min_w:
                continue
            blockers = [item for item in clipped if item.x < right and item.x2 > left]
            spans = _merge_spans([(item.y, item.y2) for item in blockers])
            cursor = bounds.y
            gaps: list[tuple[int, int]] = []
            for start, end in spans:
                if start > cursor:
                    gaps.append((cursor, start))
                cursor = max(cursor, end)
            if bounds.y2 > cursor:
                gaps.append((cursor, bounds.y2))
            for top, bottom in gaps:
                height = bottom - top
                if height < min_h:
                    continue
                area = width * height
                if area > best_area:
                    best_area = area
                    best = Rect(left, top, width, height)
    return best


def work_area(screen_name: str, fallback: Rect) -> Rect:
    app = QGuiApplication.instance()
    if app is None:
        return fallback
    for screen in app.screens():
        if (screen.name() or "") != screen_name:
            continue
        geo = screen.availableGeometry()
        return Rect(geo.x(), geo.y(), geo.width(), geo.height())
    return fallback


def visible_window_rects() -> list[Rect]:
    global _CACHE
    now = time.monotonic()
    stamp, cached = _CACHE
    if now - stamp < _CACHE_TTL:
        return list(cached)
    found = _kwin_windows()
    if found is None:
        found = _wmctrl_windows()
    _CACHE = (now, found)
    return list(found)


def tile_rect_for_screen(screen_name: str, screen_bounds: Rect) -> Rect | None:
    area = work_area(screen_name, screen_bounds)
    obstacles = visible_window_rects()
    log.info(
        "Tiling on %s work area %dx%d at %d,%d with %d window(s)",
        screen_name,
        area.w,
        area.h,
        area.x,
        area.y,
        len(obstacles),
    )
    return largest_empty_rect(area, obstacles)


_NOBORDER_AT = 0.0
_NOBORDER_PLUGIN = "scrsaver-noborder"


def strip_window_decorations(wid: str) -> None:
    """Drop KWin title bar and borders on a tiled saver window."""
    _motif_undecorate(wid)
    global _NOBORDER_AT
    now = time.monotonic()
    if now - _NOBORDER_AT < 0.4:
        return
    caption = _x11_caption(wid)
    if _kwin_noborder(caption):
        _NOBORDER_AT = now


def _motif_undecorate(wid: str) -> None:
    # MWM_HINTS_DECORATIONS = 2, decorations = 0 → no title bar or borders.
    _run(
        ["xprop", "-id", wid, "-f", "_MOTIF_WM_HINTS", "32c", "-set", "_MOTIF_WM_HINTS", "2, 0, 0, 0, 0"]
    )
    _run(
        [
            "xprop",
            "-id",
            wid,
            "-f",
            "_KDE_NET_WM_WINDOW_TYPE_OVERRIDE",
            "32a",
            "-set",
            "_KDE_NET_WM_WINDOW_TYPE_OVERRIDE",
            "_KDE_NET_WM_WINDOW_TYPE_OVERRIDE",
        ]
    )


def _x11_caption(wid: str) -> str:
    try:
        result = subprocess.run(
            ["xprop", "-id", wid, "_NET_WM_NAME", "WM_NAME"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in result.stdout.splitlines():
        if " = " not in line:
            continue
        value = line.split(" = ", 1)[1].strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if value:
            return value
    return ""


def place_windows_by_pid(
    jobs: list[tuple[set[int], int, int, int, int, bool, bool]],
) -> bool:
    """Move Wayland windows by process id.

    Each job is ``(pids, x, y, width, height, fullscreen, no_border)``.
    Used for Chromium After Dark pages, which are native Wayland windows
    and do not show up in wmctrl.
    """
    if not jobs:
        return False
    parts: list[str] = []
    for pids, x, y, width, height, fullscreen, no_border in jobs:
        pid_list = ",".join(str(int(pid)) for pid in pids if int(pid) > 0)
        if not pid_list:
            continue
        parts.append(
            "{{pids:[{pids}],x:{x},y:{y},w:{w},h:{h},fs:{fs},nb:{nb}}}".format(
                pids=pid_list,
                x=int(x),
                y=int(y),
                w=int(width),
                h=int(height),
                fs="true" if fullscreen else "false",
                nb="true" if no_border else "false",
            )
        )
    if not parts:
        return False
    script = f"""
var jobs = [{",".join(parts)}];
function screenFor(x, y) {{
    var screens = workspace.screens;
    if (!screens || !screens.length) return null;
    for (var i = 0; i < screens.length; i++) {{
        var g = screens[i].geometry;
        if (x >= g.x && x < g.x + g.width && y >= g.y && y < g.y + g.height)
            return screens[i];
    }}
    var best = screens[0];
    var bestDist = 1e15;
    for (var i = 0; i < screens.length; i++) {{
        var g = screens[i].geometry;
        var dx = (g.x + g.width / 2) - x;
        var dy = (g.y + g.height / 2) - y;
        var d = dx * dx + dy * dy;
        if (d < bestDist) {{ bestDist = d; best = screens[i]; }}
    }}
    return best;
}}
try {{
    var list = workspace.windowList();
    for (var i = 0; i < list.length; i++) {{
        var w = list[i];
        var pid = 0;
        try {{ pid = Number(w.pid); }} catch (e0) {{}}
        for (var j = 0; j < jobs.length; j++) {{
            var job = jobs[j];
            if (job.pids.indexOf(pid) === -1) continue;
            try {{ w.keepAbove = true; }} catch (e1) {{}}
            try {{ w.skipTaskbar = true; }} catch (e2) {{}}
            try {{ w.skipPager = true; }} catch (e3) {{}}
            if (job.nb) {{
                try {{ w.noBorder = true; }} catch (e4) {{}}
            }}
            var dest = screenFor(job.x + 8, job.y + 8);
            if (dest && typeof workspace.sendClientToScreen === "function") {{
                try {{ workspace.sendClientToScreen(w, dest); }} catch (e5) {{}}
            }}
            if (job.fs) {{
                try {{ w.fullScreen = true; }} catch (e6) {{}}
            }} else {{
                try {{ w.fullScreen = false; }} catch (e7) {{}}
                try {{
                    var g = w.frameGeometry;
                    g.x = job.x; g.y = job.y; g.width = job.w; g.height = job.h;
                    w.frameGeometry = g;
                }} catch (e8) {{}}
            }}
        }}
    }}
}} catch (e9) {{}}
"""
    return _run_kwin_script("scrsaver-htmlplace", script)


def _kwin_noborder(caption: str) -> bool:
    want = caption.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    script = f"""
var want = "{want}".toLowerCase();
try {{
    var list = workspace.windowList();
    for (var i = 0; i < list.length; i++) {{
        var w = list[i];
        var cap = "";
        try {{ cap = String(w.caption || "").toLowerCase(); }} catch (e0) {{}}
        var hit = cap.indexOf("scrsaver-") !== -1;
        if (!hit && want && cap.indexOf(want) !== -1) hit = true;
        if (!hit) continue;
        try {{ w.noBorder = true; }} catch (e1) {{}}
        try {{ if (w.setNoBorder) w.setNoBorder(true); }} catch (e2) {{}}
    }}
}} catch (e3) {{}}
"""
    return _run_kwin_script(_NOBORDER_PLUGIN, script)


def _run_kwin_script(plugin: str, source: str) -> bool:
    script = paths.data_dir() / f"{plugin}.js"
    try:
        script.write_text(source, encoding="utf-8")
    except OSError:
        return False
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", plugin])
    loaded = _qdbus_text(
        ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", str(script), plugin]
    )
    if loaded is None:
        return False
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.start"])
    sid = loaded.strip().splitlines()[-1] if loaded.strip() else ""
    if sid.isdigit():
        _qdbus(["org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"])
        time.sleep(0.05)
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", plugin])
    return True


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _kwin_windows() -> list[Rect] | None:
    script = paths.data_dir() / "kwin-tile.js"
    try:
        script.write_text(_SCRIPT, encoding="utf-8")
    except OSError:
        return None
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", _PLUGIN])
    loaded = _qdbus_text(
        ["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", str(script), _PLUGIN]
    )
    if loaded is None:
        return None
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.start"])
    sid = loaded.strip().splitlines()[-1] if loaded.strip() else ""
    if sid.isdigit():
        _qdbus(["org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"])
    found: list[Rect] | None = None
    for _ in range(6):
        time.sleep(0.08)
        found = _read_journal_marker()
        if found is not None:
            break
    _qdbus(["org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", _PLUGIN])
    return found


def _read_journal_marker() -> list[Rect] | None:
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-n", "80", "--since", "3 seconds ago", "-o", "cat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in reversed((result.stdout or "").splitlines()):
        if "SCRSAVER_TILE=" not in line:
            continue
        payload = line.split("SCRSAVER_TILE=", 1)[1].strip()
        return _parse_rects(payload)
    return None


def _as_int(text: str) -> int:
    return int(round(float(text)))


def _parse_rects(payload: str) -> list[Rect]:
    found: list[Rect] = []
    if not payload:
        return found
    for part in payload.split("|"):
        bits = part.split(",")
        if len(bits) < 5:
            continue
        try:
            x, y, w, h = (_as_int(bits[1]), _as_int(bits[2]), _as_int(bits[3]), _as_int(bits[4]))
        except ValueError:
            continue
        if w >= 8 and h >= 8:
            found.append(Rect(x, y, w, h))
    return found


_SKIP_CLASSES = {
    "plasmashell.plasmashell",
    "krunner.krunner",
    "kded.kded",
    "ksmserver.ksmserver",
    "xembedsniproxy.xembedsniproxy",
    "xdg-desktop-portal-kde.xdg-desktop-portal-kde",
}


def _wmctrl_windows() -> list[Rect]:
    try:
        listing = subprocess.run(
            ["wmctrl", "-lG"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        classes = subprocess.run(
            ["wmctrl", "-lx"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    class_by_id: dict[str, str] = {}
    title_by_id: dict[str, str] = {}
    for line in (classes.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        class_by_id[parts[0]] = parts[2].lower()
        title_by_id[parts[0]] = parts[4] if len(parts) > 4 else ""
    found: list[Rect] = []
    marker = wineutil.WINE_DESKTOP_PREFIX.lower()
    for line in (listing.stdout or "").splitlines():
        parts = line.split(None, 6)
        if len(parts) < 6:
            continue
        wid = parts[0]
        klass = class_by_id.get(wid, "")
        title = (title_by_id.get(wid) or (parts[6] if len(parts) > 6 else "")).lower()
        if klass in _SKIP_CLASSES or "plasmashell" in klass:
            continue
        if marker in title:
            continue
        try:
            x, y, w, h = (int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))
        except ValueError:
            continue
        if w < 48 or h < 48:
            continue
        if _window_is_ignored(wid):
            continue
        found.append(Rect(x, y, w, h))
    return found


def _window_is_ignored(wid: str) -> bool:
    try:
        result = subprocess.run(
            ["xprop", "-id", wid, "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (result.stdout or "").upper()
    is_dock = "TYPE_DOCK" in text
    if "HIDDEN" in text:
        return True
    if ("SKIP_TASKBAR" in text and "SKIP_PAGER" in text) and not is_dock:
        return True
    skip_types = (
        "TYPE_DESKTOP",
        "TYPE_TOOLBAR",
        "TYPE_MENU",
        "TYPE_NOTIFICATION",
        "TYPE_TOOLTIP",
        "TYPE_DROPDOWN_MENU",
        "TYPE_POPUP_MENU",
        "TYPE_COMBO",
        "TYPE_SPLASH",
    )
    return any(token in text for token in skip_types)


def _qdbus(args: list[str]) -> bool:
    return _qdbus_text(args) is not None


def _qdbus_text(args: list[str]) -> str | None:
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
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""
