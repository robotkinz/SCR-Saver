from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import heroic, paths, steam, wineutil
from .config import (
    ORIGIN_IMPORTED,
    ORIGIN_STEAM,
    LAUNCH_PREFIX_EXE,
    LAUNCH_STEAM,
    AppConfig,
    ManagedPrefix,
    ScreensaverEntry,
    save_config,
)
from .keystrokes import record_autoplay_keys
from .pe import friendly_name
from .runners import SYSTEM_ID

log = logging.getLogger(__name__)

_SKIP_EXE_DIRS = {
    "windows",
    "windows.old",
    "system32",
    "syswow64",
    "syswow16",
    "winsxs",
    "temp",
    "tmp",
    "installer",
}
_SKIP_EXE_NAMES = {
    "unins000.exe",
    "uninstall.exe",
    "uninst.exe",
    "unwise.exe",
    "wineboot.exe",
    "explorer.exe",
    "services.exe",
    "plugplay.exe",
    "winedevice.exe",
    "rpcss.exe",
    "start.exe",
    "cmd.exe",
    "conhost.exe",
    "winemenubuilder.exe",
}


class SteamGameDialog(QDialog):
    def __init__(self, games: list[steam.SteamGame], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._games = games
        self._chosen: steam.SteamGame | None = None
        self.setWindowTitle("Import Steam game")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Installed Steam games on this computer. Pick one to add its prefix "
                "(when Proton created one) and the game itself to SCR Saver. "
                "The game will launch through Steam as an interactive demo."
            )
        )
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name…")
        self.search.textChanged.connect(self._reload)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemDoubleClicked.connect(self._accept)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Import")
        ok.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)
        self._reload()

    def selected_game(self) -> steam.SteamGame | None:
        return self._chosen

    def _reload(self) -> None:
        needle = self.search.text().strip().lower()
        self.list.clear()
        for game in self._games:
            if needle and needle not in game.name.lower() and needle not in game.appid:
                continue
            kind = "native Linux" if game.native else "Proton"
            item = QListWidgetItem(f"{game.name}    ({kind} · {game.appid})")
            item.setData(Qt.ItemDataRole.UserRole, game.appid)
            item.setToolTip(str(game.install_dir))
            self.list.addItem(item)
        if self.list.count() and self.list.currentItem() is None:
            self.list.setCurrentRow(0)

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is None:
            QMessageBox.information(self, "Import Steam game", "Select a game first.")
            return
        appid = str(item.data(Qt.ItemDataRole.UserRole) or "")
        for game in self._games:
            if game.appid == appid:
                self._chosen = game
                self.accept()
                return


class ExePickDialog(QDialog):
    def __init__(self, prefix: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefix = prefix
        self._chosen: Path | None = None
        self.setWindowTitle("Choose a Windows program")
        self.setMinimumSize(560, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Pick the .exe to add to the screensaver list, the same way PlayOnLinux "
                "lets you install a non-listed program. Programs found in this prefix "
                "are listed below; use Browse if the one you want is elsewhere."
            )
        )
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemDoubleClicked.connect(self._accept_list)
        layout.addWidget(self.list, 1)
        for path in find_prefix_executables(prefix):
            rel = _display_rel(prefix, path)
            item = QListWidgetItem(rel)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        row = QHBoxLayout()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Use this program")
        ok.clicked.connect(self._accept_list)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)

    def selected_exe(self) -> Path | None:
        return self._chosen

    def _accept_list(self) -> None:
        item = self.list.currentItem()
        if item is None:
            QMessageBox.information(self, "Choose a Windows program", "Select a program, or browse for one.")
            return
        path = Path(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        if not path.is_file():
            QMessageBox.warning(self, "Choose a Windows program", "That file is no longer there.")
            return
        self._chosen = path
        self.accept()

    def _browse(self) -> None:
        start = self._prefix / "drive_c"
        if not start.is_dir():
            start = self._prefix
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose a Windows program",
            str(start),
            "Windows programs (*.exe *.EXE);;All files (*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        if not path.is_file():
            return
        self._chosen = path
        self.accept()


def import_steam_game(
    cfg: AppConfig,
    game: steam.SteamGame,
    parent: QWidget | None = None,
    *,
    idle=None,
) -> ScreensaverEntry:
    existing = _entry_for_steam(cfg, game.appid)
    prefix_name = ""
    if game.has_prefix and game.prefix_path is not None:
        prefix_name = _ensure_steam_prefix(cfg, game)
    if existing is None:
        existing = ScreensaverEntry(
            id=str(uuid.uuid4()),
            name=game.name,
            filename=f"steam-{game.appid}",
            enabled=True,
            arch="steam",
            notes="Launches through Steam as an interactive demo.",
            wine_runner=game.proton_runner_id,
            wine_prefix=prefix_name,
            interactive=True,
            launch_kind=LAUNCH_STEAM,
            steam_appid=game.appid,
        )
        cfg.screensavers.append(existing)
        if not cfg.selected_id:
            cfg.selected_id = existing.id
    else:
        existing.name = game.name
        existing.interactive = True
        existing.launch_kind = LAUNCH_STEAM
        existing.steam_appid = game.appid
        if prefix_name:
            existing.wine_prefix = prefix_name
        if game.proton_runner_id:
            existing.wine_runner = game.proton_runner_id
    save_config(cfg)
    macro = _record_after_launch(
        parent,
        game.name,
        idle=idle,
        on_start=lambda: steam.launch_app(game.appid),
        on_stop=lambda: steam.stop_app(game.appid, game.prefix_path),
        loop_default=existing.keystroke_loop,
        stop_on_input_default=existing.stop_on_input if existing.keystrokes else True,
    )
    if macro is not None:
        existing.keystrokes = macro.events
        existing.keystroke_loop = macro.loop
        existing.stop_on_input = macro.stop_on_input
        save_config(cfg)
    return existing


def import_program_exe(
    cfg: AppConfig,
    exe: Path,
    parent: QWidget | None = None,
    *,
    idle=None,
) -> ManagedPrefix:
    exe = exe.expanduser().resolve()
    if not exe.is_file():
        raise FileNotFoundError(f"{exe} is not a file.")
    game = heroic.match_exe(exe)
    prefix, runner_id, label = _resolve_prefix_for_exe(exe, game, parent)
    rec = _ensure_imported_prefix(cfg, prefix, runner_id, label)
    autoplay = _ask_autoplay(parent, exe.name)
    entry = _add_prefix_exe_entry(
        cfg,
        rec,
        exe,
        interactive=autoplay,
        notes=_import_notes(rec, game),
        display_name=(game.name if game is not None else ""),
    )
    if autoplay:
        macro = _record_after_launch(
            parent,
            entry.name,
            idle=idle,
            on_start=lambda: wineutil.launch_exe(wineutil.session_for(entry), exe),
            on_stop=lambda: wineutil.kill_prefix(wineutil.session_for(entry)),
            loop_default=entry.keystroke_loop,
            stop_on_input_default=entry.stop_on_input if entry.keystrokes else True,
        )
        if macro is not None:
            entry.keystrokes = macro.events
            entry.keystroke_loop = macro.loop
            entry.stop_on_input = macro.stop_on_input
            save_config(cfg)
    return rec


def import_external_prefix(
    cfg: AppConfig,
    folder: Path,
    parent: QWidget | None = None,
    *,
    idle=None,
) -> ManagedPrefix:
    prefix = paths.wine_prefix_root(folder)
    if not (prefix / "system.reg").is_file():
        raise FileNotFoundError(f"{folder} does not look like a Wine prefix (no system.reg).")
    rec = _ensure_imported_prefix(
        cfg,
        prefix,
        steam.proton_runner_for_prefix(prefix) or SYSTEM_ID,
        prefix.name,
    )
    autoplay = QMessageBox.question(
        parent,
        "Import prefix",
        "Is there a game in this prefix with an autoplay feature?\n\n"
        "Yes records the keys and mouse input that start autoplay, then replays "
        "them when the program runs as a screensaver or interactive demo.\n\n"
        "No lets you pick an .exe to add to the screensaver list, like PlayOnLinux "
        "does for a non-listed program.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if autoplay == QMessageBox.StandardButton.Yes:
        exe = _choose_exe(prefix, parent)
        if exe is None:
            return rec
        entry = _add_prefix_exe_entry(cfg, rec, exe, interactive=True)
        macro = _record_after_launch(
            parent,
            entry.name,
            idle=idle,
            on_start=lambda: wineutil.launch_exe(wineutil.session_for(entry), exe),
            on_stop=lambda: wineutil.kill_prefix(wineutil.session_for(entry)),
            loop_default=entry.keystroke_loop,
            stop_on_input_default=entry.stop_on_input if entry.keystrokes else True,
        )
        if macro is not None:
            entry.keystrokes = macro.events
            entry.keystroke_loop = macro.loop
            entry.stop_on_input = macro.stop_on_input
            save_config(cfg)
        return rec
    exe = _choose_exe(prefix, parent)
    if exe is not None:
        _add_prefix_exe_entry(cfg, rec, exe, interactive=False)
    return rec


def find_prefix_executables(prefix: Path) -> list[Path]:
    drive = prefix / "drive_c"
    if not drive.is_dir():
        return []
    roots = [
        drive / "Program Files",
        drive / "Program Files (x86)",
        drive / "users",
    ]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            iterator = root.rglob("*.exe")
        except OSError:
            continue
        for path in iterator:
            if not path.is_file():
                continue
            parts = {part.lower() for part in path.relative_to(drive).parts}
            if parts & _SKIP_EXE_DIRS:
                continue
            if path.name.lower() in _SKIP_EXE_NAMES:
                continue
            found.append(path)
            if len(found) >= 400:
                found.sort(key=lambda item: str(item).lower())
                return found
    found.sort(key=lambda item: str(item).lower())
    return found


def unique_prefix_name(cfg: AppConfig, base: str) -> str:
    return _unique_prefix_name(cfg, base)


def _choose_exe(prefix: Path, parent: QWidget | None) -> Path | None:
    dialog = ExePickDialog(prefix, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.selected_exe()


def _add_prefix_exe_entry(
    cfg: AppConfig,
    rec: ManagedPrefix,
    exe: Path,
    *,
    interactive: bool,
    notes: str = "",
    display_name: str = "",
) -> ScreensaverEntry:
    for existing in cfg.screensavers:
        if existing.exe_path and Path(existing.exe_path).expanduser().resolve() == exe.resolve():
            existing.wine_prefix = rec.name
            existing.wine_runner = rec.runner_id if rec.runner_id != SYSTEM_ID else ""
            existing.launch_kind = LAUNCH_PREFIX_EXE
            if interactive:
                existing.interactive = True
            if notes:
                existing.notes = notes
            if display_name:
                existing.name = display_name
            save_config(cfg)
            return existing
    name = display_name or (friendly_name(exe) if exe.suffix.lower() == ".exe" else exe.stem)
    entry = ScreensaverEntry(
        id=str(uuid.uuid4()),
        name=name,
        filename=exe.name,
        enabled=True,
        notes=notes or f"Runs from imported prefix {rec.name}.",
        wine_runner=rec.runner_id if rec.runner_id != SYSTEM_ID else "",
        wine_prefix=rec.name,
        interactive=interactive,
        launch_kind=LAUNCH_PREFIX_EXE,
        exe_path=str(exe),
    )
    cfg.screensavers.append(entry)
    if not cfg.selected_id:
        cfg.selected_id = entry.id
    save_config(cfg)
    return entry


def _ensure_steam_prefix(cfg: AppConfig, game: steam.SteamGame) -> str:
    assert game.prefix_path is not None
    resolved = str(game.prefix_path)
    for item in cfg.managed_prefixes:
        if item.steam_appid == game.appid or item.source_path == resolved:
            item.origin = ORIGIN_STEAM
            item.source_path = resolved
            item.steam_appid = game.appid
            if game.proton_runner_id:
                item.runner_id = game.proton_runner_id
            save_config(cfg)
            return item.name
    rec = ManagedPrefix(
        name=_unique_prefix_name(cfg, game.name),
        runner_id=game.proton_runner_id or SYSTEM_ID,
        origin=ORIGIN_STEAM,
        source_path=resolved,
        steam_appid=game.appid,
    )
    cfg.managed_prefixes.append(rec)
    save_config(cfg)
    return rec.name


def _entry_for_steam(cfg: AppConfig, appid: str) -> ScreensaverEntry | None:
    for entry in cfg.screensavers:
        if entry.steam_appid == appid or entry.filename == f"steam-{appid}":
            return entry
    return None


def _resolve_prefix_for_exe(
    exe: Path,
    game: heroic.HeroicGame | None,
    parent: QWidget | None,
) -> tuple[Path, str, str]:
    runner = SYSTEM_ID
    label = exe.stem
    if game is not None:
        runner = game.runner_id() or SYSTEM_ID
        label = game.name
        prefix = game.wine_prefix
        if prefix is not None:
            root = paths.wine_prefix_root(prefix)
            if (root / "system.reg").is_file():
                runner = game.runner_id() or steam.proton_runner_for_prefix(root) or SYSTEM_ID
                return root, runner, game.name
    nested = _prefix_containing_exe(exe)
    if nested is not None:
        if runner == SYSTEM_ID:
            runner = steam.proton_runner_for_prefix(nested) or SYSTEM_ID
        return nested, runner, label if game is not None else nested.name
    start = Path.home()
    install = heroic.default_install_dir()
    if install is not None:
        prefixes = install / "Prefixes" / "default"
        start = prefixes if prefixes.is_dir() else install
    title = f'Select the Wine prefix for {label}' if game is not None else "Select the Wine prefix for this program"
    chosen = QFileDialog.getExistingDirectory(parent, title, str(start))
    if not chosen:
        raise FileNotFoundError("No Wine prefix was selected.")
    root = paths.wine_prefix_root(Path(chosen))
    if not (root / "system.reg").is_file():
        raise FileNotFoundError(f"{chosen} does not look like a Wine prefix (no system.reg).")
    if runner == SYSTEM_ID:
        runner = steam.proton_runner_for_prefix(root) or SYSTEM_ID
    return root, runner, label if game is not None else root.name


def _ensure_imported_prefix(cfg: AppConfig, prefix: Path, runner_id: str, label: str) -> ManagedPrefix:
    if paths.is_under_data_dir(prefix):
        raise ValueError(
            "That folder is already under SCR Saver's data directory. "
            "It is listed automatically when it contains a Wine prefix."
        )
    resolved = prefix.expanduser().resolve()
    for item in cfg.managed_prefixes:
        source = Path(item.source_path).expanduser() if item.source_path else None
        if source is not None:
            try:
                if source.resolve() == resolved:
                    if runner_id and runner_id != SYSTEM_ID:
                        item.runner_id = runner_id
                    save_config(cfg)
                    return item
            except OSError:
                pass
    rec = ManagedPrefix(
        name=_unique_prefix_name(cfg, label if label.lower() not in ("pfx", "wineprefix") else prefix.name),
        runner_id=runner_id or SYSTEM_ID,
        origin=ORIGIN_IMPORTED,
        source_path=str(resolved),
    )
    cfg.managed_prefixes.append(rec)
    save_config(cfg)
    return rec


def _prefix_containing_exe(exe: Path) -> Path | None:
    try:
        current = exe.parent.resolve()
    except OSError:
        return None
    for folder in [current, *current.parents]:
        if (folder / "system.reg").is_file() and (folder / "drive_c").is_dir():
            return folder
        if folder.name.lower() == "drive_c" and (folder.parent / "system.reg").is_file():
            return folder.parent
    return None


def _ask_autoplay(parent: QWidget | None, exe_name: str) -> bool:
    answer = QMessageBox.question(
        parent,
        "Import program",
        f'Does "{exe_name}" have an autoplay feature?\n\n'
        "Yes records the keys and mouse input that start autoplay, then replays "
        "them when the program runs as a screensaver or interactive demo.\n\n"
        "No adds the program to the screensaver list as it is.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def _import_notes(rec: ManagedPrefix, game: heroic.HeroicGame | None) -> str:
    if game is None:
        return f"Runs from imported prefix {rec.name}."
    runner = game.wine_name or rec.runner_id or "Wine"
    return f"Heroic: {game.name}. Uses {runner} and prefix {rec.name}."


def _unique_prefix_name(cfg: AppConfig, base: str) -> str:
    cleaned = paths.sanitize_prefix_name(base) or "imported"
    if cleaned.lower() in ("pfx", "wineprefix", "drive_c"):
        cleaned = "imported"
    taken = {item.name.lower() for item in cfg.managed_prefixes}
    taken.add("wineprefix")
    taken.update(paths.RESERVED_PREFIX_NAMES)
    if cleaned.lower() not in taken:
        return cleaned
    index = 2
    while True:
        candidate = f"{cleaned}-{index}"
        if candidate.lower() not in taken:
            return candidate
        index += 1


def _display_rel(prefix: Path, path: Path) -> str:
    try:
        return str(path.relative_to(prefix / "drive_c"))
    except ValueError:
        return str(path)


def _record_after_launch(
    parent: QWidget | None,
    title: str,
    *,
    idle,
    on_start,
    on_stop,
    loop_default: bool = False,
    stop_on_input_default: bool = True,
):
    paused = False
    if idle is not None:
        try:
            idle.pause()
            paused = True
        except Exception:
            paused = False
    try:
        return record_autoplay_keys(
            parent,
            title,
            on_start=on_start,
            on_stop=on_stop,
            loop_default=loop_default,
            stop_on_input_default=stop_on_input_default,
        )
    finally:
        if paused and idle is not None:
            try:
                idle.resume()
            except Exception:
                pass
