from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .runners import discover_runners

log = logging.getLogger(__name__)

_STEAM_TOOL_NAMES = (
    "proton",
    "steam linux runtime",
    "steamworks common redistributables",
    "steamworks common",
)
_STEAM_TOOL_APPIDS = {
    "228980",  # Steamworks Common Redistributables
}
_STATE_INSTALLED = 4
_CLIENT_COMMS = {"steam"}
_MINIMIZE_SECONDS = 60.0
_MINIMIZE_INTERVAL = 0.4


@dataclass(frozen=True)
class SteamGame:
    appid: str
    name: str
    install_dir: Path
    library_root: Path
    prefix_path: Path | None
    proton_runner_id: str
    native: bool

    @property
    def has_prefix(self) -> bool:
        return self.prefix_path is not None and (self.prefix_path / "system.reg").is_file()


def steam_binary() -> str | None:
    found = shutil.which("steam")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/share/Steam/steam.sh",
        Path.home() / ".steam/steam/steam.sh",
        Path("/usr/bin/steam"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def steam_roots() -> list[Path]:
    return _unique_existing(
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".steam/root",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
    )


def list_installed_games() -> list[SteamGame]:
    games: list[SteamGame] = []
    seen: set[str] = set()
    libraries = _library_folders()
    runner_index = _runner_index()
    for library in libraries:
        steamapps = library / "steamapps"
        if not steamapps.is_dir():
            continue
        try:
            manifests = list(steamapps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for manifest in manifests:
            game = _game_from_manifest(manifest, library, runner_index)
            if game is None or game.appid in seen:
                continue
            seen.add(game.appid)
            games.append(game)
    games.sort(key=lambda item: item.name.lower())
    return games


def client_is_running() -> bool:
    """True when the Steam client process is already up."""
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip().lower()
        except OSError:
            continue
        if comm in _CLIENT_COMMS:
            return True
    return False


def launch_app(appid: str) -> None:
    binary = steam_binary()
    if not binary:
        raise FileNotFoundError("Steam was not found. Install Steam or add it to PATH.")
    already = client_is_running()
    log.info("Launching Steam app %s (client already running: %s)", appid, already)
    subprocess.Popen(
        [binary, "-silent", "-applaunch", str(appid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if already:
        return
    threading.Thread(
        target=_minimize_client_windows_loop,
        name="scrsaver-steam-minimize",
        daemon=True,
    ).start()


def minimize_client_windows() -> int:
    """Minimize Steam library/login windows. Leaves game windows alone."""
    count = 0
    for wid in _client_window_ids():
        if _minimize_window(wid):
            count += 1
    return count


def _minimize_client_windows_loop() -> None:
    deadline = time.monotonic() + _MINIMIZE_SECONDS
    logged: set[str] = set()
    while time.monotonic() < deadline:
        for wid in _client_window_ids():
            if _window_is_hidden(wid):
                continue
            if _minimize_window(wid) and wid not in logged:
                log.info("Minimized Steam client window %s so it does not cover a display", wid)
                logged.add(wid)
        time.sleep(_MINIMIZE_INTERVAL)


def _client_window_ids() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    output = _run_capture(["wmctrl", "-lx"])
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        wid, _desktop, klass = parts[0], parts[1], parts[2]
        title = parts[4] if len(parts) > 4 else ""
        if not _is_client_window(klass, title):
            continue
        if wid not in seen:
            seen.add(wid)
            found.append(wid)
    return found


def _is_client_window(klass: str, title: str) -> bool:
    lowered = (klass or "").lower()
    if "steam_app_" in lowered:
        return False
    instance, _, wm_class = lowered.partition(".")
    if wm_class != "steam":
        return False
    if instance not in ("", "steam", "steamwebhelper"):
        return False
    name = (title or "").strip()
    if not name:
        return True
    return name == "Steam" or name.startswith("Steam ") or name.startswith("Steam-") or name == "Friends List"


def _window_is_hidden(wid: str) -> bool:
    output = _run_capture(["xprop", "-id", wid, "_NET_WM_STATE"])
    return "_NET_WM_STATE_HIDDEN" in output


def _minimize_window(wid: str) -> bool:
    ok = False
    if _run(["wmctrl", "-i", "-r", wid, "-b", "add,hidden"]) == 0:
        ok = True
    decimal = _window_id_decimal(wid)
    if decimal and _run(["xdotool", "windowminimize", decimal]) == 0:
        ok = True
    return ok


def _window_id_decimal(wid: str) -> str:
    text = (wid or "").strip()
    if not text:
        return ""
    try:
        if text.lower().startswith("0x"):
            return str(int(text, 16))
        int(text, 10)
        return text
    except ValueError:
        return ""


def _run(cmd: list[str]) -> int:
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return int(completed.returncode)


def _run_capture(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def list_app_pids(appid: str) -> list[int]:
    wanted = str(appid)
    my_pid = os.getpid()
    found: list[int] = []
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    steam_names = {b"steam", b"steam.sh", b"steamwebhelper", b"steam-runtime"}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        app = _env_value(environ, b"SteamAppId") or _env_value(environ, b"SteamGameId")
        if not app:
            continue
        token = app.split(b"/", 1)[0].decode("ascii", errors="ignore")
        if token != wanted:
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip().encode()
        except OSError:
            comm = b""
        if comm.lower() in steam_names:
            continue
        found.append(pid)
    return found


def app_is_running(appid: str) -> bool:
    return bool(list_app_pids(appid))


def stop_app(appid: str, prefix: Path | None = None) -> None:
    pids = list_app_pids(appid)
    log.info("Stopping Steam app %s (pids %s)", appid, pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        remaining = list_app_pids(appid)
        if not remaining:
            break
        time.sleep(0.08)
    for pid in list_app_pids(appid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if prefix is not None and prefix.exists():
        try:
            from . import wineutil

            # Avoid constructing a full session if Wine is missing; wineserver -k is enough.
            env = os.environ.copy()
            env["WINEPREFIX"] = str(prefix)
            server = shutil.which("wineserver")
            if server:
                subprocess.run(
                    [server, "-k"],
                    env=env,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=6,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Could not stop Proton prefix for %s: %s", appid, exc)


def proton_runner_for_prefix(prefix: Path) -> str:
    info = prefix.parent / "config_info"
    if not info.is_file():
        info = prefix / "config_info"
    text = ""
    if info.is_file():
        try:
            text = info.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    index = _runner_index()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        mapped = index.get(lines[0].lower())
        if mapped:
            return mapped
    for line in lines:
        for path_str, runner_id in index.items():
            if "/" in str(path_str) and path_str and path_str in line:
                return runner_id
    return ""


def _game_from_manifest(path: Path, library: Path, runner_index: dict[str, str]) -> SteamGame | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    data = parse_vdf(text)
    state = data.get("AppState") if isinstance(data.get("AppState"), dict) else data
    if not isinstance(state, dict):
        return None
    appid = str(state.get("appid") or "")
    name = str(state.get("name") or "")
    installdir = str(state.get("installdir") or "")
    if not appid or not name:
        return None
    try:
        flags = int(str(state.get("StateFlags") or "0"))
    except ValueError:
        flags = 0
    if flags & _STATE_INSTALLED == 0:
        return None
    if appid in _STEAM_TOOL_APPIDS or _is_steam_tool(name):
        return None
    common = library / "steamapps" / "common" / installdir
    if not common.is_dir():
        return None
    pfx = library / "steamapps" / "compatdata" / appid / "pfx"
    prefix_path = pfx if (pfx / "system.reg").is_file() else None
    runner_id = ""
    if prefix_path is not None:
        runner_id = proton_runner_for_prefix(prefix_path) or _compat_tool_runner(appid, runner_index)
    native = prefix_path is None
    return SteamGame(
        appid=appid,
        name=name,
        install_dir=common,
        library_root=library,
        prefix_path=prefix_path,
        proton_runner_id=runner_id,
        native=native,
    )


def _compat_tool_runner(appid: str, runner_index: dict[str, str]) -> str:
    mapping = _compat_tool_mapping()
    tool = mapping.get(str(appid), "")
    if not tool:
        return ""
    return runner_index.get(tool.lower(), "")


def _compat_tool_mapping() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for root in steam_roots():
        config = root / "config" / "config.vdf"
        if not config.is_file():
            continue
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data = parse_vdf(text)
        node = _find_key(data, "CompatToolMapping")
        if not isinstance(node, dict):
            continue
        for appid, body in node.items():
            if isinstance(body, dict) and body.get("name"):
                mapping[str(appid)] = str(body["name"])
    return mapping


def _is_steam_tool(name: str) -> bool:
    lowered = name.strip().lower()
    if any(lowered.startswith(prefix) or lowered == prefix for prefix in _STEAM_TOOL_NAMES):
        return True
    return lowered.endswith(" soundtrack") or lowered.endswith(" ost") or lowered.endswith(" artbook")


def _library_folders() -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for root in steam_roots():
        _add_unique(found, seen, root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        folders = data.get("libraryfolders")
        if not isinstance(folders, dict):
            continue
        for body in folders.values():
            if isinstance(body, dict):
                path_str = str(body.get("path") or "")
            else:
                path_str = str(body)
            if path_str:
                _add_unique(found, seen, Path(path_str))
    return found


def _runner_index() -> dict[str, str]:
    index: dict[str, str] = {}
    try:
        runners = discover_runners()
    except Exception:
        return index
    for runner in runners:
        index[runner.name.lower()] = runner.id
        index[str(runner.root).lower()] = runner.id
        index[str(runner.root)] = runner.id
        # Proton config_info mentions files/share/fonts under the runner root.
        index[f"{runner.root}/files"] = runner.id
        slug = runner.name.lower().replace(" ", "")
        index[slug] = runner.id
        if runner.id.startswith("steam:"):
            index[runner.id.split(":", 1)[1].lower()] = runner.id
        if "experimental" in runner.name.lower():
            index["proton_experimental"] = runner.id
            index["proton experimental"] = runner.id
        match = re.search(r"proton[ -]?(\d+)", runner.name.lower())
        if match:
            index[f"proton_{match.group(1)}"] = runner.id
            index[f"proton {match.group(1)}"] = runner.id
        if "proton-ge" in runner.name.lower() or "ge-proton" in runner.name.lower():
            index["proton-ge"] = runner.id
            index["ge-proton"] = runner.id
    return index


def _env_value(environ: bytes, key: bytes) -> bytes | None:
    prefix = key + b"="
    for part in environ.split(b"\0"):
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None


def _find_key(node, name: str):
    if isinstance(node, dict):
        if name in node:
            return node[name]
        for value in node.values():
            found = _find_key(value, name)
            if found is not None:
                return found
    return None


def _unique_existing(*candidates: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        _add_unique(found, seen, candidate)
    return found


def _add_unique(found: list[Path], seen: set[Path], path: Path) -> None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return
    if resolved in seen or not resolved.is_dir():
        return
    seen.add(resolved)
    found.append(resolved)


def parse_vdf(text: str) -> dict:
    tokens = _vdf_tokens(text)
    values, index = _parse_vdf_map(tokens, 0, expect_braces=False)
    del index
    return values


def _parse_vdf_map(tokens: list[str], index: int, expect_braces: bool) -> tuple[dict, int]:
    if expect_braces:
        if index >= len(tokens) or tokens[index] != "{":
            return {}, index
        index += 1
    result: dict = {}
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            return result, index + 1
        key = token
        index += 1
        if index >= len(tokens):
            break
        if tokens[index] == "{":
            value, index = _parse_vdf_map(tokens, index, expect_braces=True)
        else:
            value = tokens[index]
            index += 1
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            merged = dict(result[key])
            merged.update(value)
            result[key] = merged
        else:
            result[key] = value
    return result, index


def _vdf_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "/" and i + 1 < length and text[i + 1] == "/":
            while i < length and text[i] not in "\n":
                i += 1
            continue
        if ch in "{}":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            while i < length:
                cur = text[i]
                if cur == "\\" and i + 1 < length:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if cur == '"':
                    i += 1
                    break
                buf.append(cur)
                i += 1
            tokens.append("".join(buf))
            continue
        buf = [ch]
        i += 1
        while i < length and text[i] not in ' \t\r\n{}"':
            buf.append(text[i])
            i += 1
        tokens.append("".join(buf))
    return tokens
