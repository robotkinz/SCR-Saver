from __future__ import annotations

from pathlib import Path

APP_NAME = "scrsaver"
APP_DISPLAY_NAME = "SCR Saver"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def icon_path() -> Path:
    return project_root() / "data" / "scrsaver.svg"


def xdg_config_home() -> Path:
    return Path.home() / ".config"


def xdg_data_home() -> Path:
    return Path.home() / ".local" / "share"


def config_dir() -> Path:
    path = xdg_config_home() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return config_dir() / "config.json"


def data_dir() -> Path:
    path = xdg_data_home() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def library_dir() -> Path:
    path = data_dir() / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


RESERVED_PREFIX_NAMES = {"library", "logs", "prefixes"}


def wineprefix_dir() -> Path:
    return data_dir() / "wineprefix"


def sanitize_prefix_name(name: str) -> str:
    text = (name or "").strip()
    text = text.replace("/", "-").replace("\\", "-")
    while ".." in text:
        text = text.replace("..", ".")
    return text.strip(" .")


def is_under_data_dir(path: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(data_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def looks_like_wine_prefix(path: Path) -> bool:
    folder = path.expanduser()
    if (folder / "system.reg").is_file() and (folder / "drive_c").is_dir():
        return True
    nested = folder / "pfx"
    return (nested / "system.reg").is_file() and (nested / "drive_c").is_dir()


def wine_prefix_root(path: Path) -> Path:
    """Return the actual Wine prefix, unwrapping a Proton compatdata folder if needed."""
    folder = path.expanduser().resolve()
    if (folder / "system.reg").is_file():
        return folder
    nested = folder / "pfx"
    if (nested / "system.reg").is_file():
        return nested
    return folder


def resolve_named_prefix(name: str) -> Path:
    """Named prefixes live in ~/.local/share/scrsaver/<name>.

    Older installer prefixes under prefixes/ are still resolved if they exist.
    """
    cleaned = sanitize_prefix_name(name)
    if not cleaned:
        return wineprefix_dir()
    at_root = data_dir() / cleaned
    nested = prefixes_dir() / cleaned
    if at_root.exists() or not nested.exists():
        return at_root
    return nested


def next_default_prefix_name() -> str:
    existing = {path.name.lower() for path in data_dir().iterdir()} if data_dir().is_dir() else set()
    existing.update(RESERVED_PREFIX_NAMES)
    existing.add("wineprefix")
    index = 2
    while True:
        candidate = f"wineprefix{index}"
        if candidate.lower() not in existing and not (data_dir() / candidate).exists():
            return candidate
        index += 1


def prefixes_dir() -> Path:
    path = data_dir() / "prefixes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prefix_for_entry(entry_id: str) -> Path:
    path = prefixes_dir() / entry_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def autostart_dir() -> Path:
    path = xdg_config_home() / "autostart"
    path.mkdir(parents=True, exist_ok=True)
    return path


def autostart_desktop() -> Path:
    return autostart_dir() / "scrsaver.desktop"


def applications_dir() -> Path:
    path = xdg_data_home() / "applications"
    path.mkdir(parents=True, exist_ok=True)
    return path


def systemd_user_dir() -> Path:
    path = xdg_config_home() / "systemd" / "user"
    path.mkdir(parents=True, exist_ok=True)
    return path


def systemd_unit_path() -> Path:
    return systemd_user_dir() / "scrsaver.service"
