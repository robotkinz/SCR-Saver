from __future__ import annotations

import struct
import subprocess
from pathlib import Path

MACHINE_I386 = 0x14C
MACHINE_AMD64 = 0x8664


class NotScreensaverError(ValueError):
    pass


def read_pe_machine(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise NotScreensaverError("Not a Windows executable (missing MZ header).")
        (e_lfanew,) = struct.unpack_from("<I", header, 0x3C)
        if e_lfanew > 10_000_000:
            raise NotScreensaverError("Not a Windows PE executable.")
        handle.seek(e_lfanew)
        pe = handle.read(6)
        if len(pe) < 6 or pe[:4] != b"PE\x00\x00":
            raise NotScreensaverError("Not a Windows PE executable.")
        (machine,) = struct.unpack_from("<H", pe, 4)
        return machine


def arch_name(machine: int) -> str:
    if machine == MACHINE_I386:
        return "i386"
    if machine == MACHINE_AMD64:
        return "x64"
    return f"unknown-{machine:#x}"


def file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pe_file_description(path: Path) -> str | None:
    try:
        data = subprocess.check_output(
            ["wrestool", "-x", "-t", "version", str(path)],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    if not data:
        return None

    decoded = data.decode("utf-16le", errors="ignore")
    parts = [part.strip() for part in decoded.split("\x00") if part.strip()]
    for key in ("FileDescription", "ProductName", "InternalName"):
        try:
            index = parts.index(key)
        except ValueError:
            continue
        if index + 1 < len(parts):
            value = parts[index + 1].strip()
            if value and value not in {"StringFileInfo", "VarFileInfo", "Translation"}:
                return value
    return None


def friendly_name(path: Path) -> str:
    description = pe_file_description(path)
    if description:
        return description
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return stem or path.name


def validate_scr(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise NotScreensaverError("File does not exist.")
    if path.stat().st_size < 64:
        raise NotScreensaverError("File is too small to be a screensaver.")
    machine = read_pe_machine(path)
    return machine, arch_name(machine)


def pe_subsystem_version(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise NotScreensaverError("Not a Windows executable.")
        (e_lfanew,) = struct.unpack_from("<I", header, 0x3C)
        handle.seek(e_lfanew)
        pe = handle.read(24 + 52)
        if len(pe) < 26 or pe[:4] != b"PE\x00\x00":
            raise NotScreensaverError("Not a Windows PE executable.")
        (magic,) = struct.unpack_from("<H", pe, 24)
        if magic not in (0x10B, 0x20B):
            return 0, 0
        (major, minor) = struct.unpack_from("<HH", pe, 24 + 48)
        return major, minor


def suggested_winver(path: Path) -> str:
    try:
        major, _minor = pe_subsystem_version(path)
    except (OSError, NotScreensaverError, struct.error):
        return ""
    if major <= 4:
        return "win98"
    if major == 5:
        return "winxp"
    return ""


def looks_like_installer(path: Path) -> bool:
    try:
        data = path.read_bytes()[: 2 * 1024 * 1024]
    except OSError:
        return False
    markers = (
        b"InstallShield",
        b"ISc(",
        b"NullsoftInst",
        b"Inno Setup",
        b"WinRAR SFX",
        b"7z\xbc\xaf'\x1c",
        b"Setup.exe",
        b"This installation",
    )
    if any(marker in data for marker in markers):
        return True
    suffix = path.suffix.lower()
    if suffix == ".exe":
        lower_name = path.name.lower()
        if any(token in lower_name for token in ("setup", "install", "sfx")):
            return True
    return False


def is_wildlife_stub(path: Path) -> bool:
    try:
        data = path.read_bytes()[: 512 * 1024]
    except OSError:
        return False
    return b"WL32DLL.DLL" in data or b"InitWildlife" in data
