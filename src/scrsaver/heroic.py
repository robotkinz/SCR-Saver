from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .runners import SYSTEM_ID, discover_runners

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeroicGame:
    app_id: str
    name: str
    install_path: Path
    wine_prefix: Path | None
    wine_bin: str
    wine_name: str
    wine_type: str

    def runner_id(self) -> str:
        return runner_id_for_wine(self.wine_bin, self.wine_name, self.wine_type)


def config_roots() -> list[Path]:
    return _existing_dirs(
        Path.home() / ".config/heroic",
        Path.home() / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
    )


def default_install_dir() -> Path | None:
    for root in config_roots():
        path = _default_setting(root, "defaultInstallPath")
        if path and path.is_dir():
            return path
    games = Path.home() / "Games" / "Heroic"
    return games if games.is_dir() else None


def match_exe(exe: Path) -> HeroicGame | None:
    try:
        resolved = exe.expanduser().resolve()
    except OSError:
        return None
    best: HeroicGame | None = None
    best_len = -1
    for game in list_installed_games():
        try:
            install = game.install_path.resolve()
        except OSError:
            continue
        if resolved == install or install in resolved.parents:
            length = len(str(install))
            if length > best_len:
                best = game
                best_len = length
    return best


def list_installed_games() -> list[HeroicGame]:
    games: list[HeroicGame] = []
    seen: set[str] = set()
    for root in config_roots():
        titles = _library_titles(root)
        settings = _load_json(root / "config.json")
        defaults = settings.get("defaultSettings") if isinstance(settings.get("defaultSettings"), dict) else {}
        default_prefix = Path(str(defaults.get("defaultWinePrefix") or defaults.get("winePrefix") or "")).expanduser()
        default_wine = defaults.get("wineVersion") if isinstance(defaults.get("wineVersion"), dict) else {}
        configs = _games_configs(root)
        for app_id, install_path, name_hint in _installed_entries(root):
            if app_id in seen:
                continue
            seen.add(app_id)
            cfg = configs.get(app_id) or {}
            wine = cfg.get("wineVersion") if isinstance(cfg.get("wineVersion"), dict) else default_wine
            prefix_str = str(cfg.get("winePrefix") or "")
            prefix = Path(prefix_str).expanduser() if prefix_str else None
            if prefix is None or not prefix.exists():
                guessed = _guess_prefix(default_prefix, name_hint or install_path.name)
                if guessed is not None:
                    prefix = guessed
            name = titles.get(app_id) or name_hint or install_path.name
            games.append(
                HeroicGame(
                    app_id=app_id,
                    name=_clean_title(name),
                    install_path=install_path,
                    wine_prefix=prefix if prefix and prefix.exists() else None,
                    wine_bin=str((wine or {}).get("bin") or ""),
                    wine_name=str((wine or {}).get("name") or ""),
                    wine_type=str((wine or {}).get("type") or ""),
                )
            )
    games.sort(key=lambda item: item.name.lower())
    return games


def runner_id_for_wine(bin_path: str, name: str, wine_type: str) -> str:
    wanted_bin = Path(bin_path).expanduser() if bin_path else None
    wanted_name = (name or "").strip().lower()
    kind = (wine_type or "").strip().lower()
    try:
        resolved_bin = wanted_bin.resolve() if wanted_bin and wanted_bin.exists() else wanted_bin
    except OSError:
        resolved_bin = wanted_bin
    for runner in discover_runners():
        if wanted_name and runner.name.lower() == wanted_name:
            return runner.id
        if resolved_bin is None:
            continue
        try:
            wine = runner.wine_bin.resolve()
            root = runner.root.resolve()
        except OSError:
            continue
        if resolved_bin == wine:
            return runner.id
        if resolved_bin == root or root in resolved_bin.parents:
            return runner.id
    if kind in ("wine", "") and (
        not bin_path or Path(bin_path).name == "wine"
    ):
        return SYSTEM_ID
    return SYSTEM_ID


def _installed_entries(root: Path) -> list[tuple[str, Path, str]]:
    found: list[tuple[str, Path, str]] = []
    catalogs = [
        root / "gog_store" / "installed.json",
        root / "legendaryConfig" / "legendary" / "installed.json",
        root / "nile_store" / "installed.json",
        root / "zoom_store" / "installed.json",
        root / "sideload_apps" / "library.json",
    ]
    for path in catalogs:
        data = _load_json(path)
        items: list = []
        if isinstance(data.get("installed"), list):
            items = data["installed"]
        elif isinstance(data.get("games"), list):
            items = data["games"]
        elif path.name == "installed.json" and data and all(isinstance(v, dict) for v in data.values()):
            for key, body in data.items():
                if key in ("version",) or not isinstance(body, dict):
                    continue
                row = dict(body)
                row.setdefault("appName", key)
                items.append(row)
        for item in items:
            if not isinstance(item, dict):
                continue
            app_id = str(item.get("appName") or item.get("app_name") or item.get("app_id") or "")
            install = item.get("install_path") or item.get("installPath") or ""
            if isinstance(item.get("install"), dict) and not install:
                install = item["install"].get("install_path") or item["install"].get("installPath") or ""
            if not app_id or not install:
                continue
            folder = Path(str(install)).expanduser()
            if not folder.exists():
                continue
            found.append((app_id, folder, str(item.get("title") or item.get("folder_name") or folder.name)))
    return found


def _games_configs(root: Path) -> dict[str, dict]:
    folder = root / "GamesConfig"
    result: dict[str, dict] = {}
    if not folder.is_dir():
        return result
    try:
        files = list(folder.glob("*.json"))
    except OSError:
        return result
    for path in files:
        data = _load_json(path)
        for key, body in data.items():
            if key in ("version", "explicit") or not isinstance(body, dict):
                continue
            result[str(key)] = body
    return result


def _library_titles(root: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    cache = root / "store_cache"
    files = [
        cache / "gog_library.json",
        cache / "legendary_library.json",
        cache / "nile_library.json",
        cache / "zoom_library.json",
        cache / "sideload_library.json",
    ]
    for path in files:
        data = _load_json(path)
        games = data.get("games")
        if not isinstance(games, list):
            games = data.get("library")
        if not isinstance(games, list):
            continue
        for item in games:
            if not isinstance(item, dict):
                continue
            app_id = str(item.get("app_name") or item.get("appName") or "")
            title = str(item.get("title") or "")
            if app_id and title:
                titles[app_id] = title
    return titles


def _guess_prefix(default_prefix: Path, name: str) -> Path | None:
    if not default_prefix.is_dir() or not name:
        return None
    wanted = _norm(name)
    try:
        children = [child for child in default_prefix.iterdir() if child.is_dir()]
    except OSError:
        return None
    for child in children:
        if _norm(child.name) == wanted:
            return child
    return None


def _default_setting(root: Path, key: str) -> Path | None:
    data = _load_json(root / "config.json")
    settings = data.get("defaultSettings")
    if not isinstance(settings, dict):
        return None
    value = str(settings.get(key) or "")
    return Path(value).expanduser() if value else None


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean_title(name: str) -> str:
    text = (name or "").replace("™", "").replace("®", "").strip()
    return text or name


def _norm(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def _existing_dirs(*candidates: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        found.append(resolved)
    return found
