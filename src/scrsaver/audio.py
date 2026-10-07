from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_WINE_BINARIES = {
    "wine",
    "wine64",
    "wineserver",
    "wine-preloader",
    "wine64-preloader",
    "explorer.exe",
    "start.exe",
}


def _pactl_json(args: list[str]) -> list | dict | None:
    try:
        result = subprocess.run(
            ["pactl", "--format=json", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _is_wine_stream(properties: dict, wine_pids: set[int]) -> bool:
    try:
        pid = int(properties.get("application.process.id") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and pid in wine_pids:
        return True
    binary = str(properties.get("application.process.binary") or "").lower()
    if Path(binary).name.lower() in _WINE_BINARIES:
        return True
    name = str(properties.get("application.name") or "").lower()
    return name.startswith("wine") or "wine" in name


def _stream_audible(item: dict) -> bool:
    if item.get("corked") or item.get("mute"):
        return False
    volume = item.get("volume") or {}
    for channel in volume.values():
        if isinstance(channel, dict):
            percent = str(channel.get("value_percent") or "0").replace("%", "")
            try:
                if float(percent) > 1:
                    return True
            except ValueError:
                continue
        elif isinstance(channel, (int, float)) and channel > 0:
            return True
    return False


def ensure_silent_sink() -> str:
    name = "scrsaver-silent"
    sinks = _pactl_json(["list", "sinks"])
    if isinstance(sinks, list):
        for sink in sinks:
            props = sink.get("properties") or {}
            if sink.get("name") == name or props.get("node.name") == name:
                return name
    try:
        subprocess.run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={name}",
                "sink_properties=device.description=SCRSaverSilent",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return name


def pids_in_trees(leaders: set[int]) -> set[int]:
    if not leaders:
        return set()
    ppid_of: dict[int, int] = {}
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return set(leaders)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            rparen = stat.rfind(")")
            fields = stat[rparen + 2 :].split()
            ppid_of[pid] = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
    result = set(leaders)
    for pid in ppid_of:
        current = pid
        seen: set[int] = set()
        while current and current not in seen:
            if current in leaders:
                result.add(pid)
                break
            seen.add(current)
            current = ppid_of.get(current, 0)
    return result


def apply_screensaver_audio(
    *,
    mute: bool,
    duck_percent: int,
    wine_pids: set[int],
    muted_pids: set[int] | None = None,
) -> None:
    items = _pactl_json(["list", "sink-inputs"])
    if not isinstance(items, list):
        return
    muted_pids = muted_pids or set()
    ours: list[tuple[int, int]] = []
    others_playing = False
    for item in items:
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        props = item.get("properties") or {}
        try:
            pid = int(props.get("application.process.id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if _is_wine_stream(props, wine_pids):
            ours.append((index, pid))
            continue
        if _stream_audible(item):
            others_playing = True
    if not ours:
        return
    for index, pid in ours:
        if mute or (pid and pid in muted_pids):
            _pactl(["set-sink-input-mute", str(index), "1"])
            continue
        _pactl(["set-sink-input-mute", str(index), "0"])
        level = max(0, min(100, int(duck_percent))) if others_playing else 100
        _pactl(["set-sink-input-volume", str(index), f"{level}%"])


def _pactl(args: list[str]) -> None:
    try:
        subprocess.run(["pactl", *args], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
