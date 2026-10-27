"""Play .NET HTML screensaver wrappers (After Dark CSS packs) without Wine."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import zipfile
from pathlib import Path
from urllib.request import urlopen

from . import paths

log = logging.getLogger(__name__)

PLACEHOLDER = "[image content will be provided separately]"
SPRITE_NAME = "toaster-sprite.gif"
SPRITE_URL = (
    "https://raw.githubusercontent.com/bryanbraun/after-dark-css/gh-pages/img/"
    + SPRITE_NAME
)
ASSET_BASE = "https://raw.githubusercontent.com/bryanbraun/after-dark-css/gh-pages/img/"


def is_html_wrapper(path: Path) -> bool:
    """True when a .scr is a .NET host with an embedded zip of index.html."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if b"mscoree.dll" not in data[: min(len(data), 512 * 1024)] and b"mscoree.dll" not in data:
        if b"mscoree" not in data:
            return False
    idx = data.find(b"PK\x03\x04")
    if idx < 0:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data[idx:])) as archive:
            names = {info.filename.lower() for info in archive.infolist()}
    except zipfile.BadZipFile:
        return False
    return "index.html" in names or any(name.endswith("index.html") for name in names)


def prepare(path: Path) -> Path:
    """Extract and repair HTML content. Returns the directory that contains index.html."""
    dest = paths.data_dir() / "html" / _stem_key(path)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dest / ".source"
    digest = _sha256(path)
    current = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
    if current != digest:
        _extract_zip(path, dest)
        stamp.write_text(digest + "\n", encoding="utf-8")
    index = dest / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"No index.html inside {path.name}")
    _repair_html(index)
    return dest


def browser_cmd(
    html_dir: Path,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    desktop: str,
    kiosk: bool = True,
) -> list[str]:
    chrome = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chrome:
        raise FileNotFoundError(
            "This After Dark screensaver is an HTML page. Install Chromium to play it."
        )
    index = html_dir / "index.html"
    profile = paths.data_dir() / "chromium" / desktop
    profile.mkdir(parents=True, exist_ok=True)
    _seed_profile(profile)
    cmd = [
        chrome,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-features=Translate,MediaRouter",
        "--autoplay-policy=no-user-gesture-required",
        "--password-store=basic",
        "--ozone-platform=wayland",
        "--disable-vulkan",
        "--hide-scrollbars",
        f"--class={desktop}",
        f"--window-position={x},{y}",
        f"--window-size={width},{height}",
    ]
    if kiosk:
        cmd.append("--kiosk")
    cmd.append(f"--app={index.resolve().as_uri()}")
    return cmd


def _stem_key(path: Path) -> str:
    text = path.stem.strip() or "html"
    return "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in text)[:80].strip() or "html"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_zip(path: Path, dest: Path) -> None:
    data = path.read_bytes()
    idx = data.find(b"PK\x03\x04")
    if idx < 0:
        raise FileNotFoundError(f"{path.name} has no embedded HTML archive")
    with zipfile.ZipFile(io.BytesIO(data[idx:])) as archive:
        archive.extractall(dest)
    log.info("Extracted HTML screensaver content from %s to %s", path.name, dest)


def _seed_profile(profile: Path) -> None:
    """Keep Chromium from asking to restore a crashed session in our kiosk profile."""
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True)
    (profile / "First Run").touch()
    prefs = default / "Preferences"
    prefs.write_text(
        '{"profile":{"exit_type":"Normal","exited_cleanly":true},'
        '"browser":{"has_seen_welcome_page":true}}\n',
        encoding="utf-8",
    )


def _repair_html(index: Path) -> None:
    text = index.read_text(encoding="utf-8", errors="replace")
    changed = False
    if "cursor: none" not in text and "cursor:none" not in text:
        injected = (
            "html,body,*{cursor:none !important;user-select:none !important;}"
            "html,body{background:#111;overflow:hidden;}"
        )
        if "</style>" in text:
            text = text.replace("</style>", injected + "</style>", 1)
        else:
            text = text.replace("</head>", f"<style>{injected}</style></head>", 1)
        changed = True
    if PLACEHOLDER in text:
        _fetch_asset(index.parent / SPRITE_NAME, SPRITE_URL)
        text = text.replace(f"url('{PLACEHOLDER}')", f"url('{SPRITE_NAME}')")
        text = text.replace(f'url("{PLACEHOLDER}")', f'url("{SPRITE_NAME}")')
        changed = True
        log.info("Filled missing toaster sprite in %s", index)
    for name in re.findall(r"""url\(['"]?(?:\.\./img/)?([A-Za-z0-9._-]+\.(?:gif|png|jpg|jpeg))['"]?\)""", text):
        target = index.parent / name
        if target.is_file():
            continue
        try:
            _fetch_asset(target, ASSET_BASE + name)
            changed = True
        except Exception as exc:
            log.warning("Could not fetch %s: %s", name, exc)
        else:
            text = text.replace(f"../img/{name}", name)
            changed = True
    if changed:
        index.write_text(text, encoding="utf-8")


def _fetch_asset(dest: Path, url: str) -> None:
    if dest.is_file() and dest.stat().st_size > 0:
        return
    log.info("Downloading %s", url)
    with urlopen(url, timeout=30) as response:
        data = response.read()
    dest.write_bytes(data)
