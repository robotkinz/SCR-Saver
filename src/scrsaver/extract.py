from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".cab", ".iso"}
SKIP_EXE_NAMES = {
    "setup.exe",
    "setup16.exe",
    "install.exe",
    "ikernel.exe",
    "unins000.exe",
    "uninstall.exe",
}


def unshield_binary() -> str | None:
    candidates = [
        shutil.which("unshield"),
        str(paths.project_root() / "native" / "unshield"),
        str(paths.data_dir() / "bin" / "unshield"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def is_installshield_cab(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic == b"ISc("


def unpack_container(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    seven = shutil.which("7z") or shutil.which("7za")
    if seven:
        result = subprocess.run(
            [seven, "x", "-y", f"-o{dest}", str(source)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if result.returncode != 0:
            log.debug("7z extract of %s: %s", source, result.stderr[-300:])
    elif source.suffix.lower() == ".zip":
        shutil.unpack_archive(str(source), str(dest), "zip")

    unshield = unshield_binary()
    if not unshield:
        return
    cabs = [source] if is_installshield_cab(source) and source.suffix.lower() != ".hdr" else []
    cabs.extend(
        path
        for path in dest.rglob("*")
        if path.is_file() and path.suffix.lower() != ".hdr" and is_installshield_cab(path)
    )
    preferred = [path for path in cabs if path.name.lower() == "data1.cab"]
    if preferred:
        cabs = preferred
    seen: set[str] = set()
    for cab in cabs:
        key = str(cab.resolve())
        if key in seen:
            continue
        seen.add(key)
        out = dest / "_unshield" / cab.stem
        out.mkdir(parents=True, exist_ok=True)
        log.info("Extracting InstallShield cabinet %s", cab)
        subprocess.run(
            [unshield, "-d", str(out), "x", str(cab)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )


def find_screensaver_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".scr":
            found.append(path)
            continue
        if suffix == ".exe" and path.name.lower() not in SKIP_EXE_NAMES:
            # Ignore installer leftovers; real savers named .exe are uncommon here.
            continue
    found.sort(key=lambda item: item.as_posix().lower())
    return found
