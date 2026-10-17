"""Shareable Wine compatibility scripts for a prefix and its screensavers."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths, wineutil
from .config import (
    LAUNCH_PREFIX_EXE,
    LAUNCH_STEAM,
    AppConfig,
    ManagedPrefix,
    ScreensaverEntry,
    _coerce_keystrokes,
    save_config,
)
from .runners import SYSTEM_ID, get_runner

log = logging.getLogger(__name__)

FORMAT_NAME = "scrsaver-profile"
FORMAT_VERSION = 1
FILE_FILTER = "SCR Saver scripts (*.scrsaver.json *.json);;JSON files (*.json);;All files (*)"


@dataclass
class WineNeeds:
    winetricks: list[str] = field(default_factory=list)
    winver: str = ""
    dll_overrides: dict[str, str] = field(default_factory=dict)
    runner_hint: str = ""


@dataclass
class SaverRecipe:
    name: str = ""
    filename: str = ""
    sha256: str = ""
    arch: str = ""
    extras: list[str] = field(default_factory=list)
    notes: str = ""
    winver: str = ""
    wine_runner: str = ""
    interactive: bool = False
    enabled: bool = True
    keystrokes: list[dict] = field(default_factory=list)
    keystroke_loop: bool = False
    stop_on_input: bool = False


@dataclass
class GameRecipe:
    name: str = ""
    launch_kind: str = ""
    steam_appid: str = ""
    exe_name: str = ""
    interactive: bool = True
    keystrokes: list[dict] = field(default_factory=list)
    keystroke_loop: bool = False
    stop_on_input: bool = False


@dataclass
class ProfileScript:
    name: str = ""
    description: str = ""
    kind: str = "prefix"
    wine: WineNeeds = field(default_factory=WineNeeds)
    cpu_percent: float = 100.0
    single_core: bool = False
    screensavers: list[SaverRecipe] = field(default_factory=list)
    games: list[GameRecipe] = field(default_factory=list)


@dataclass
class ApplyResult:
    prefix_path: str = ""
    verbs_run: list[str] = field(default_factory=list)
    savers_updated: list[str] = field(default_factory=list)
    savers_missing: list[str] = field(default_factory=list)
    games_updated: list[str] = field(default_factory=list)
    games_missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_profile(path: Path) -> ProfileScript:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("This file is not an SCR Saver compatibility script.")
    marker = str(raw.get("format") or raw.get("kind") or "")
    if marker not in (FORMAT_NAME, "prefix", "game") and "screensavers" not in raw and "games" not in raw and "wine" not in raw:
        raise ValueError("This JSON file is not an SCR Saver compatibility script.")
    wine_raw = raw.get("wine") if isinstance(raw.get("wine"), dict) else {}
    overrides = wine_raw.get("dll_overrides") if isinstance(wine_raw.get("dll_overrides"), dict) else {}
    wine = WineNeeds(
        winetricks=_string_list(wine_raw.get("winetricks")),
        winver=str(wine_raw.get("winver") or ""),
        dll_overrides={str(k): str(v) for k, v in overrides.items() if k and v},
        runner_hint=str(wine_raw.get("runner_hint") or ""),
    )
    prefix_raw = raw.get("prefix") if isinstance(raw.get("prefix"), dict) else {}
    try:
        cpu = float(prefix_raw.get("cpu_percent", raw.get("cpu_percent", 100)))
    except (TypeError, ValueError):
        cpu = 100.0
    return ProfileScript(
        name=str(raw.get("name") or path.stem),
        description=str(raw.get("description") or ""),
        kind=str(raw.get("kind") or "prefix"),
        wine=wine,
        cpu_percent=max(0.3, min(100.0, cpu)),
        single_core=bool(prefix_raw.get("single_core", raw.get("single_core", False))),
        screensavers=[_saver_from_raw(item) for item in raw.get("screensavers") or [] if isinstance(item, dict)],
        games=[_game_from_raw(item) for item in raw.get("games") or [] if isinstance(item, dict)],
    )


def dump_profile(profile: ProfileScript) -> str:
    payload = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "name": profile.name,
        "description": profile.description,
        "kind": profile.kind,
        "wine": {
            "winetricks": profile.wine.winetricks,
            "winver": profile.wine.winver,
            "dll_overrides": profile.wine.dll_overrides,
            "runner_hint": profile.wine.runner_hint,
        },
        "prefix": {
            "cpu_percent": profile.cpu_percent,
            "single_core": profile.single_core,
        },
        "screensavers": [asdict(item) for item in profile.screensavers],
        "games": [asdict(item) for item in profile.games],
    }
    return json.dumps(payload, indent=2) + "\n"


def save_profile(profile: ProfileScript, path: Path) -> None:
    path.write_text(dump_profile(profile), encoding="utf-8")


def export_prefix(cfg: AppConfig, prefix_name: str) -> ProfileScript:
    key = prefix_name or "wineprefix"
    rec = _prefix_record(cfg, key)
    folder = wineutil.prefix_path_for_name(key, cfg)
    verbs = wineutil.read_winetricks_log(folder)
    savers: list[SaverRecipe] = []
    games: list[GameRecipe] = []
    winvers: list[str] = []
    runners: list[str] = []
    for entry in cfg.screensavers:
        if not _entry_uses_prefix(entry, key):
            continue
        if entry.is_steam() or entry.is_prefix_exe():
            games.append(_game_from_entry(entry))
            continue
        recipe = _saver_from_entry(entry)
        savers.append(recipe)
        if recipe.winver:
            winvers.append(recipe.winver)
        if recipe.wine_runner:
            runners.append(recipe.wine_runner)
    shared_winver = winvers[0] if winvers and all(item == winvers[0] for item in winvers) else ""
    shared_runner = runners[0] if runners and all(item == runners[0] for item in runners) else ""
    kind = "game" if games and not savers and not verbs else "prefix"
    label = "Default bottle" if key in ("", "wineprefix") else key
    bits = []
    if verbs:
        bits.append(f"{len(verbs)} Winetricks package(s)")
    if savers:
        bits.append(f"{len(savers)} screensaver setting(s)")
    if games:
        bits.append(f"{len(games)} game autoplay recording(s)")
    description = (
        f"Compatibility script for {label}"
        + (f": {', '.join(bits)}." if bits else ".")
    )
    return ProfileScript(
        name=label,
        description=description,
        kind=kind,
        wine=WineNeeds(
            winetricks=verbs,
            winver=shared_winver,
            runner_hint=(rec.runner_id if rec and rec.runner_id != SYSTEM_ID else shared_runner),
        ),
        cpu_percent=float(rec.cpu_percent) if rec else 100.0,
        single_core=bool(rec.single_core) if rec else False,
        screensavers=savers,
        games=games,
    )


def apply_profile(
    cfg: AppConfig,
    profile: ProfileScript,
    prefix_name: str,
    runner_id: str,
    progress=None,
) -> ApplyResult:
    def say(message: str) -> None:
        log.info("%s", message)
        if progress is not None:
            progress(message)

    key = prefix_name or "wineprefix"
    runner = runner_id or (profile.wine.runner_hint if get_runner(profile.wine.runner_hint) else "") or SYSTEM_ID
    if key in ("", "wineprefix"):
        runner_obj = get_runner(runner)
        if runner_obj is None:
            raise FileNotFoundError("Wine was not found.")
        session = wineutil.WineSession(runner=runner_obj, prefix=paths.wineprefix_dir())
    else:
        session = wineutil.session_for_named_prefix(key, runner)
    result = ApplyResult(prefix_path=str(session.prefix))
    say(f"Using prefix {session.prefix}")
    wineutil.setup_prefix(session)

    already = {verb.lower() for verb in wineutil.read_winetricks_log(session.prefix)}
    needed = [verb for verb in profile.wine.winetricks if verb.lower() not in already]
    skipped = [verb for verb in profile.wine.winetricks if verb.lower() in already]
    if skipped:
        say("Already installed: " + ", ".join(skipped))
    if needed:
        if shutil.which("winetricks") is None:
            result.warnings.append(
                "Winetricks is not installed, so these packages were not downloaded: "
                + ", ".join(needed)
            )
            say(result.warnings[-1])
        else:
            say("Installing: " + ", ".join(needed))
            wineutil.run_winetricks(session, needed, line_cb=say)
            result.verbs_run = list(needed)
    if profile.wine.winver:
        say(f"Windows version for this bottle: {profile.wine.winver}")
        wineutil.set_prefix_windows_version(session, profile.wine.winver)
    if profile.wine.dll_overrides:
        say("Applying DLL overrides")
        wineutil.set_dll_overrides(session, profile.wine.dll_overrides)

    rec = _ensure_prefix_record(cfg, key, runner)
    rec.cpu_percent = float(profile.cpu_percent)
    rec.single_core = bool(profile.single_core)
    if profile.wine.runner_hint and get_runner(profile.wine.runner_hint) and not rec.runner_id:
        rec.runner_id = profile.wine.runner_hint

    for recipe in profile.screensavers:
        entry = _match_saver(cfg, recipe)
        if entry is None:
            result.savers_missing.append(recipe.name or recipe.filename)
            continue
        _apply_saver_recipe(entry, recipe, key, result)
        result.savers_updated.append(entry.name)

    for recipe in profile.games:
        entry = _match_game(cfg, recipe)
        if entry is None:
            result.games_missing.append(recipe.name or recipe.exe_name or recipe.steam_appid)
            continue
        _apply_game_recipe(entry, recipe)
        result.games_updated.append(entry.name)

    save_config(cfg)
    say("Saved library settings")
    return result


def summarize_profile(profile: ProfileScript) -> str:
    lines: list[str] = []
    if profile.description:
        lines.append(profile.description)
        lines.append("")
    wine_bits: list[str] = []
    if profile.wine.winetricks:
        wine_bits.append("Winetricks (downloaded into the prefix):")
        for verb in profile.wine.winetricks:
            wine_bits.append(f"  • {verb}")
    if profile.wine.winver:
        wine_bits.append(f"Windows version: {profile.wine.winver}")
    if profile.wine.dll_overrides:
        wine_bits.append("DLL overrides:")
        for name, mode in profile.wine.dll_overrides.items():
            wine_bits.append(f"  • {name} = {mode}")
    if profile.wine.runner_hint:
        wine_bits.append(f"Suggested Wine/Proton: {profile.wine.runner_hint}")
    if profile.cpu_percent < 100 or profile.single_core:
        wine_bits.append(
            f"Clock: {profile.cpu_percent:g}% of one CPU"
            + (" (single-core)" if profile.single_core else "")
        )
    if wine_bits:
        lines.extend(wine_bits)
        lines.append("")
    if profile.screensavers:
        lines.append("Screensaver settings (applied to matching files in your library):")
        for item in profile.screensavers:
            bits = [item.name or item.filename]
            if item.winver:
                bits.append(f"winver {item.winver}")
            if item.wine_runner:
                bits.append(item.wine_runner)
            if item.interactive:
                bits.append("interactive")
            if item.notes:
                bits.append(item.notes)
            lines.append("  • " + " — ".join(bits))
        lines.append("")
    if profile.games:
        lines.append("Game autoplay (keystrokes / mouse only):")
        for item in profile.games:
            extra = f"{len(item.keystrokes)} event(s)"
            if item.keystroke_loop:
                extra += ", loop"
            if item.stop_on_input:
                extra += ", stop on input"
            lines.append(f"  • {item.name or item.exe_name or item.steam_appid} — {extra}")
        lines.append("")
    lines.append(
        "This script does not include .scr files, demo EXEs, or copyrighted game data. "
        "Import those yourself; matching library items pick up the settings above."
    )
    return "\n".join(lines).strip() + "\n"


def default_export_name(prefix_name: str) -> str:
    key = prefix_name or "wineprefix"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in key).strip("-") or "wineprefix"
    return f"{safe}.scrsaver.json"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        items.append(text)
    return items


def _saver_from_raw(item: dict) -> SaverRecipe:
    return SaverRecipe(
        name=str(item.get("name") or ""),
        filename=str(item.get("filename") or ""),
        sha256=str(item.get("sha256") or ""),
        arch=str(item.get("arch") or ""),
        extras=_string_list(item.get("extras")),
        notes=str(item.get("notes") or ""),
        winver=str(item.get("winver") or ""),
        wine_runner=str(item.get("wine_runner") or ""),
        interactive=bool(item.get("interactive", False)),
        enabled=bool(item.get("enabled", True)),
        keystrokes=_coerce_keystrokes(item.get("keystrokes")),
        keystroke_loop=bool(item.get("keystroke_loop", False)),
        stop_on_input=bool(item.get("stop_on_input", False)),
    )


def _game_from_raw(item: dict) -> GameRecipe:
    kind = str(item.get("launch_kind") or "")
    if kind not in (LAUNCH_STEAM, LAUNCH_PREFIX_EXE):
        if item.get("steam_appid"):
            kind = LAUNCH_STEAM
        elif item.get("exe_name") or item.get("exe_path"):
            kind = LAUNCH_PREFIX_EXE
    exe = str(item.get("exe_name") or "")
    if not exe and item.get("exe_path"):
        exe = Path(str(item.get("exe_path"))).name
    return GameRecipe(
        name=str(item.get("name") or ""),
        launch_kind=kind,
        steam_appid=str(item.get("steam_appid") or ""),
        exe_name=exe,
        interactive=bool(item.get("interactive", True)),
        keystrokes=_coerce_keystrokes(item.get("keystrokes")),
        keystroke_loop=bool(item.get("keystroke_loop", False)),
        stop_on_input=bool(item.get("stop_on_input", False)),
    )


def _saver_from_entry(entry: ScreensaverEntry) -> SaverRecipe:
    return SaverRecipe(
        name=entry.name,
        filename=entry.filename,
        sha256=entry.sha256,
        arch=entry.arch,
        extras=list(entry.extras),
        notes=entry.notes,
        winver=entry.winver,
        wine_runner=entry.wine_runner,
        interactive=entry.interactive,
        enabled=entry.enabled,
        keystrokes=list(entry.keystrokes),
        keystroke_loop=entry.keystroke_loop,
        stop_on_input=entry.stop_on_input,
    )


def _game_from_entry(entry: ScreensaverEntry) -> GameRecipe:
    exe = Path(entry.exe_path).name if entry.exe_path else ""
    return GameRecipe(
        name=entry.name,
        launch_kind=entry.launch_kind,
        steam_appid=entry.steam_appid,
        exe_name=exe,
        interactive=entry.interactive,
        keystrokes=list(entry.keystrokes),
        keystroke_loop=entry.keystroke_loop,
        stop_on_input=entry.stop_on_input,
    )


def _entry_uses_prefix(entry: ScreensaverEntry, prefix_name: str) -> bool:
    key = prefix_name or "wineprefix"
    current = entry.wine_prefix or "wineprefix"
    if key in ("", "wineprefix"):
        return current in ("", "wineprefix")
    return current == key


def _prefix_record(cfg: AppConfig, name: str) -> ManagedPrefix | None:
    key = name or "wineprefix"
    for item in cfg.managed_prefixes:
        if item.name == key or (key == "wineprefix" and item.name in ("", "wineprefix")):
            return item
    return None


def _ensure_prefix_record(cfg: AppConfig, name: str, runner_id: str) -> ManagedPrefix:
    rec = _prefix_record(cfg, name)
    if rec is not None:
        if runner_id and not rec.runner_id:
            rec.runner_id = runner_id
        return rec
    rec = ManagedPrefix(name=name or "wineprefix", runner_id=runner_id or SYSTEM_ID)
    cfg.managed_prefixes.append(rec)
    return rec


def _match_saver(cfg: AppConfig, recipe: SaverRecipe) -> ScreensaverEntry | None:
    if recipe.sha256:
        for entry in cfg.screensavers:
            if entry.sha256 and entry.sha256 == recipe.sha256 and not entry.is_steam() and not entry.is_prefix_exe():
                return entry
    if recipe.filename:
        wanted = recipe.filename.lower()
        for entry in cfg.screensavers:
            if entry.filename.lower() == wanted and not entry.is_steam() and not entry.is_prefix_exe():
                return entry
    if recipe.name:
        wanted = recipe.name.lower()
        for entry in cfg.screensavers:
            if entry.name.lower() == wanted and not entry.is_steam() and not entry.is_prefix_exe():
                return entry
    return None


def _match_game(cfg: AppConfig, recipe: GameRecipe) -> ScreensaverEntry | None:
    if recipe.steam_appid:
        for entry in cfg.screensavers:
            if entry.steam_appid == recipe.steam_appid:
                return entry
    if recipe.exe_name:
        wanted = recipe.exe_name.lower()
        for entry in cfg.screensavers:
            if Path(entry.exe_path).name.lower() == wanted:
                return entry
    if recipe.name:
        wanted = recipe.name.lower()
        for entry in cfg.screensavers:
            if entry.name.lower() == wanted and (entry.is_steam() or entry.is_prefix_exe()):
                return entry
    return None


def _apply_saver_recipe(entry: ScreensaverEntry, recipe: SaverRecipe, prefix_name: str, result: ApplyResult) -> None:
    entry.wine_prefix = "" if prefix_name in ("", "wineprefix") else prefix_name
    if recipe.winver:
        entry.winver = recipe.winver
    if recipe.wine_runner:
        if get_runner(recipe.wine_runner) is not None:
            entry.wine_runner = recipe.wine_runner
        else:
            result.warnings.append(
                f"{entry.name}: Wine/Proton '{recipe.wine_runner}' is not on this computer; left unchanged."
            )
    entry.interactive = bool(recipe.interactive)
    if recipe.notes:
        entry.notes = recipe.notes
    if recipe.extras:
        extra = list(entry.extras)
        for item in recipe.extras:
            if item not in extra:
                extra.append(item)
        entry.extras = extra
    if recipe.keystrokes:
        entry.keystrokes = list(recipe.keystrokes)
        entry.keystroke_loop = bool(recipe.keystroke_loop)
        entry.stop_on_input = bool(recipe.stop_on_input)


def _apply_game_recipe(entry: ScreensaverEntry, recipe: GameRecipe) -> None:
    if recipe.keystrokes:
        entry.keystrokes = list(recipe.keystrokes)
        entry.keystroke_loop = bool(recipe.keystroke_loop)
        entry.stop_on_input = bool(recipe.stop_on_input)
    entry.interactive = bool(recipe.interactive)
