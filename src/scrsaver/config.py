from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import paths

log = logging.getLogger(__name__)

PlayMode = Literal["random", "selected"]


LAUNCH_LIBRARY = ""
LAUNCH_STEAM = "steam"
LAUNCH_PREFIX_EXE = "prefix_exe"


@dataclass
class ScreensaverEntry:
    id: str
    name: str
    filename: str
    enabled: bool = True
    sha256: str = ""
    arch: str = ""
    extras: list[str] = field(default_factory=list)
    notes: str = ""
    winver: str = ""
    wine_runner: str = ""
    wine_prefix: str = ""
    interactive: bool = False
    launch_kind: str = ""
    steam_appid: str = ""
    exe_path: str = ""
    keystrokes: list[dict] = field(default_factory=list)
    keystroke_loop: bool = False
    stop_on_input: bool = False

    def linux_path(self) -> Path:
        return paths.library_dir() / self.filename

    def is_steam(self) -> bool:
        return self.launch_kind == LAUNCH_STEAM or bool(self.steam_appid)

    def is_prefix_exe(self) -> bool:
        return self.launch_kind == LAUNCH_PREFIX_EXE or bool(self.exe_path)

    def is_playable(self) -> bool:
        if self.is_steam():
            return bool(self.steam_appid)
        if self.launch_kind == LAUNCH_PREFIX_EXE:
            return bool(self.exe_path) and Path(self.exe_path).expanduser().is_file()
        return self.linux_path().is_file()


ORIGIN_OWNED = "owned"
ORIGIN_IMPORTED = "imported"
ORIGIN_STEAM = "steam"


@dataclass
class ManagedPrefix:
    name: str
    runner_id: str = ""
    cpu_percent: float = 100.0
    single_core: bool = False
    fps_limit: int = 0
    origin: str = ORIGIN_OWNED
    source_path: str = ""
    steam_appid: str = ""

    def is_imported(self) -> bool:
        return self.origin in (ORIGIN_IMPORTED, ORIGIN_STEAM) or bool(self.source_path)


@dataclass
class AppConfig:
    timeout_seconds: int = 300
    enabled: bool = False
    play_mode: PlayMode = "random"
    selected_id: str = ""
    display_mode: str = "all"
    autostart: bool = False
    interactive_exit_key: str = "Escape"
    mute_screensaver_audio: bool = False
    duck_percent: int = 25
    one_audio_source: bool = False
    audio_source_display: str = "primary"
    skip_fullscreen_video: bool = False
    fps_limit: int = 0
    managed_prefixes: list[ManagedPrefix] = field(default_factory=list)
    screensavers: list[ScreensaverEntry] = field(default_factory=list)

    def entry_by_id(self, sid: str) -> ScreensaverEntry | None:
        for item in self.screensavers:
            if item.id == sid:
                return item
        return None

    def enabled_entries(self) -> list[ScreensaverEntry]:
        return [item for item in self.screensavers if item.enabled and item.is_playable()]

    def pick_playable(self) -> ScreensaverEntry | None:
        enabled = self.enabled_entries()
        if not enabled:
            return None
        if self.play_mode == "selected":
            selected = self.entry_by_id(self.selected_id)
            if selected and selected in enabled:
                return selected
            if selected and selected.is_playable():
                return selected
        import random

        return random.choice(enabled)


def _coerce_cpu_percent(value: Any) -> float:
    from .cpu_limit import clamp_quota_percent

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 100.0
    return clamp_quota_percent(number)


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _coerce_keystrokes(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    events: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "key")
        if kind not in ("key", "btn", "rel"):
            kind = "key"
        try:
            t = float(item.get("t", 0))
            code = int(item.get("code", 0))
            val = int(item.get("value", 0 if kind == "rel" else 1))
        except (TypeError, ValueError):
            continue
        if kind == "key" and code in range(0x110, 0x118):
            kind = "btn"
        if kind == "key" and code <= 0:
            continue
        name = str(item.get("name") or "")
        if kind == "btn" and (not name or name.startswith("KEY_")):
            name = {
                0x110: "BTN_LEFT",
                0x111: "BTN_RIGHT",
                0x112: "BTN_MIDDLE",
                0x113: "BTN_SIDE",
                0x114: "BTN_EXTRA",
                0x115: "BTN_FORWARD",
                0x116: "BTN_BACK",
            }.get(code, f"BTN_{code}")
        events.append(
            {"t": max(0.0, t), "kind": kind, "code": code, "name": name, "value": val}
        )
    return events


def default_config() -> AppConfig:
    return AppConfig()


def load_config() -> AppConfig:
    path = paths.config_file()
    if not path.is_file():
        return default_config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read config, using defaults: %s", exc)
        return default_config()
    if not isinstance(raw, dict):
        return default_config()

    play_mode = raw.get("play_mode", "random")
    if play_mode not in ("random", "selected"):
        play_mode = "random"
    display_mode = str(raw.get("display_mode") or "all")
    if display_mode not in ("all", "primary") and not display_mode.startswith("output:"):
        display_mode = "all"

    managed: list[ManagedPrefix] = []
    for item in raw.get("managed_prefixes") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        origin = str(item.get("origin") or ORIGIN_OWNED)
        if origin not in (ORIGIN_OWNED, ORIGIN_IMPORTED, ORIGIN_STEAM):
            origin = ORIGIN_IMPORTED if item.get("source_path") else ORIGIN_OWNED
        managed.append(
            ManagedPrefix(
                name=str(item["name"]),
                runner_id=str(item.get("runner_id") or ""),
                cpu_percent=_coerce_cpu_percent(item.get("cpu_percent")),
                single_core=bool(item.get("single_core", False)),
                fps_limit=_coerce_int(item.get("fps_limit"), 0, 0, 120),
                origin=origin,
                source_path=str(item.get("source_path") or ""),
                steam_appid=str(item.get("steam_appid") or ""),
            )
        )

    entries: list[ScreensaverEntry] = []
    for item in raw.get("screensavers") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("filename"):
            continue
        launch_kind = str(item.get("launch_kind") or "")
        if launch_kind not in ("", LAUNCH_STEAM, LAUNCH_PREFIX_EXE):
            launch_kind = ""
        if not launch_kind and item.get("steam_appid"):
            launch_kind = LAUNCH_STEAM
        if not launch_kind and item.get("exe_path"):
            launch_kind = LAUNCH_PREFIX_EXE
        entries.append(
            ScreensaverEntry(
                id=str(item["id"]),
                name=str(item.get("name") or Path(str(item["filename"])).stem),
                filename=str(item["filename"]),
                enabled=bool(item.get("enabled", True)),
                sha256=str(item.get("sha256") or ""),
                arch=str(item.get("arch") or ""),
                extras=[str(extra) for extra in item.get("extras") or [] if extra],
                notes=str(item.get("notes") or ""),
                winver=str(item.get("winver") or ""),
                wine_runner=str(item.get("wine_runner") or ""),
                wine_prefix=str(item.get("wine_prefix") or ""),
                interactive=bool(item.get("interactive", False)),
                launch_kind=launch_kind,
                steam_appid=str(item.get("steam_appid") or ""),
                exe_path=str(item.get("exe_path") or ""),
                keystrokes=_coerce_keystrokes(item.get("keystrokes")),
                keystroke_loop=bool(item.get("keystroke_loop", False)),
                stop_on_input=bool(item.get("stop_on_input", False)),
            )
        )

    return AppConfig(
        timeout_seconds=_coerce_int(raw.get("timeout_seconds"), 300, 10, 86400),
        enabled=bool(raw.get("enabled", False)),
        play_mode=play_mode,
        selected_id=str(raw.get("selected_id") or ""),
        display_mode=display_mode,
        autostart=bool(raw.get("autostart", False)),
        interactive_exit_key=str(raw.get("interactive_exit_key") or "Escape"),
        mute_screensaver_audio=bool(raw.get("mute_screensaver_audio", False)),
        duck_percent=_coerce_int(raw.get("duck_percent"), 25, 0, 100),
        one_audio_source=bool(raw.get("one_audio_source", False)),
        audio_source_display=str(raw.get("audio_source_display") or "primary"),
        skip_fullscreen_video=bool(raw.get("skip_fullscreen_video", False)),
        fps_limit=_coerce_int(raw.get("fps_limit"), 0, 0, 120),
        managed_prefixes=managed,
        screensavers=entries,
    )


def save_config(cfg: AppConfig) -> None:
    path = paths.config_file()
    payload = asdict(cfg)
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix="scrsaver-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
