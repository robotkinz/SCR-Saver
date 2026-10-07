from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SYSTEM_ID = "system"

_WINE_LAYOUTS = (
    ("files/bin/wine", "files/bin/wineserver"),
    ("dist/bin/wine", "dist/bin/wineserver"),
    ("bin/wine", "bin/wineserver"),
)


@dataclass(frozen=True)
class WineRunner:
    id: str
    name: str
    source: str
    kind: str
    wine_bin: Path
    wineserver_bin: Path
    root: Path


_CACHE: list[WineRunner] | None = None


def discover_runners(*, refresh: bool = False) -> list[WineRunner]:
    global _CACHE
    if _CACHE is not None and not refresh:
        return list(_CACHE)
    found: list[WineRunner] = []
    seen: set[Path] = set()

    def add(runner: WineRunner) -> None:
        try:
            key = runner.wine_bin.resolve()
        except OSError:
            key = runner.wine_bin
        if key in seen:
            return
        if not runner.wine_bin.is_file() or not os.access(runner.wine_bin, os.X_OK):
            return
        seen.add(key)
        found.append(runner)

    system = shutil.which("wine")
    if system:
        wine = Path(system)
        server = Path(shutil.which("wineserver") or (wine.parent / "wineserver"))
        add(
            WineRunner(
                id=SYSTEM_ID,
                name=f"System Wine ({_system_wine_version(wine)})",
                source="System",
                kind="wine",
                wine_bin=wine,
                wineserver_bin=server,
                root=wine.parent,
            )
        )

    steam_roots = _unique_dirs(
        Path.home() / ".local/share/Steam/steamapps/common",
        Path.home() / ".steam/steam/steamapps/common",
        Path.home() / ".steam/root/steamapps/common",
    )
    for root in steam_roots:
        for child in _iter_dirs(root):
            if not child.name.startswith("Proton"):
                continue
            bins = _wine_bins(child)
            if bins is None:
                continue
            wine, server = bins
            add(
                WineRunner(
                    id=f"steam:{child.name}",
                    name=child.name,
                    source="Steam Proton",
                    kind="proton",
                    wine_bin=wine,
                    wineserver_bin=server,
                    root=child,
                )
            )

    compat_roots = _unique_dirs(
        Path.home() / ".local/share/Steam/compatibilitytools.d",
        Path.home() / ".steam/root/compatibilitytools.d",
        Path.home() / ".steam/steam/compatibilitytools.d",
        Path("/usr/share/steam/compatibilitytools.d"),
        Path.home() / ".local/share/umu/compatibilitytools.d",
    )
    for root in compat_roots:
        for child in _iter_dirs(root):
            bins = _wine_bins(child)
            if bins is None:
                continue
            wine, server = bins
            add(
                WineRunner(
                    id=f"compat:{child.name}",
                    name=child.name,
                    source="Proton (compatibility tools)",
                    kind="proton",
                    wine_bin=wine,
                    wineserver_bin=server,
                    root=child,
                )
            )

    lutris = Path.home() / ".local/share/lutris/runners/wine"
    for child in _iter_dirs(lutris):
        bins = _wine_bins(child)
        if bins is None:
            continue
        wine, server = bins
        kind = "proton" if (child / "proton").is_file() else "wine"
        add(
            WineRunner(
                id=f"lutris:{child.name}",
                name=child.name,
                source="Lutris",
                kind=kind,
                wine_bin=wine,
                wineserver_bin=server,
                root=child,
            )
        )

    for heroic_root, label in (
        (Path.home() / ".config/heroic/tools/proton", "Heroic"),
        (Path.home() / ".config/heroic/tools/wine", "Heroic"),
        (Path.home() / ".var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton", "Heroic"),
        (Path.home() / ".var/app/com.heroicgameslauncher.hgl/config/heroic/tools/wine", "Heroic"),
    ):
        for child in _iter_dirs(heroic_root):
            bins = _wine_bins(child)
            if bins is None:
                continue
            wine, server = bins
            kind = "proton" if (child / "proton").is_file() else "wine"
            add(
                WineRunner(
                    id=f"heroic:{child.name}",
                    name=child.name,
                    source=label,
                    kind=kind,
                    wine_bin=wine,
                    wineserver_bin=server,
                    root=child,
                )
            )

    bottles = Path.home() / ".local/share/bottles/runners"
    for child in _iter_dirs(bottles):
        bins = _wine_bins(child)
        if bins is None:
            continue
        wine, server = bins
        add(
            WineRunner(
                id=f"bottles:{child.name}",
                name=child.name,
                source="Bottles",
                kind="wine",
                wine_bin=wine,
                wineserver_bin=server,
                root=child,
            )
        )

    for arch, arch_label in (("linux-amd64", "64-bit"), ("linux-x86", "32-bit")):
        pol = Path.home() / ".PlayOnLinux/wine" / arch
        for child in _iter_dirs(pol):
            bins = _wine_bins(child)
            if bins is None:
                continue
            wine, server = bins
            add(
                WineRunner(
                    id=f"playonlinux:{arch}:{child.name}",
                    name=f"{child.name} ({arch_label})",
                    source="PlayOnLinux",
                    kind="wine",
                    wine_bin=wine,
                    wineserver_bin=server,
                    root=child,
                )
            )

    _CACHE = found
    log.info("Detected %s Wine/Proton runners", len(found))
    return list(found)


def get_runner(runner_id: str | None) -> WineRunner | None:
    wanted = runner_id or SYSTEM_ID
    for runner in discover_runners():
        if runner.id == wanted:
            return runner
    return None


def runner_display(runner_id: str | None) -> str:
    runner = get_runner(runner_id)
    if runner is None:
        if runner_id and runner_id != SYSTEM_ID:
            return f"{runner_id} (not found)"
        return "System Wine"
    return runner.name


def grouped_runners() -> list[tuple[str, list[WineRunner]]]:
    groups: dict[str, list[WineRunner]] = {}
    order: list[str] = []
    for runner in discover_runners(refresh=True):
        if runner.source not in groups:
            groups[runner.source] = []
            order.append(runner.source)
        groups[runner.source].append(runner)
    return [(source, groups[source]) for source in order]


def _wine_bins(root: Path) -> tuple[Path, Path] | None:
    for wine_rel, server_rel in _WINE_LAYOUTS:
        wine = root / wine_rel
        if wine.is_file() and os.access(wine, os.X_OK):
            server = root / server_rel
            if not server.is_file():
                server = wine.parent / "wineserver"
            return wine, server
    return None


def _iter_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return []
    children.sort(key=lambda item: item.name.lower())
    return children


def _unique_dirs(*candidates: Path) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _system_wine_version(wine: Path) -> str:
    env = os.environ.copy()
    env["WINEDEBUG"] = "-all"
    try:
        result = subprocess.run(
            [str(wine), "--version"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return wine.name
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0] if text else wine.name
