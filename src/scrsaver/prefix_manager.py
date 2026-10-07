from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import heroic, paths, steam, wineutil
from .config import AppConfig, ManagedPrefix, save_config
from .cpu_limit import CELERON_PERCENT, WIN31_MHZ, estimated_mhz, host_max_mhz, win31_quota_percent
from .prefix_import import SteamGameDialog, import_program_exe, import_steam_game
from .runners import SYSTEM_ID, get_runner, runner_display

log = logging.getLogger(__name__)
WIN31_PRESET = 0


class PrefixManagerDialog(QDialog):
    def __init__(
        self,
        cfg: AppConfig,
        parent: QWidget | None = None,
        *,
        library_changed=None,
        idle=None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._library_changed = library_changed
        self._idle = idle
        self._loading = False
        self.setWindowTitle("Manage Wine prefixes")
        self.setMinimumSize(720, 420)
        self._build()
        self._reload_list()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("Prefixes"))
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_prefix_menu)
        left.addWidget(self.list, 1)
        import_row = QHBoxLayout()
        self.import_btn = QPushButton("Import prefix…")
        self.import_btn.setToolTip(
            "Pick a Windows .exe. Heroic games under ~/Games/Heroic keep their "
            "Wine/Proton bottle and runner. Other programs can still point at an existing prefix."
        )
        self.import_btn.clicked.connect(self._import_prefix)
        self.steam_btn = QPushButton("Import Steam game…")
        self.steam_btn.setToolTip(
            "Scan installed Steam games, add the Proton bottle when there is one, "
            "and add the game to the screensaver list. It launches through Steam."
        )
        self.steam_btn.clicked.connect(self._import_steam)
        import_row.addWidget(self.import_btn)
        import_row.addWidget(self.steam_btn)
        left.addLayout(import_row)
        root.addLayout(left, 2)

        right = QVBoxLayout()
        self.title = QLabel("Select a prefix")
        title_font = self.title.font()
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        right.addWidget(self.title)
        right.addWidget(self.detail)

        speed = QGroupBox("Clock speed")
        speed_form = QFormLayout(speed)
        host = host_max_mhz()
        win31 = win31_quota_percent()
        hint = QLabel(
            "Windows 98 OpenGL savers such as 3D Maze (ssmaze) run as fast as the CPU "
            "will go — they have no frame cap. 3D Maze would not have been a normal "
            "Windows 3.1 program (it needs Win95-era OpenGL), but a typical 3.1 box "
            f"was a 386 around 25–33 MHz. This machine's max is about {host:.0f} MHz, so "
            f"the slowest setting is ~{WIN31_MHZ:.0f} MHz (≈{win31:.1f}% of one core). "
            "Limits apply only to the highlighted bottle."
        )
        hint.setWordWrap(True)
        speed_form.addRow(hint)
        self.preset = QComboBox()
        self.preset.addItem("Full speed", 100)
        self.preset.addItem("Noticeably slower (half a modern core)", 50)
        self.preset.addItem("Celeron-era (about a late-90s Gateway)", CELERON_PERCENT)
        self.preset.addItem("Windows 3.1-era (≈33 MHz 386)", WIN31_PRESET)
        self.preset.addItem("Custom", -1)
        self.preset.currentIndexChanged.connect(self._on_preset)
        speed_form.addRow("Feel", self.preset)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, 100)
        self.slider.setValue(100)
        self.slider.valueChanged.connect(self._on_slider)
        self.slider_label = QLabel("100% of one CPU")
        row = QHBoxLayout()
        row.addWidget(self.slider, 1)
        row.addWidget(self.slider_label)
        speed_form.addRow("CPU quota", row)
        self.single_core = QCheckBox("Pretend this is a single-core PC (Windows 98-style)")
        self.single_core.toggled.connect(self._save_speed)
        speed_form.addRow(self.single_core)
        fps_note = QLabel(
            "A smooth frame cap for every saver is in Additional Settings. "
            "Use that for 3D Maze; CPU quota here is still per bottle."
        )
        fps_note.setWordWrap(True)
        speed_form.addRow(fps_note)
        right.addWidget(speed)

        tools = QGroupBox("Wine tools")
        tools_l = QHBoxLayout(tools)
        self.winecfg_btn = QPushButton("Wine configuration…")
        self.winecfg_btn.clicked.connect(self._winecfg)
        self.tricks_btn = QPushButton("Winetricks…")
        self.tricks_btn.clicked.connect(self._winetricks)
        if not shutil.which("winetricks"):
            self.tricks_btn.setEnabled(False)
        self.uninst_btn = QPushButton("Wine uninstaller…")
        self.uninst_btn.clicked.connect(self._uninstaller)
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(self._open_folder)
        tools_l.addWidget(self.winecfg_btn)
        tools_l.addWidget(self.tricks_btn)
        tools_l.addWidget(self.uninst_btn)
        tools_l.addWidget(self.folder_btn)
        right.addWidget(tools)

        self.delete_btn = QPushButton("Delete this prefix…")
        self.delete_btn.clicked.connect(self._remove_prefix)
        right.addWidget(self.delete_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        hint = QLabel(
            "Right-click a prefix to remove it from this list. "
            "Only bottles created under ~/.local/share/scrsaver/ can be deleted from disk. "
            "Imported and Steam bottles are delisted. The default bottle stays."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        right.addWidget(hint)

        note = QLabel(
            "Install-script import and export for each prefix is planned for October."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        right.addWidget(note)
        right.addStretch(1)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        right.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        root.addLayout(right, 3)

    def _reload_list(self, select: str | None = None) -> None:
        current = select
        if current is None and self.list.currentItem() is not None:
            current = str(self.list.currentItem().data(Qt.ItemDataRole.UserRole) or "")
        self.list.blockSignals(True)
        self.list.clear()
        for label, name, runner_id in wineutil.available_prefixes(self.cfg):
            key = name or "wineprefix"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.UserRole + 1, runner_id)
            self.list.addItem(item)
            if key == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self._on_select()

    def _current_name(self) -> str:
        item = self.list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _current_runner(self) -> str:
        item = self.list.currentItem()
        if item is None:
            return SYSTEM_ID
        return str(item.data(Qt.ItemDataRole.UserRole + 1) or SYSTEM_ID)

    def _is_default(self) -> bool:
        return self._current_name() in ("", "wineprefix")

    def _record(self) -> ManagedPrefix:
        name = self._current_name() or "wineprefix"
        for item in self.cfg.managed_prefixes:
            if item.name == name:
                if not item.runner_id:
                    item.runner_id = self._current_runner()
                return item
        rec = ManagedPrefix(name=name, runner_id=self._current_runner())
        self.cfg.managed_prefixes.append(rec)
        return rec

    def _session(self):
        name = self._current_name()
        runner = self._current_runner()
        rec = self._record()
        runner = rec.runner_id or runner or SYSTEM_ID
        if not name or name == "wineprefix":
            runner_obj = get_runner(runner)
            if runner_obj is None:
                raise FileNotFoundError("Wine was not found.")
            return wineutil.WineSession(runner=runner_obj, prefix=paths.wineprefix_dir())
        return wineutil.session_for_named_prefix(name, runner)

    def _current_record(self) -> ManagedPrefix | None:
        name = self._current_name()
        if not name:
            return None
        for item in self.cfg.managed_prefixes:
            if item.name == name or (name == "wineprefix" and item.name in ("", "wineprefix")):
                return item
        return None

    def _can_delete_folder(self) -> bool:
        if self._is_default():
            return False
        rec = self._current_record()
        if rec is not None and rec.is_imported():
            return False
        name = self._current_name()
        folder = wineutil.prefix_path_for_name(name, self.cfg)
        return paths.is_under_data_dir(folder)

    def _on_select(self) -> None:
        name = self._current_name()
        if not name:
            return
        self._loading = True
        rec = self._record()
        path = wineutil.prefix_path_for_name(name, self.cfg) if not self._is_default() else paths.wineprefix_dir()
        users = [
            entry.name
            for entry in self.cfg.screensavers
            if (entry.wine_prefix or "wineprefix") == name
            or (self._is_default() and not entry.wine_prefix)
        ]
        used = f"{len(users)} screensaver(s) in the library point here."
        if users[:4]:
            used += " " + ", ".join(users[:4])
            if len(users) > 4:
                used += ", …"
        self.title.setText("Default bottle" if self._is_default() else name)
        self.detail.setText(
            f"{path}\nWine/Proton: {runner_display(rec.runner_id or self._current_runner())}\n{used}"
        )
        percent = float(rec.cpu_percent)
        win31 = win31_quota_percent()
        self.slider.blockSignals(True)
        if abs(percent - win31) <= 0.15 or percent < 1:
            self.slider.setValue(1)
            preset = WIN31_PRESET
        else:
            self.slider.setValue(max(1, int(round(percent))))
            preset = int(self.preset.findData(int(round(percent))))
        self.slider.blockSignals(False)
        self.preset.blockSignals(True)
        idx = self.preset.findData(preset if percent < 1.5 and abs(percent - win31) <= 0.15 else int(round(percent)))
        if abs(percent - win31) <= 0.15:
            idx = self.preset.findData(WIN31_PRESET)
        self.preset.setCurrentIndex(idx if idx >= 0 else self.preset.findData(-1))
        self.preset.blockSignals(False)
        self.single_core.blockSignals(True)
        self.single_core.setChecked(rec.single_core)
        self.single_core.blockSignals(False)
        self._update_slider_label(percent)
        if self._is_default():
            self.delete_btn.setText("Delete this prefix…")
            self.delete_btn.setEnabled(False)
            self.delete_btn.setToolTip("The default bottle cannot be removed from this list.")
        elif self._can_delete_folder():
            self.delete_btn.setText("Delete this prefix…")
            self.delete_btn.setEnabled(True)
            self.delete_btn.setToolTip("Stops Wine for this bottle and deletes its folder.")
        else:
            self.delete_btn.setText("Remove from list")
            self.delete_btn.setEnabled(True)
            self.delete_btn.setToolTip(
                "Takes this imported bottle off the list. The original folder is left in place."
            )
        self._loading = False

    def _on_preset(self) -> None:
        value = int(self.preset.currentData() or -1)
        if value < 0:
            return
        win31 = win31_quota_percent()
        self.slider.blockSignals(True)
        if value == WIN31_PRESET:
            self.slider.setValue(1)
            display = win31
            self.single_core.blockSignals(True)
            self.single_core.setChecked(True)
            self.single_core.blockSignals(False)
        else:
            self.slider.setValue(value)
            display = float(value)
            if value == CELERON_PERCENT:
                self.single_core.blockSignals(True)
                self.single_core.setChecked(True)
                self.single_core.blockSignals(False)
        self.slider.blockSignals(False)
        self._update_slider_label(display)
        self._save_speed()

    def _on_slider(self, value: int) -> None:
        self._update_slider_label(float(value))
        preset = self.preset.findData(value)
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(preset if preset >= 0 else self.preset.findData(-1))
        self.preset.blockSignals(False)
        self._save_speed()

    def _update_slider_label(self, percent: float) -> None:
        mhz = estimated_mhz(percent)
        extra = ""
        if abs(percent - win31_quota_percent()) <= 0.15:
            extra = "  ·  Windows 3.1-era"
        elif int(round(percent)) == CELERON_PERCENT:
            extra = "  ·  Celeron-era"
        elif percent >= 100:
            extra = "  ·  full speed"
        if percent < 1:
            quota = f"{percent:.1f}%"
        else:
            quota = f"{int(round(percent))}%"
        self.slider_label.setText(f"{quota} of one CPU  ·  ≈ {mhz:.0f} MHz{extra}")

    def _save_speed(self) -> None:
        if self._loading or not self._current_name():
            return
        rec = self._record()
        if int(self.preset.currentData() or -1) == WIN31_PRESET:
            rec.cpu_percent = win31_quota_percent()
            rec.single_core = True
        else:
            rec.cpu_percent = float(self.slider.value())
            rec.single_core = self.single_core.isChecked()
        save_config(self.cfg)

    def _winecfg(self) -> None:
        try:
            wineutil.open_winecfg(self._session())
        except Exception as exc:
            QMessageBox.warning(self, "Wine configuration", str(exc))

    def _winetricks(self) -> None:
        try:
            wineutil.open_winetricks(self._session())
        except Exception as exc:
            QMessageBox.warning(self, "Winetricks", str(exc))

    def _uninstaller(self) -> None:
        try:
            wineutil.launch_wine_uninstaller_gui(self._session())
        except Exception as exc:
            QMessageBox.warning(self, "Wine uninstaller", str(exc))

    def _open_folder(self) -> None:
        try:
            session = self._session()
        except Exception as exc:
            QMessageBox.warning(self, "Prefix folder", str(exc))
            return
        session.prefix.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(session.prefix)))

    def _on_prefix_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        menu = QMenu(self)
        action = menu.addAction("Remove prefix")
        if self._is_default():
            action.setEnabled(False)
            action.setToolTip("The default bottle cannot be delisted or deleted.")
        else:
            action.triggered.connect(self._remove_prefix)
        menu.exec(self.list.mapToGlobal(pos))

    def _import_prefix(self) -> None:
        start = heroic.default_install_dir() or Path.home()
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            "Select a Windows program to import",
            str(start),
            "Windows programs (*.exe *.EXE);;All files (*)",
        )
        if not chosen:
            return
        exe = Path(chosen)
        skip_names = {
            "unins000.exe",
            "uninstall.exe",
            "uninst.exe",
            "unwise.exe",
        }
        lowered = exe.name.lower()
        if lowered.startswith("vcredist") or lowered in skip_names:
            answer = QMessageBox.question(
                self,
                "Import prefix",
                f'"{exe.name}" looks like an installer or redistributable, not the game itself.\n\n'
                "Import it anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            rec = import_program_exe(self.cfg, exe, self, idle=self._idle)
        except (OSError, ValueError, FileNotFoundError) as exc:
            QMessageBox.warning(self, "Import prefix", str(exc))
            return
        self._notify_library()
        self._reload_list(select=rec.name)

    def _import_steam(self) -> None:
        if steam.steam_binary() is None:
            QMessageBox.warning(
                self,
                "Import Steam game",
                "Steam was not found. Install Steam or add it to PATH.",
            )
            return
        try:
            games = steam.list_installed_games()
        except Exception as exc:
            QMessageBox.warning(self, "Import Steam game", str(exc))
            return
        if not games:
            QMessageBox.information(
                self,
                "Import Steam game",
                "No installed Steam games were found. Proton, Steamworks, and "
                "Steam Linux Runtime tool packages are skipped.",
            )
            return
        picker = SteamGameDialog(games, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        game = picker.selected_game()
        if game is None:
            return
        try:
            entry = import_steam_game(self.cfg, game, self, idle=self._idle)
        except Exception as exc:
            QMessageBox.warning(self, "Import Steam game", str(exc))
            return
        self._notify_library()
        self._reload_list(select=entry.wine_prefix or None)
        extra = ""
        if entry.keystrokes:
            looping = " looping until you exit" if entry.keystroke_loop else ""
            extra = (
                f" {len(entry.keystrokes)} input event(s) will replay when it starts as a demo"
                f"{looping}."
            )
        elif game.native:
            extra = " This is a native Linux build, so there is no Proton prefix to list."
        QMessageBox.information(
            self,
            "Import Steam game",
            f'"{entry.name}" is in the screensaver list and will launch through Steam '
            f"as an interactive demo.{extra}",
        )

    def _notify_library(self) -> None:
        if self._library_changed is not None:
            self._library_changed()

    def _remove_prefix(self) -> None:
        if self._is_default():
            QMessageBox.information(
                self,
                "Remove prefix",
                "The default bottle cannot be delisted or deleted from here. It usually holds many programs.",
            )
            return
        name = self._current_name()
        delete_folder = self._can_delete_folder()
        users = [
            entry.name
            for entry in self.cfg.screensavers
            if (entry.wine_prefix or "") == name
        ]
        extra = ""
        if users:
            extra = (
                f"\n\n{len(users)} screensaver(s) still point at this prefix "
                f"({', '.join(users[:5])}{'…' if len(users) > 5 else ''}). "
                "They stay in the library."
            )
        if delete_folder:
            title = "Delete prefix"
            prompt = f'Delete "{name}" and all Windows programs inside it?{extra}'
        else:
            title = "Remove prefix"
            prompt = (
                f'Remove "{name}" from this list? The original folder is left in place.{extra}'
            )
        answer = QMessageBox.question(self, title, prompt)
        if answer != QMessageBox.StandardButton.Yes:
            return
        folder: Path | None = None
        if delete_folder:
            try:
                session = self._session()
                wineutil.kill_prefix(session)
                folder = session.prefix
            except Exception as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            if folder.resolve() == paths.wineprefix_dir().resolve():
                QMessageBox.warning(self, title, "Refusing to delete the default bottle.")
                return
            if not paths.is_under_data_dir(folder):
                QMessageBox.warning(
                    self,
                    title,
                    "Refusing to delete a folder outside ~/.local/share/scrsaver/.",
                )
                return
            shutil.rmtree(folder, ignore_errors=True)
        self.cfg.managed_prefixes = [item for item in self.cfg.managed_prefixes if item.name != name]
        save_config(self.cfg)
        self._reload_list()
