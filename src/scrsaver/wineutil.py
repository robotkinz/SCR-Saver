from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from dataclasses import dataclass

from . import paths
from .runners import SYSTEM_ID, WineRunner, get_runner

log = logging.getLogger(__name__)

WINE_DESKTOP_PREFIX = "SCRSAVER"
_PREFIX_LOCK = threading.Lock()


@dataclass
class WineSession:
    runner: WineRunner
    prefix: Path

    @property
    def wine(self) -> str:
        return str(self.runner.wine_bin)

    @property
    def wineserver(self) -> str:
        server = self.runner.wineserver_bin
        if server.is_file():
            return str(server)
        return str(self.runner.wine_bin.parent / "wineserver")


def session_for(entry=None) -> WineSession:
    runner_id = getattr(entry, "wine_runner", "") or SYSTEM_ID
    runner = get_runner(runner_id)
    if runner is None:
        if runner_id and runner_id != SYSTEM_ID:
            raise FileNotFoundError(
                f"Wine/Proton '{runner_id}' was not found on this system. "
                "Right-click the screensaver and pick another runner."
            )
        raise FileNotFoundError("Wine is not installed, and no Proton runner was found.")
    custom_prefix = getattr(entry, "wine_prefix", "") or ""
    if custom_prefix:
        prefix = prefix_path_for_name(custom_prefix)
    elif entry is not None and runner.id != SYSTEM_ID:
        prefix = paths.prefix_for_entry(entry.id)
    else:
        prefix = paths.wineprefix_dir()
    return WineSession(runner=runner, prefix=prefix)


def _runner_slug(runner_id: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in runner_id).strip("-")
    return slug or "runner"


def session_for_installer(runner_id: str) -> WineSession:
    runner = get_runner(runner_id)
    if runner is None:
        raise FileNotFoundError(f"Wine/Proton '{runner_id}' was not found on this system.")
    prefix = paths.prefixes_dir() / f"installer-{_runner_slug(runner.id)}"
    return WineSession(runner=runner, prefix=prefix)


def available_prefixes(cfg) -> list[tuple[str, str, str]]:
    """Return (label, prefix_name, runner_id). Empty prefix_name is the default bottle."""
    from .runners import runner_display

    choices: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    default_label = f"Default (wineprefix)  — {runner_display(SYSTEM_ID)}"
    choices.append((default_label, "", SYSTEM_ID))
    seen.add("wineprefix")
    for item in getattr(cfg, "managed_prefixes", []) or []:
        name = str(getattr(item, "name", "") or "")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        runner_id = str(getattr(item, "runner_id", "") or SYSTEM_ID)
        origin = str(getattr(item, "origin", "") or "")
        extra = ""
        if origin == "steam":
            extra = "Steam · "
        elif origin == "imported" or getattr(item, "source_path", ""):
            extra = "imported · "
        choices.append((f"{name}  — {extra}{runner_display(runner_id)}", name, runner_id))
    roots = [paths.data_dir(), paths.prefixes_dir()]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for path in children:
            if not path.is_dir() or not (path / "system.reg").is_file():
                continue
            if path.name.lower() in seen or path.name.lower() in paths.RESERVED_PREFIX_NAMES:
                continue
            seen.add(path.name.lower())
            choices.append((path.name, path.name, SYSTEM_ID))
    return choices


def session_for_named_prefix(name: str, runner_id: str) -> WineSession:
    runner = get_runner(runner_id)
    if runner is None:
        raise FileNotFoundError(f"Wine/Proton '{runner_id}' was not found on this system.")
    return WineSession(runner=runner, prefix=prefix_path_for_name(name))


def prefix_path_for_name(name: str, cfg=None) -> Path:
    if not name or name == "wineprefix":
        return paths.wineprefix_dir()
    if cfg is None:
        try:
            from .config import load_config

            cfg = load_config()
        except Exception:
            cfg = None
    if cfg is not None:
        for item in getattr(cfg, "managed_prefixes", []) or []:
            if item.name != name:
                continue
            source = str(getattr(item, "source_path", "") or "")
            if source:
                return Path(source).expanduser()
            break
    return paths.resolve_named_prefix(name)


def prefix_record_name(prefix: Path) -> str:
    try:
        resolved = prefix.expanduser().resolve()
    except OSError:
        resolved = prefix
    try:
        from .config import load_config

        cfg = load_config()
        for item in getattr(cfg, "managed_prefixes", []) or []:
            source = str(getattr(item, "source_path", "") or "")
            if source:
                try:
                    if Path(source).expanduser().resolve() == resolved:
                        return item.name
                except OSError:
                    pass
            if item.name == prefix.name:
                return item.name
    except Exception:
        pass
    name = prefix.name
    return name or "wineprefix"


def cpu_policy_for(session: WineSession) -> tuple[float, bool]:
    try:
        from .config import load_config

        cfg = load_config()
    except Exception:
        return 100, False
    key = prefix_record_name(session.prefix)
    for item in getattr(cfg, "managed_prefixes", []) or []:
        if item.name == key or (key == "wineprefix" and item.name in ("", "wineprefix")):
            from .cpu_limit import clamp_quota_percent

            return clamp_quota_percent(item.cpu_percent or 100), bool(item.single_core)
    return 100.0, False


def popen_wine(
    session: WineSession,
    argv: list[str],
    env: dict[str, str] | None = None,
    **kwargs,
) -> subprocess.Popen[bytes]:
    """Launch a Wine command with this prefix's CPU slowdown policy."""
    run_env = dict(env or wine_env(session))
    percent, single_core = cpu_policy_for(session)
    if single_core:
        run_env["WINE_CPU_TOPOLOGY"] = "1:0"
    fps_limit = fps_limit_for(session)
    if fps_limit > 0:
        _apply_frame_pace(run_env, fps_limit)
        try:
            from .audio import ensure_silent_sink

            run_env["PULSE_SINK"] = ensure_silent_sink()
        except Exception:
            pass
    cmd = list(argv)
    properties: list[str] = []
    if percent < 100:
        from .cpu_limit import quota_property

        properties.append(quota_property(percent))
    if single_core:
        properties.append("AllowedCPUs=0")
    if properties and shutil.which("systemd-run"):
        wrapped = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
        for prop in properties:
            wrapped.extend(["-p", prop])
        wrapped.extend(["--", *cmd])
        cmd = wrapped
    popen_kwargs: dict = {
        "env": run_env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    popen_kwargs.update(kwargs)

    def _pin() -> None:
        if single_core:
            try:
                os.sched_setaffinity(0, {0})
            except OSError:
                pass

    if single_core and "systemd-run" not in cmd[:1]:
        popen_kwargs["preexec_fn"] = _pin
    extras = []
    if percent < 100 or single_core:
        extras.append(f"cpu={percent:g}%")
    if fps_limit > 0:
        extras.append(f"fps={fps_limit}")
    log.info("Wine launch%s: %s", f" ({', '.join(extras)})" if extras else "", " ".join(cmd[:8]))
    return subprocess.Popen(cmd, **popen_kwargs)


def fps_limit_for(session: WineSession) -> int:
    try:
        from .config import load_config

        cfg = load_config()
    except Exception:
        return 0
    try:
        return max(0, min(120, int(getattr(cfg, "fps_limit", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def _apply_frame_pace(env: dict[str, str], fps: int) -> None:
    env["SCRSAVER_FPS_LIMIT"] = str(fps)
    env["DXVK_FRAME_RATE"] = str(fps)
    so = paths.project_root() / "native" / "libscrsaver_pace.so"
    if so.is_file():
        prev = env.get("LD_PRELOAD", "")
        env["LD_PRELOAD"] = str(so) if not prev else f"{so}:{prev}"


def launch_exe(session: WineSession, linux_path: Path) -> None:
    setup_prefix(session)
    wine_path = linux_to_wine_path(linux_path)
    popen_wine(session, [session.wine, wine_path])


def list_prefix_screensavers(prefix: Path) -> list[Path]:
    drive = prefix / "drive_c"
    if not drive.is_dir():
        return []
    found: list[Path] = []
    for path in drive.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".scr":
            found.append(path)
    return found


@dataclass
class UninstallEntry:
    key: str
    display_name: str
    uninstall_string: str
    quiet_uninstall: str = ""
    install_location: str = ""


def is_installed_program(entry) -> bool:
    """True when this saver was installed into Wine as a Windows program."""
    prefix = str(getattr(entry, "wine_prefix", "") or "")
    return bool(prefix)


def list_uninstall_entries(prefix: Path) -> list[UninstallEntry]:
    found: list[UninstallEntry] = []
    for name in ("system.reg", "user.reg"):
        try:
            found.extend(_parse_reg_uninstall(prefix / name))
        except Exception as exc:
            log.warning("Could not read %s: %s", name, exc)
    return found


def match_uninstall_entry(entry, items: list[UninstallEntry]) -> UninstallEntry | None:
    name = (entry.name or "").strip().lower()
    stem = Path(entry.filename).stem.lower()
    best: tuple[int, UninstallEntry] | None = None
    for item in items:
        hay = " ".join(
            [item.display_name, item.key, item.install_location, item.uninstall_string]
        ).lower()
        score = 0
        if name and item.display_name.lower() == name:
            score = 100
        elif name and name in item.display_name.lower():
            score = 85
        elif stem and stem in item.display_name.lower():
            score = 75
        elif name and name in hay:
            score = 60
        elif stem and stem in hay:
            score = 55
        if score and (best is None or score > best[0]):
            best = (score, item)
    if best is None or best[0] < 55:
        return None
    return best[1]


_UNINSTALLER_NAMES = {"unins000.exe", "uninstall.exe", "uninst.exe", "unwise.exe"}


def find_uninstaller_exe(prefix: Path, entry) -> Path | None:
    """Find an uninstaller next to the program, not a random unins000.exe in Windows."""
    install_dir = _program_install_dir(prefix, entry)
    if install_dir is None:
        return None
    search = [install_dir, *install_dir.parents]
    drive = prefix / "drive_c"
    for folder in search:
        try:
            folder.relative_to(drive)
        except ValueError:
            break
        for name in _UNINSTALLER_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return candidate
        if folder == drive:
            break
    return None


def _program_install_dir(prefix: Path, entry) -> Path | None:
    drive = prefix / "drive_c"
    if not drive.is_dir():
        return None
    filename = Path(entry.filename).name
    roots = [
        drive / "Program Files",
        drive / "Program Files (x86)",
        drive / "Program Files (x86)" / "nvidia corporation",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob(filename):
                if not path.is_file():
                    continue
                if path.parent.name.lower() == "scrsaver":
                    continue
                return path.parent
        except OSError:
            continue
    return None


def launch_uninstaller(session: WineSession, command: str) -> None:
    setup_prefix(session)
    exe = _windows_command_exe(session.prefix, command)
    if exe is not None:
        launch_exe(session, exe)
        return
    popen_wine(session, [session.wine, "start", "/wait", command])


def uninstaller_exe_for(session: WineSession, entry) -> Path | None:
    exe = find_uninstaller_exe(session.prefix, entry)
    if exe is not None:
        return exe
    try:
        match = match_uninstall_entry(entry, list_uninstall_entries(session.prefix))
    except Exception as exc:
        log.warning("Uninstall registry lookup failed: %s", exc)
        return None
    if match is None:
        return None
    command = (match.uninstall_string or match.quiet_uninstall).strip()
    return _windows_command_exe(session.prefix, command)


def uninstaller_exe_for_entry(entry) -> Path | None:
    try:
        session = session_for(entry)
    except Exception:
        return None
    try:
        return uninstaller_exe_for(session, entry)
    except Exception as exc:
        log.warning("Could not look up uninstaller: %s", exc)
        return None


def start_program_uninstaller(session: WineSession, entry) -> str:
    """Launch a product uninstaller, or Wine's Add/Remove Programs as a fallback.

    Returns "exe" or "gui".
    """
    exe = uninstaller_exe_for(session, entry)
    if exe is not None:
        launch_exe(session, exe)
        return "exe"
    launch_wine_uninstaller_gui(session)
    return "gui"


def _windows_command_exe(prefix: Path, command: str) -> Path | None:
    text = command.strip().strip('"')
    if not text or text.lower().startswith("msiexec"):
        return None
    token = text
    if token.startswith('"'):
        end = token.find('"', 1)
        token = token[1:end] if end > 1 else token.strip('"')
    else:
        token = token.split(" ", 1)[0]
    token = token.strip().strip('"')
    if not token.lower().endswith(".exe"):
        return None
    linux = _wine_path_to_linux(prefix, token)
    if linux is not None and linux.is_file():
        return linux
    return None


def _wine_path_to_linux(prefix: Path, windows_path: str) -> Path | None:
    text = windows_path.replace("/", "\\")
    if len(text) < 3 or text[1] != ":":
        return None
    rest = text[2:].lstrip("\\")
    return prefix / "drive_c" / rest.replace("\\", "/")


def launch_wine_uninstaller_gui(session: WineSession) -> None:
    setup_prefix(session)
    popen_wine(session, [session.wine, "uninstaller"])


def _read_reg_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    return data.decode("utf-8", errors="replace")


def _parse_reg_uninstall(path: Path) -> list[UninstallEntry]:
    if not path.is_file():
        return []
    try:
        text = _read_reg_text(path)
    except OSError:
        return []
    entries: list[UninstallEntry] = []
    current: UninstallEntry | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            if current is not None and (current.display_name or current.uninstall_string):
                entries.append(current)
            current = None
            header = line.strip("[]")
            marker = "CurrentVersion\\\\Uninstall\\\\"
            alt = "CurrentVersion\\Uninstall\\"
            if marker in header:
                key = header.split(marker, 1)[1].split("]")[0]
            elif alt in header:
                key = header.split(alt, 1)[1].split("]")[0]
            else:
                continue
            current = UninstallEntry(key=key.replace("\\\\", "\\"), display_name="", uninstall_string="")
            continue
        if current is None or not line.startswith('"'):
            continue
        key_name, _, rest = line.partition("=")
        name = key_name.strip().strip('"')
        value = _reg_unquote(rest)
        if name == "DisplayName":
            current.display_name = value
        elif name == "UninstallString":
            current.uninstall_string = value
        elif name == "QuietUninstallString":
            current.quiet_uninstall = value
        elif name == "InstallLocation":
            current.install_location = value
    if current is not None and (current.display_name or current.uninstall_string):
        entries.append(current)
    return entries


def _reg_unquote(value: str) -> str:
    text = value.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.replace('\\"', '"').replace("\\\\", "\\")


def wine_binary(session: WineSession | None = None) -> str:
    current = session or session_for(None)
    if not Path(current.wine).is_file():
        raise FileNotFoundError(f"Wine binary not found: {current.wine}")
    return current.wine


def wineserver_binary(session: WineSession | None = None) -> str:
    current = session or session_for(None)
    return current.wineserver


def wine_env(session: WineSession | None = None) -> dict[str, str]:
    current = session or session_for(None)
    env = os.environ.copy()
    env["WINEPREFIX"] = str(current.prefix)
    env["WINE"] = current.wine
    env["WINELOADER"] = current.wine
    env["WINESERVER"] = current.wineserver
    env["WINEDEBUG"] = "-all"
    env["WINEARCH"] = "win64"
    wine_dir = str(Path(current.wine).parent)
    env["PATH"] = wine_dir + os.pathsep + env.get("PATH", "")
    overrides = env.get("WINEDLLOVERRIDES", "")
    extra = "winemenubuilder.exe=d"
    env["WINEDLLOVERRIDES"] = f"{overrides};{extra}" if overrides else extra
    return env


def prefix_ready(session: WineSession | None = None) -> bool:
    prefix = (session or session_for(None)).prefix
    return (prefix / "system.reg").is_file() and (prefix / "drive_c").is_dir()


def linux_to_wine_path(linux_path: Path) -> str:
    resolved = linux_path.resolve()
    return "Z:" + str(resolved).replace("/", "\\")


def _copy_if_needed(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (
        dest.is_file()
        and dest.stat().st_mtime == source.stat().st_mtime
        and dest.stat().st_size == source.stat().st_size
    ):
        return
    shutil.copy2(source, dest)


def stage_screensaver(linux_path: Path, session: WineSession | None = None) -> str:
    """Copy a .scr into the prefix and return a C:\\ path Wine can execute."""
    current = session or session_for(None)
    setup_prefix(current)
    dest_dir = current.prefix / "drive_c" / "scrsaver"
    dest = dest_dir / linux_path.name
    _copy_if_needed(linux_path, dest)
    return f"C:\\scrsaver\\{dest.name}"


def _library_sidecars(entry) -> list[Path]:
    """Sidecars listed in config plus same-stem / engine DLLs sitting in the library."""
    library = paths.library_dir()
    names = {str(name) for name in (getattr(entry, "extras", []) or [])}
    source = entry.linux_path()
    stem = source.stem.lower()
    engine = {"wl32dll.dll", "wl16dll.dll", "wildlb32.dll"}
    if library.is_dir():
        for child in library.iterdir():
            if not child.is_file():
                continue
            name = child.name.lower()
            if child.resolve() == source.resolve():
                continue
            if child.stem.lower() == stem or name in engine:
                names.add(child.name)
    return [library / name for name in sorted(names)]


def stage_entry(entry, session: WineSession | None = None) -> str:
    """Copy a library screensaver and its sidecars into the Wine prefix."""
    current = session or session_for(entry)
    setup_prefix(current)
    source = entry.linux_path()
    wine_path = stage_screensaver(source, current)
    dest_dir = current.prefix / "drive_c" / "scrsaver"
    system32 = current.prefix / "drive_c" / "windows" / "system32"
    syswow64 = current.prefix / "drive_c" / "windows" / "syswow64"
    for extra in _library_sidecars(entry):
        if not extra.is_file():
            continue
        _copy_if_needed(extra, dest_dir / extra.name)
        if extra.suffix.lower() == ".dll":
            _copy_if_needed(extra, system32 / extra.name)
            _copy_if_needed(extra, syswow64 / extra.name)
    winver = getattr(entry, "winver", "") or ""
    if winver:
        set_app_windows_version(source.name, winver, current)
    return wine_path


def set_app_windows_version(exe_name: str, version: str, session: WineSession | None = None) -> None:
    current = session or session_for(None)
    try:
        wine = wine_binary(current)
    except FileNotFoundError:
        return
    subprocess.run(
        [
            wine,
            "reg",
            "add",
            rf"HKCU\Software\Wine\AppDefaults\{exe_name}",
            "/v",
            "Version",
            "/d",
            version,
            "/f",
        ],
        env=wine_env(current),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def setup_prefix(session: WineSession | None = None) -> None:
    current = session or session_for(None)
    wine = wine_binary(current)
    prefix = current.prefix
    with _PREFIX_LOCK:
        prefix.mkdir(parents=True, exist_ok=True)
        env = wine_env(current)
        if not prefix_ready(current):
            log.info("Initializing Wine prefix at %s with %s", prefix, current.runner.name)
            boot_env = dict(env)
            boot_env["WINEDLLOVERRIDES"] = "winemenubuilder.exe=d;mscoree=d;mshtml=d"
            completed = subprocess.run(
                [wine, "wineboot", "--init"],
                env=boot_env,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=180,
            )
            if completed.returncode != 0 and not prefix_ready(current):
                detail = completed.stderr.decode("utf-8", errors="replace")[-400:]
                raise RuntimeError(detail or f"wineboot failed with code {completed.returncode}")
            subprocess.run(
                [wine, "winecfg", "-v", "win7"],
                env=env,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        (prefix / "drive_c" / "scrsaver").mkdir(parents=True, exist_ok=True)


def open_winecfg(session: WineSession) -> None:
    setup_prefix(session)
    popen_wine(session, [session.wine, "winecfg"])


def open_winetricks(session: WineSession) -> None:
    tricks = shutil.which("winetricks")
    if not tricks:
        raise FileNotFoundError("Winetricks is not installed or not on PATH.")
    setup_prefix(session)
    popen_wine(session, [tricks, "--gui"])


def list_prefix_pids(session: WineSession | None = None) -> list[int]:
    """PIDs whose environment points at this Wine prefix."""
    prefix = (session or session_for(None)).prefix.resolve()
    needle = f"WINEPREFIX={prefix}".encode()
    prefix_bytes = str(prefix).encode()
    my_pid = os.getpid()
    found: list[int] = []
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == my_pid:
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            environ = b""
        belongs = False
        if environ:
            for part in environ.split(b"\0"):
                if part == needle or part.startswith(needle + b"/"):
                    belongs = True
                    break
        if not belongs:
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                cmdline = b""
            if prefix_bytes and prefix_bytes in cmdline:
                belongs = True
        if belongs:
            found.append(pid)
    return found


def kill_prefix(session: WineSession | None = None) -> None:
    """Stop every process in the private Wine prefix, including audio clients."""
    current = session or session_for(None)
    prefix = current.prefix
    if not prefix.exists():
        return
    log.info("Stopping Wine prefix %s", prefix)
    try:
        subprocess.run(
            [wineserver_binary(current), "-k"],
            env=wine_env(current),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("wineserver -k failed: %s", exc)

    deadline = time.monotonic() + 1.2
    remaining: list[int] = []
    while time.monotonic() < deadline:
        remaining = list_prefix_pids(current)
        if not remaining:
            return
        time.sleep(0.08)

    if remaining:
        log.warning("Killing leftover Wine processes: %s", remaining)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
