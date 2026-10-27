"""After Dark .ad modules, played through After Dark.scr as a host."""

from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, ScreensaverEntry
from .pe import MACHINE_AMD64, MACHINE_I386, arch_name

log = logging.getLogger(__name__)

HOST_NAMES = {"after dark.scr", "afterdar.scr"}
HOST_MARKERS = (b"LoadADModule3216", b"SelectedFolder")
REG_AFTER_DARK = r"HKCU\Software\Berkeley Systems\After Dark"
REG_FOLDERS = r"HKCU\Software\Berkeley Systems\After Dark\Folders"


@dataclass
class AdModule:
    folder: str
    module: str
    path: Path
    arch: str = ""


def is_host_name(name: str) -> bool:
    return Path(name).name.lower() in HOST_NAMES


def is_host_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if is_host_name(path.name):
        return True
    try:
        data = path.read_bytes()[: 512 * 1024]
    except OSError:
        return False
    return all(marker in data for marker in HOST_MARKERS)


def is_afterdark_entry(entry: ScreensaverEntry) -> bool:
    return bool(getattr(entry, "ad_module", "") or "")


def find_root(prefix: Path) -> Path | None:
    drive = Path(prefix) / "drive_c"
    for name in ("After Dark", "AFTERDRK", "AFTER~1"):
        candidate = drive / name
        if candidate.is_dir():
            return candidate
    if not drive.is_dir():
        return None
    try:
        for child in drive.iterdir():
            if not child.is_dir():
                continue
            try:
                if any(child.rglob("*.ad")):
                    return child
            except OSError:
                continue
    except OSError:
        return None
    return None


def find_host_scr(prefix: Path) -> Path | None:
    drive = Path(prefix) / "drive_c"
    windows = drive / "windows"
    for folder in (windows / "syswow64", windows / "system32", windows):
        for name in ("After Dark.scr", "afterdar.scr", "AFTERDAR.SCR"):
            path = folder / name
            if path.is_file():
                return path
    root = find_root(prefix)
    if root is not None:
        for name in ("After Dark.scr", "afterdar.scr"):
            path = root / name
            if path.is_file():
                return path
    if drive.is_dir():
        try:
            for path in drive.rglob("*.scr"):
                if is_host_file(path):
                    return path
        except OSError:
            return None
    return None


def list_modules(prefix: Path) -> list[AdModule]:
    root = find_root(prefix)
    if root is None:
        return []
    found: list[AdModule] = []
    try:
        ads = sorted(root.rglob("*.ad"), key=lambda item: (item.parent.name.lower(), item.stem.lower()))
    except OSError:
        return []
    for ad in ads:
        if not ad.is_file():
            continue
        folder = ad.parent.name.strip()
        if not folder or ad.parent.resolve() == root.resolve():
            continue
        found.append(AdModule(folder=folder, module=ad.stem, path=ad, arch=_module_arch(ad)))
    return found


def expand_prefix(cfg: AppConfig, prefix_name: str) -> list[ScreensaverEntry]:
    """Replace After Dark.scr in the list with one row per .ad module in that prefix."""
    from . import wineutil

    key = prefix_name or "wineprefix"
    folder = wineutil.prefix_path_for_name(key, cfg)
    modules = list_modules(folder)
    if not modules:
        return []
    host = _host_entry(cfg, key)
    if host is None:
        host_path = find_host_scr(folder)
        if host_path is None:
            return []
        host = _import_host(cfg, host_path, key)
        if host is None:
            return []
    existing = {
        (item.ad_folder.lower(), item.ad_module.lower())
        for item in cfg.screensavers
        if is_afterdark_entry(item) and _same_prefix(item.wine_prefix, key)
    }
    created: list[ScreensaverEntry] = []
    insert_at = cfg.screensavers.index(host) if host in cfg.screensavers else len(cfg.screensavers)
    for module in modules:
        ident = (module.folder.lower(), module.module.lower())
        if ident in existing:
            continue
        entry = ScreensaverEntry(
            id=str(uuid.uuid4()),
            name=f"{module.module} ({module.folder})",
            filename=host.filename,
            enabled=True,
            sha256=host.sha256,
            arch=module.arch or host.arch,
            extras=list(host.extras),
            notes="",
            winver=host.winver,
            wine_runner=host.wine_runner,
            wine_prefix=host.wine_prefix or ("" if key in ("", "wineprefix") else key),
            ad_folder=module.folder,
            ad_module=module.module,
        )
        cfg.screensavers.insert(insert_at, entry)
        insert_at += 1
        created.append(entry)
        existing.add(ident)
    if created or any(
        is_afterdark_entry(item) and _same_prefix(item.wine_prefix, key) for item in cfg.screensavers
    ):
        _drop_bare_host(cfg, key, host)
    if created:
        log.info(
            "Listed %s After Dark module(s) from %s",
            len(created),
            folder,
        )
    return created


def expand_all(cfg: AppConfig) -> list[ScreensaverEntry]:
    names: set[str] = {"wineprefix"}
    for entry in cfg.screensavers:
        if is_host_name(entry.filename) or is_afterdark_entry(entry):
            names.add(entry.wine_prefix or "wineprefix")
    for rec in cfg.managed_prefixes:
        if rec.name:
            names.add(rec.name)
    created: list[ScreensaverEntry] = []
    for name in names:
        created.extend(expand_prefix(cfg, name))
    return created


def select_module(session, folder: str, module: str) -> None:
    """Point After Dark.scr at this pack folder and .ad module."""
    from . import wineutil

    if not folder or not module:
        return
    wine = wineutil.wine_binary(session)
    env = wineutil.wine_env(session)
    commands = [
        [wine, "reg", "add", REG_AFTER_DARK, "/v", "SelectedFolder", "/d", folder, "/f"],
        [wine, "reg", "add", REG_FOLDERS, "/v", folder, "/d", module, "/f"],
    ]
    for cmd in commands:
        subprocess.run(
            cmd,
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    log.info("After Dark module %s\\%s.ad", folder, module)


def config_helper() -> Path:
    from . import paths

    return paths.project_root() / "native" / "afterdark_cfg.exe"


def open_settings(session) -> None:
    """Open After Dark's real settings page (ADPage.dll), not Wine's desk.cpl."""
    from . import wineutil

    helper = config_helper()
    if not helper.is_file():
        raise FileNotFoundError(
            "After Dark settings helper is missing. From the SCR Saver source folder run: make"
        )
    dest = session.prefix / "drive_c" / "scrsaver" / "afterdark_cfg.exe"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(helper, dest)
    wine = wineutil.wine_binary(session)
    log.info("After Dark settings via %s", dest)
    wineutil.popen_wine(session, [wine, r"C:\scrsaver\afterdark_cfg.exe"])


def _module_arch(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                return ""
            (e_lfanew,) = struct.unpack_from("<I", header, 0x3C)
            handle.seek(e_lfanew)
            sig = handle.read(6)
    except OSError:
        return ""
    if len(sig) >= 2 and sig[:2] == b"NE":
        return "win16"
    if len(sig) >= 6 and sig[:4] == b"PE\x00\x00":
        (machine,) = struct.unpack_from("<H", sig, 4)
        if machine == MACHINE_I386:
            return "i386"
        if machine == MACHINE_AMD64:
            return "x64"
        return arch_name(machine)
    return ""


def _same_prefix(name: str, current: str) -> bool:
    left = (name or "").strip() or "wineprefix"
    right = (current or "").strip() or "wineprefix"
    if left in ("", "wineprefix"):
        left = "wineprefix"
    if right in ("", "wineprefix"):
        right = "wineprefix"
    return left == right


def _host_entry(cfg: AppConfig, prefix_name: str) -> ScreensaverEntry | None:
    bare: ScreensaverEntry | None = None
    any_host: ScreensaverEntry | None = None
    for entry in cfg.screensavers:
        if not _same_prefix(entry.wine_prefix, prefix_name):
            continue
        if not is_host_name(entry.filename) and not is_afterdark_entry(entry):
            continue
        if not is_afterdark_entry(entry):
            bare = entry
            break
        if any_host is None:
            any_host = entry
    return bare or any_host


def _import_host(cfg: AppConfig, source: Path, prefix_name: str) -> ScreensaverEntry | None:
    from .library import import_scr_file

    try:
        return import_scr_file(
            cfg,
            source,
            wine_prefix="" if prefix_name in ("", "wineprefix") else prefix_name,
            expand_afterdark=False,
        )
    except FileExistsError:
        return _host_entry(cfg, prefix_name)
    except Exception as exc:
        log.warning("Could not import After Dark.scr from %s: %s", source, exc)
        return None


def _drop_bare_host(cfg: AppConfig, prefix_name: str, host: ScreensaverEntry) -> None:
    if is_afterdark_entry(host):
        return
    if not any(
        is_afterdark_entry(item) and _same_prefix(item.wine_prefix, prefix_name)
        for item in cfg.screensavers
    ):
        return
    cfg.screensavers = [
        item
        for item in cfg.screensavers
        if not (
            item.id == host.id
            or (
                not is_afterdark_entry(item)
                and is_host_name(item.filename)
                and _same_prefix(item.wine_prefix, prefix_name)
            )
        )
    ]
    if cfg.selected_id == host.id:
        modules = [
            item
            for item in cfg.screensavers
            if is_afterdark_entry(item) and _same_prefix(item.wine_prefix, prefix_name)
        ]
        replacement = next(
            (
                item
                for item in modules
                if item.ad_module.lower() == "flying toasters!"
                and "4.0" in item.ad_folder
            ),
            modules[0] if modules else (cfg.screensavers[0] if cfg.screensavers else None),
        )
        cfg.selected_id = replacement.id if replacement is not None else ""
