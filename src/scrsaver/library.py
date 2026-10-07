from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from . import paths
from .config import AppConfig, ScreensaverEntry
from .extract import ARCHIVE_SUFFIXES, find_screensaver_files, unpack_container
from .pe import (
    NotScreensaverError,
    file_sha256,
    friendly_name,
    is_wildlife_stub,
    looks_like_installer,
    suggested_winver,
    validate_scr,
)

log = logging.getLogger(__name__)

WILDLIFE_NOTE = (
    "Plus! 98 Wildlife screensaver. It needs WL32DLL.DLL, WILDLB32.DLL, and the "
    "matching theme DLL (for example Mystery.dll) next to the .scr."
)

KNOWN_ENGINE_DLLS = {
    "wl32dll.dll",
    "wl16dll.dll",
    "wildlb32.dll",
}


def unique_filename(original: str) -> str:
    name = Path(original).name
    dest = paths.library_dir() / name
    if not dest.exists():
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while True:
        candidate = f"{stem}-{index}{suffix}"
        if not (paths.library_dir() / candidate).exists():
            return candidate
        index += 1


def _already_imported(cfg: AppConfig, digest: str, filename: str) -> ScreensaverEntry | None:
    stem = Path(filename).stem.lower()
    for existing in cfg.screensavers:
        if existing.sha256 == digest and Path(existing.filename).stem.lower() == stem:
            return existing
    return None


def _copy_into_library(source: Path) -> str:
    digest = file_sha256(source)
    for existing in paths.library_dir().iterdir():
        if not existing.is_file():
            continue
        if existing.name.lower() == source.name.lower():
            try:
                if file_sha256(existing) == digest:
                    return existing.name
            except OSError:
                pass
    filename = unique_filename(source.name)
    shutil.copy2(source, paths.library_dir() / filename)
    return filename


def companion_files(scr_path: Path) -> list[Path]:
    folder = scr_path.parent
    if not folder.is_dir():
        return []
    stem = scr_path.stem.lower()
    found: list[Path] = []
    for child in folder.iterdir():
        if not child.is_file() or child.resolve() == scr_path.resolve():
            continue
        name = child.name.lower()
        if child.stem.lower() == stem or name in KNOWN_ENGINE_DLLS:
            found.append(child)
    return found


def import_scr_file(
    cfg: AppConfig,
    source: Path,
    *,
    extras: list[Path] | None = None,
    wine_runner: str = "",
    wine_prefix: str = "",
) -> ScreensaverEntry:
    source = source.expanduser().resolve()
    machine, arch = validate_scr(source)
    digest = file_sha256(source)
    duplicate = _already_imported(cfg, digest, source.name)
    if duplicate is not None:
        raise FileExistsError(f'"{duplicate.name}" is already in the library.')
    filename = unique_filename(source.name)
    dest = paths.library_dir() / filename
    shutil.copy2(source, dest)

    extra_names: list[str] = []
    for extra in extras if extras is not None else companion_files(source):
        try:
            extra_names.append(_copy_into_library(extra))
        except OSError as exc:
            log.warning("Could not copy companion %s: %s", extra, exc)

    notes = ""
    wildlife = is_wildlife_stub(dest)
    if wildlife:
        has_engine = any(Path(name).name.lower() in KNOWN_ENGINE_DLLS for name in extra_names)
        if not has_engine:
            notes = WILDLIFE_NOTE

    entry = ScreensaverEntry(
        id=str(uuid.uuid4()),
        name=friendly_name(source),
        filename=filename,
        enabled=True,
        sha256=digest,
        arch=arch,
        extras=extra_names,
        notes=notes,
        winver=suggested_winver(dest),
        wine_runner=wine_runner,
        wine_prefix=wine_prefix,
    )
    cfg.screensavers.append(entry)
    if not cfg.selected_id:
        cfg.selected_id = entry.id
    log.info("Imported %s (%s) as %s", source, arch, dest)
    del machine
    return entry


def import_any(cfg: AppConfig, source: Path) -> list[ScreensaverEntry]:
    source = source.expanduser().resolve()
    if source.is_dir():
        return _import_tree(cfg, source)
    suffix = source.suffix.lower()
    if suffix == ".scr":
        return [import_scr_file(cfg, source)]
    if suffix in ARCHIVE_SUFFIXES or (suffix == ".exe" and looks_like_installer(source)):
        return _import_container(cfg, source)
    if suffix == ".exe":
        return [import_scr_file(cfg, source)]
    raise NotScreensaverError("Not a .scr, .exe, or archive of screensavers.")


def _import_container(cfg: AppConfig, source: Path) -> list[ScreensaverEntry]:
    tmp = Path(tempfile.mkdtemp(prefix="scrsaver-extract-"))
    try:
        unpack_container(source, tmp)
        files = find_screensaver_files(tmp)
        if not files and source.suffix.lower() == ".exe":
            # Not an unpackable installer — treat the exe itself as the saver.
            if not looks_like_installer(source):
                return [import_scr_file(cfg, source)]
            raise NotScreensaverError(
                "This looks like an installer, but no .scr file was found inside. "
                "InstallShield extract needs the bundled unshield helper."
            )
        if not files:
            raise NotScreensaverError("No .scr files were found inside this archive.")
        imported: list[ScreensaverEntry] = []
        errors: list[str] = []
        for path in files:
            extras = companion_files(path)
            try:
                imported.append(import_scr_file(cfg, path, extras=extras))
            except FileExistsError as exc:
                errors.append(str(exc))
            except NotScreensaverError as exc:
                errors.append(f"{path.name}: {exc}")
        if not imported:
            raise NotScreensaverError(
                errors[0] if errors else "No new screensavers were imported from this file."
            )
        return imported
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _import_tree(cfg: AppConfig, root: Path) -> list[ScreensaverEntry]:
    files = find_screensaver_files(root)
    if not files:
        raise NotScreensaverError(f"No .scr files found in {root.name}.")
    imported: list[ScreensaverEntry] = []
    for path in files:
        try:
            imported.append(import_scr_file(cfg, path))
        except FileExistsError:
            continue
        except NotScreensaverError:
            continue
    if not imported:
        raise FileExistsError(f"Every screensaver in {root.name} is already in the library.")
    return imported


def import_screensaver(cfg: AppConfig, source: Path) -> ScreensaverEntry:
    entries = import_any(cfg, source)
    return entries[0]


def is_installed_program(entry: ScreensaverEntry) -> bool:
    return bool(entry.wine_prefix)


def others_sharing_prefix(cfg: AppConfig, entry: ScreensaverEntry) -> list[ScreensaverEntry]:
    if not entry.wine_prefix:
        return []
    return [
        other
        for other in cfg.screensavers
        if other.id != entry.id and other.wine_prefix == entry.wine_prefix
    ]


def remove_screensaver(cfg: AppConfig, sid: str) -> None:
    entry = cfg.entry_by_id(sid)
    if entry is None:
        return
    victims = [entry.filename, *entry.extras]
    still_used: set[str] = set()
    for other in cfg.screensavers:
        if other.id == sid:
            continue
        still_used.add(other.filename)
        still_used.update(other.extras)
    skip_library = entry.is_steam() or entry.is_prefix_exe()
    if not skip_library:
        for name in victims:
            if name in still_used:
                continue
            path = paths.library_dir() / name
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:
                log.warning("Could not delete %s: %s", path, exc)
    cfg.screensavers = [item for item in cfg.screensavers if item.id != sid]
    if cfg.selected_id == sid:
        cfg.selected_id = cfg.screensavers[0].id if cfg.screensavers else ""
