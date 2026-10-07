from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
    QScreen,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import paths, wineutil
from .config import AppConfig, save_config
from .daemon import SaverController
from .library import import_any, others_sharing_prefix, remove_screensaver
from .pe import NotScreensaverError
from .autostart import install_application_launcher, service_is_enabled, set_autostart
from .keys import interactive_notice
from .runners import SYSTEM_ID, get_runner, grouped_runners, runner_display
from .settings import AdditionalSettingsDialog

log = logging.getLogger(__name__)




def load_app_icon() -> QIcon:
    icon_file = paths.icon_path()
    if icon_file.is_file():
        icon = QIcon(str(icon_file))
        if not icon.isNull():
            return icon
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#1b1f2a"))
    painter.setPen(QColor("#6ec6ff"))
    painter.drawRoundedRect(12, 18, 104, 78, 10, 10)
    painter.setBrush(QColor("#0b1020"))
    painter.drawRect(22, 28, 84, 52)
    painter.setPen(QColor("#8be9fd"))
    painter.drawText(pixmap.rect().adjusted(0, -8, 0, 0), Qt.AlignmentFlag.AlignCenter, "SCR")
    painter.setBrush(QColor("#3a4155"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRect(48, 96, 32, 8)
    painter.drawRoundedRect(36, 104, 56, 8, 4, 4)
    painter.end()
    return QIcon(pixmap)


class WineSetupThread(QThread):
    ok = pyqtSignal()
    fail = pyqtSignal(str)

    def __init__(self, parent=None, session=None) -> None:
        super().__init__(parent)
        self._session = session

    def run(self) -> None:
        try:
            wineutil.setup_prefix(self._session)
        except Exception as exc:
            self.fail.emit(str(exc))
            return
        self.ok.emit()


class ScreensaverList(QListWidget):
    files_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        files = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
        files = [path for path in files if path.exists()]
        if files:
            self.files_dropped.emit(files)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, controller: SaverController, show_window: bool = True) -> None:
        super().__init__()
        self.controller = controller
        self.cfg: AppConfig = controller.cfg
        self._setup_thread: WineSetupThread | None = None
        self._pending_play: str | None = None
        self._pending_configure = False
        self._pending_tool: str | None = None
        self._really_quit = False
        self._settings_dialog: AdditionalSettingsDialog | None = None

        self.setWindowTitle("SCR Saver")
        self.setWindowIcon(load_app_icon())
        self.resize(560, 560)

        self._build()
        self._build_tray()
        self._reload_list()
        self._sync_controls()

        self.controller.state_changed.connect(self._sync_status)
        self.controller.error.connect(self._show_error)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._sync_status)
        self._status_timer.start()

        self.controller.start_watching()
        if not wineutil.prefix_ready():
            try:
                wineutil.wine_binary()
            except FileNotFoundError:
                pass
            else:
                self._setup_thread = WineSetupThread(self)
                self._setup_thread.fail.connect(lambda message: log.warning("Wine setup: %s", message))
                self._setup_thread.start()
        if show_window:
            self.show()
        elif not QSystemTrayIcon.isSystemTrayAvailable():
            self.show()

    def _build(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("SCR Saver")
        title_font = title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QLabel("Import Windows .scr files and play them when this computer is idle.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: palette(mid);")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        idle_box = QGroupBox("When idle")
        idle_form = QFormLayout(idle_box)

        self.enabled_box = QCheckBox("Run a screensaver after idle timeout")
        self.enabled_box.toggled.connect(self._on_enabled_toggled)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 180)
        self.timeout_spin.setSuffix(" minutes")
        self.timeout_spin.valueChanged.connect(self._on_timeout_changed)

        self.display_combo = QComboBox()
        self._populate_displays()
        self.display_combo.currentIndexChanged.connect(self._on_display_changed)
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._on_screens_changed)
            app.screenRemoved.connect(self._on_screens_changed)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Random from enabled", "random")
        self.mode_combo.addItem("Always the selected one", "selected")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.autostart_box = QCheckBox("Start SCR Saver when I log in (systemd user service)")
        self.autostart_box.toggled.connect(self._on_autostart_toggled)

        idle_form.addRow(self.enabled_box)
        idle_form.addRow("Timeout", self.timeout_spin)
        idle_form.addRow("Display", self.display_combo)
        idle_form.addRow("Which file", self.mode_combo)
        idle_form.addRow(self.autostart_box)
        layout.addWidget(idle_box)

        lib_box = QGroupBox("Screensavers")
        lib_layout = QVBoxLayout(lib_box)
        hint = QLabel("Drop .scr files here, or use Import. Uncheck a file to keep it without playing it.")
        hint.setWordWrap(True)
        self.list = ScreensaverList()
        self.list.files_dropped.connect(self.import_paths)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemDoubleClicked.connect(lambda *_: self._test_or_stop())
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_list_menu)

        buttons = QHBoxLayout()
        self.import_btn = QPushButton("Import")
        self.import_btn.clicked.connect(self._choose_import)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(self._test_or_stop)
        self.config_btn = QPushButton("Configure")
        self.config_btn.clicked.connect(self._configure_selected)
        buttons.addWidget(self.import_btn)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.config_btn)
        buttons.addWidget(self.test_btn)

        lib_layout.addWidget(hint)
        lib_layout.addWidget(self.list, 1)
        lib_layout.addLayout(buttons)
        layout.addWidget(lib_box, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.setCentralWidget(root)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(load_app_icon(), self)
        self.tray.setToolTip("SCR Saver")
        menu = QMenu()
        show_action = QAction("Open SCR Saver", self)
        show_action.triggered.connect(self._show_from_tray)
        self.tray_toggle = QAction("Run when idle", self)
        self.tray_toggle.setCheckable(True)
        self.tray_toggle.triggered.connect(self._on_tray_toggle)
        test_action = QAction("Test now", self)
        test_action.triggered.connect(self._test_or_stop)
        extra_action = QAction("Additional Settings", self)
        extra_action.triggered.connect(self._open_additional_settings)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show_action)
        menu.addAction(self.tray_toggle)
        menu.addAction(test_action)
        self.tray_display_menu = menu.addMenu("Display")
        menu.addSeparator()
        menu.addAction(extra_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        menu.aboutToShow.connect(self._rebuild_tray_display_menu)
        self._rebuild_tray_display_menu()
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _reload_list(self) -> None:
        self.list.blockSignals(True)
        current_id = self._selected_id()
        self.list.clear()
        for entry in self.cfg.screensavers:
            exists = entry.is_playable()
            label = entry.name
            extra = []
            if entry.is_steam():
                extra.append("Steam")
                extra.append(f"app {entry.steam_appid}")
            elif entry.is_prefix_exe():
                extra.append(entry.filename)
            else:
                if entry.arch:
                    extra.append(entry.arch)
                extra.append(entry.filename)
            if entry.notes and not entry.is_steam() and not entry.is_prefix_exe():
                extra.append("needs extra files")
            if entry.keystrokes:
                extra.append("looping autoplay" if entry.keystroke_loop else "autoplay")
            if not exists:
                extra.append("missing")
            item = QListWidgetItem(f"{label}  ({', '.join(extra)})")
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            if entry.is_steam():
                tooltip = f"Steam app {entry.steam_appid}"
            elif entry.exe_path:
                tooltip = entry.exe_path
            else:
                tooltip = str(entry.linux_path())
            tooltip += f"\nWine/Proton: {runner_display(entry.wine_runner)}"
            if entry.interactive:
                key = self.cfg.interactive_exit_key or "Escape"
                tooltip += f"\nInteractive demo ({key} to exit)"
            if entry.keystrokes:
                looping = " (looping until exit)" if entry.keystroke_loop else ""
                halt = "; stops when you use the keyboard or mouse" if entry.stop_on_input else ""
                tooltip += f"\n{len(entry.keystrokes)} recorded autoplay event(s){looping}{halt}"
            if entry.notes:
                tooltip = entry.notes + "\n\n" + tooltip
            item.setToolTip(tooltip)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked)
            if not exists:
                item.setForeground(QColor("#c05050"))
            self.list.addItem(item)
            if entry.id == (current_id or self.cfg.selected_id):
                item.setSelected(True)
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._sync_status()

    def _selected_id(self) -> str:
        item = self.list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _sync_controls(self) -> None:
        widgets = [
            self.enabled_box,
            self.timeout_spin,
            self.mode_combo,
            self.autostart_box,
        ]
        for widget in widgets:
            widget.blockSignals(True)
        self.enabled_box.setChecked(self.cfg.enabled)
        self.timeout_spin.setValue(max(1, round(self.cfg.timeout_seconds / 60)))
        self._populate_displays()
        index = self.mode_combo.findData(self.cfg.play_mode)
        self.mode_combo.setCurrentIndex(max(0, index))
        self.cfg.autostart = service_is_enabled()
        self.autostart_box.setChecked(self.cfg.autostart)
        self.tray_toggle.setChecked(self.cfg.enabled)
        for widget in widgets:
            widget.blockSignals(False)
        self._sync_status()

    def _sync_status(self) -> None:
        playing = self.controller.player.is_playing()
        self.test_btn.setText("Stop" if playing else "Test")
        self.tray_toggle.setChecked(self.cfg.enabled)
        if playing:
            name = self.controller.player.playing_name() or "screensaver"
            if self.controller.player.is_interactive():
                key = self.cfg.interactive_exit_key or "Escape"
                self.status.setText(f"Playing {name}. Press {key} to exit.")
                self.tray.setToolTip(f"SCR Saver — playing {name} ({key} to exit)")
            else:
                self.status.setText(f"Playing {name}. Move the mouse or press a key to stop.")
                self.tray.setToolTip(f"SCR Saver — playing {name}")
            return
        count = len(self.cfg.enabled_entries())
        if not self.cfg.enabled:
            self.status.setText("Idle watching is off. Enable it above when you want .scr files to run on idle.")
            self.tray.setToolTip("SCR Saver — idle watching off")
            return
        if count == 0:
            self.status.setText("Idle watching is on, but no enabled screensaver files are in the library.")
            self.tray.setToolTip("SCR Saver — no files")
            return
        idle = int(self.controller.idle.idle_seconds())
        remaining = int(self.controller.idle.seconds_until_idle())
        self.status.setText(
            f"Watching for idle. Last input { _fmt_seconds(idle) } ago · starts in { _fmt_seconds(remaining) }."
        )
        self.tray.setToolTip(f"SCR Saver — starts in {_fmt_seconds(remaining)}")

    def _on_enabled_toggled(self, checked: bool) -> None:
        self.cfg.enabled = checked
        self.controller.apply_config()
        self.tray_toggle.setChecked(checked)

    def _on_timeout_changed(self, minutes: int) -> None:
        self.cfg.timeout_seconds = max(10, int(minutes) * 60)
        self.controller.apply_config()

    def _display_choices(self) -> list[tuple[str, str]]:
        choices: list[tuple[str, str]] = [
            ("All screens", "all"),
            ("Primary screen only", "primary"),
        ]
        app = QGuiApplication.instance()
        primary = app.primaryScreen() if app is not None else None
        screens = list(app.screens()) if app is not None else []
        names: list[str] = []
        for screen in screens:
            name = screen.name() or "Unknown"
            names.append(name)
            geo = screen.geometry()
            label = f"{name}  ({geo.width()}×{geo.height()})"
            if primary is not None and screen is primary:
                label += "  — primary"
            choices.append((label, f"output:{name}"))
        current = self.cfg.display_mode
        if current.startswith("output:"):
            wanted = current.split(":", 1)[1]
            if wanted not in names:
                choices.append((f"{wanted}  (disconnected)", current))
        return choices

    def _populate_displays(self) -> None:
        current = self.cfg.display_mode
        self.display_combo.blockSignals(True)
        self.display_combo.clear()
        for label, data in self._display_choices():
            self.display_combo.addItem(label, data)
        index = self.display_combo.findData(current)
        self.display_combo.setCurrentIndex(max(0, index))
        self.display_combo.blockSignals(False)

    def _rebuild_tray_display_menu(self) -> None:
        if not hasattr(self, "tray_display_menu"):
            return
        self.tray_display_menu.clear()
        group = QActionGroup(self.tray_display_menu)
        group.setExclusive(True)
        current = self.cfg.display_mode
        for label, data in self._display_choices():
            action = self.tray_display_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(data == current)
            group.addAction(action)
            action.triggered.connect(
                lambda checked, mode=data: checked and self._set_display_mode(mode)
            )

    def _set_display_mode(self, mode: str) -> None:
        if not mode:
            mode = "all"
        if self.cfg.display_mode == mode:
            return
        self.cfg.display_mode = mode
        self.controller.apply_config()
        self._populate_displays()
        self._rebuild_tray_display_menu()
        self._sync_status()

    def _on_screens_changed(self, _screen: QScreen | None = None) -> None:
        self._populate_displays()
        self._rebuild_tray_display_menu()

    def _on_display_changed(self) -> None:
        self._set_display_mode(str(self.display_combo.currentData() or "all"))

    def _on_mode_changed(self) -> None:
        self.cfg.play_mode = str(self.mode_combo.currentData() or "random")
        self.controller.apply_config()

    def _on_autostart_toggled(self, checked: bool) -> None:
        self.cfg.autostart = checked
        try:
            set_autostart(checked)
            install_application_launcher()
        except Exception as exc:
            self._show_error(f"Could not update autostart: {exc}")
            self.autostart_box.blockSignals(True)
            self.autostart_box.setChecked(service_is_enabled())
            self.autostart_box.blockSignals(False)
            return
        self.controller.apply_config()

    def _on_tray_toggle(self, checked: bool) -> None:
        self.enabled_box.setChecked(checked)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        sid = str(item.data(Qt.ItemDataRole.UserRole) or "")
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        entry.enabled = item.checkState() == Qt.CheckState.Checked
        save_config(self.cfg)
        self._sync_status()

    def _on_current_changed(self) -> None:
        sid = self._selected_id()
        if sid:
            self.cfg.selected_id = sid
            save_config(self.cfg)

    def _choose_import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Windows screensaver",
            str(Path.home() / "Downloads"),
            "Screensavers and installers (*.scr *.SCR *.exe *.EXE *.zip *.7z *.cab);;All files (*)",
        )
        if files:
            self.import_paths([Path(item) for item in files])

    def import_paths(self, files: list[Path]) -> None:
        imported = 0
        notes: list[str] = []
        errors: list[str] = []
        for path in files:
            try:
                entries = import_any(self.cfg, path)
            except FileExistsError as exc:
                errors.append(str(exc))
                continue
            except NotScreensaverError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            imported += len(entries)
            for entry in entries:
                log.info("Imported %s", entry.name)
                if entry.notes:
                    notes.append(f"{entry.name}: {entry.notes}")
        if imported:
            save_config(self.cfg)
            self._reload_list()
        if errors:
            self._show_error("\n".join(errors + notes))
        elif notes:
            self._show_error("\n".join(notes))
        elif imported:
            self.status.setText(f"Imported {imported} screensaver{'s' if imported != 1 else ''}.")

    def _remove_selected(self) -> None:
        sid = self._selected_id()
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        uninstall_wine = False
        if entry.is_steam() or entry.is_prefix_exe():
            kind = "Steam game" if entry.is_steam() else "imported program"
            answer = QMessageBox.question(
                self,
                "Remove screensaver",
                f'Remove "{entry.name}" ({kind}) from the screensaver list?\n\n'
                "The original Steam install or Wine prefix folder is left in place.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if self.controller.player.is_playing() and self.controller.player.playing_name() == entry.name:
                self.controller.stop_playback()
            remove_screensaver(self.cfg, sid)
            save_config(self.cfg)
            self._reload_list()
            return
        uninstaller = wineutil.uninstaller_exe_for_entry(entry)
        if uninstaller is not None:
            others = others_sharing_prefix(self.cfg, entry)
            extra = ""
            if others:
                names = ", ".join(item.name for item in others)
                extra = (
                    f"\n\nThe same Wine install is also used by: {names}. "
                    "Uninstalling the Windows program may remove those as well."
                )
            box = QMessageBox(self)
            box.setWindowTitle("Remove screensaver")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(
                f'"{entry.name}" was installed as a Windows program in Wine.\n\n'
                "Uninstall it from Wine and remove it from the library?"
                f"{extra}"
            )
            wine_btn = box.addButton("Uninstall from Wine", QMessageBox.ButtonRole.AcceptRole)
            library_btn = box.addButton("Library only", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked not in (wine_btn, library_btn):
                return
            uninstall_wine = clicked is wine_btn
        else:
            extra = ""
            if entry.filename.lower().endswith(".exe"):
                extra = (
                    f'\n\nThis looks like a standalone demo ({entry.filename}). '
                    "No Windows uninstaller was found, so only the file in the library will be removed."
                )
            answer = QMessageBox.question(
                self,
                "Remove screensaver",
                f'Remove "{entry.name}" from the library?{extra}',
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.controller.player.is_playing() and self.controller.player.playing_name() == entry.name:
            self.controller.stop_playback()
        if uninstall_wine:
            self._uninstall_from_wine(entry)
        prefix_name = entry.wine_prefix
        remove_screensaver(self.cfg, sid)
        if (
            uninstall_wine
            and prefix_name
            and not any(item.wine_prefix == prefix_name for item in self.cfg.screensavers)
        ):
            leftover = paths.prefixes_dir() / prefix_name
            if leftover.is_dir() and leftover.resolve() != paths.wineprefix_dir().resolve():
                try:
                    session = wineutil.session_for_installer(entry.wine_runner or "system")
                    if session.prefix.resolve() == leftover.resolve():
                        wineutil.kill_prefix(session)
                    shutil.rmtree(leftover, ignore_errors=True)
                except Exception as exc:
                    log.warning("Could not remove unused Wine prefix %s: %s", leftover, exc)
        save_config(self.cfg)
        self._reload_list()

    def _uninstall_from_wine(self, entry) -> None:
        try:
            session = wineutil.session_for(entry)
        except FileNotFoundError as exc:
            self._show_error(str(exc))
            return
        try:
            kind = wineutil.start_program_uninstaller(session, entry)
        except Exception as exc:
            log.exception("Uninstall from Wine failed")
            try:
                wineutil.launch_wine_uninstaller_gui(session)
                kind = "gui"
            except Exception:
                self._show_error(f"Could not start a Wine uninstaller: {exc}")
                return
        if kind == "exe":
            QMessageBox.information(
                self,
                "Uninstall from Wine",
                "The Windows uninstaller is open.\n\n"
                "When it has finished, click OK to remove this screensaver from the library.",
            )
            return
        QMessageBox.information(
            self,
            "Uninstall from Wine",
            "Wine's Add/Remove Programs window is open.\n\n"
            "Remove the program there, then click OK to take it out of the library.",
        )

    def _test_or_stop(self) -> None:
        if self.controller.player.is_playing():
            self.controller.stop_playback()
            return
        sid = self._selected_id()
        if not sid:
            self._show_error("Import a .scr file first.")
            return
        self._ensure_wine(play_id=sid)

    def _configure_selected(self) -> None:
        sid = self._selected_id()
        if not sid:
            self._show_error("Select a screensaver first.")
            return
        self._ensure_wine(configure=True)

    def _on_list_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        sid = str(item.data(Qt.ItemDataRole.UserRole) or "")
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        menu = QMenu(self)
        test_action = menu.addAction("Test")
        test_action.triggered.connect(self._test_or_stop)
        configure_action = menu.addAction("Configure screensaver")
        configure_action.triggered.connect(self._configure_selected)
        if entry.is_steam() or entry.is_prefix_exe():
            configure_action.setEnabled(False)
        if entry.is_steam() or entry.is_prefix_exe():
            record_action = menu.addAction("Record autoplay input…")
            record_action.triggered.connect(
                lambda _checked=False, item_id=entry.id: self._record_autoplay(item_id)
            )
        interactive_action = menu.addAction("Allow interaction with this demo")
        interactive_action.setCheckable(True)
        interactive_action.setChecked(entry.interactive)
        interactive_action.toggled.connect(
            lambda checked, item_id=entry.id: self._set_entry_interactive(item_id, checked)
        )
        menu.addSeparator()
        wine_menu = menu.addMenu("Wine / Proton")
        group = QActionGroup(wine_menu)
        group.setExclusive(True)
        current = entry.wine_runner or SYSTEM_ID
        for source, runners in grouped_runners():
            section = wine_menu.addMenu(source) if source != "System" else wine_menu
            for runner in runners:
                action = section.addAction(runner.name)
                action.setCheckable(True)
                action.setChecked(runner.id == current)
                action.setData(runner.id)
                group.addAction(action)
                action.triggered.connect(
                    lambda checked, rid=runner.id, item_id=entry.id: checked
                    and self._set_entry_runner(item_id, rid)
                )
        if current != SYSTEM_ID and get_runner(current) is None:
            missing = wine_menu.addAction(f"{current} (not found)")
            missing.setEnabled(False)
        menu.addSeparator()
        winecfg_action = menu.addAction("Wine configuration…")
        winecfg_action.triggered.connect(lambda: self._ensure_wine(tool="winecfg"))
        tricks_action = menu.addAction("Winetricks…")
        if shutil.which("winetricks"):
            tricks_action.triggered.connect(lambda: self._ensure_wine(tool="winetricks"))
        else:
            tricks_action.setEnabled(False)
            tricks_action.setText("Winetricks… (not installed)")
        folder_action = menu.addAction("Open prefix folder")
        folder_action.triggered.connect(lambda: self._open_prefix_folder(entry.id))
        uninst_action = menu.addAction("Wine uninstaller…")
        uninst_action.triggered.connect(lambda: self._ensure_wine(tool="uninstaller"))
        if entry.is_steam() and not entry.wine_prefix:
            winecfg_action.setEnabled(False)
            tricks_action.setEnabled(False)
            uninst_action.setEnabled(False)
            folder_action.setEnabled(False)
        menu.exec(self.list.mapToGlobal(pos))

    def _set_entry_interactive(self, sid: str, checked: bool) -> None:
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        entry.interactive = bool(checked)
        save_config(self.cfg)
        self._reload_list()
        if checked:
            QMessageBox.information(
                self,
                "Interactive demo",
                interactive_notice(self.cfg.interactive_exit_key),
            )
        key = self.cfg.interactive_exit_key or "Escape"
        self.status.setText(
            f"{entry.name}: interactive demo on. Press {key} to exit."
            if checked
            else f"{entry.name}: mouse or keyboard will exit as usual."
        )

    def _open_additional_settings(self) -> None:
        if self._settings_dialog is None:
            self._settings_dialog = AdditionalSettingsDialog(
                self.cfg,
                self._reload_list,
                self.controller.apply_config,
                self,
            )
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _set_entry_runner(self, sid: str, runner_id: str) -> None:
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        if self.controller.player.is_playing() and self.controller.player.playing_name() == entry.name:
            self.controller.stop_playback()
        entry.wine_runner = "" if runner_id == SYSTEM_ID else runner_id
        save_config(self.cfg)
        self.status.setText(f"{entry.name} will use {runner_display(entry.wine_runner)}.")
        self._reload_list()

    def _open_prefix_folder(self, sid: str) -> None:
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        try:
            session = wineutil.session_for(entry)
        except FileNotFoundError as exc:
            self._show_error(str(exc))
            return
        session.prefix.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(session.prefix)))

    def _record_autoplay(self, sid: str) -> None:
        entry = self.cfg.entry_by_id(sid)
        if entry is None:
            return
        from . import steam as steamlib
        from .keystrokes import record_autoplay_keys

        idle = self.controller.idle
        idle.pause()

        def _stop() -> None:
            if entry.is_steam():
                prefix = None
                if entry.wine_prefix:
                    try:
                        prefix = wineutil.session_for(entry).prefix
                    except FileNotFoundError:
                        prefix = None
                steamlib.stop_app(entry.steam_appid, prefix)
            elif entry.wine_prefix:
                try:
                    wineutil.kill_prefix(wineutil.session_for(entry))
                except FileNotFoundError:
                    pass

        def _start() -> None:
            if entry.is_steam():
                steamlib.launch_app(entry.steam_appid)
                return
            if not entry.exe_path:
                raise FileNotFoundError("This item has no program path to launch.")
            wineutil.launch_exe(wineutil.session_for(entry), Path(entry.exe_path))

        try:
            macro = record_autoplay_keys(
                self,
                entry.name,
                on_start=_start,
                on_stop=_stop,
                loop_default=entry.keystroke_loop,
                stop_on_input_default=True if not entry.keystrokes else entry.stop_on_input,
            )
        finally:
            idle.resume()
        if macro is None:
            return
        entry.keystrokes = macro.events
        entry.keystroke_loop = macro.loop
        entry.stop_on_input = macro.stop_on_input
        save_config(self.cfg)
        self._reload_list()
        if not macro.events:
            self.status.setText(f"{entry.name}: recording finished with no input.")
        else:
            bits = [f"{len(macro.events)} autoplay event(s)"]
            if macro.loop:
                bits.append("looping until exit")
            if macro.stop_on_input:
                bits.append("stops when you use the keyboard or mouse")
            self.status.setText(f"{entry.name}: recorded {', '.join(bits)}.")

    def _ensure_wine(
        self,
        play_id: str | None = None,
        configure: bool = False,
        tool: str | None = None,
    ) -> None:
        sid = play_id or self._selected_id()
        entry = self.cfg.entry_by_id(sid) if sid else None
        if entry is not None and entry.is_steam() and play_id and not tool and not configure:
            self.controller.play_entry(play_id, manual=True)
            return
        try:
            session = wineutil.session_for(entry)
            wineutil.wine_binary(session)
        except FileNotFoundError as exc:
            self._show_error(str(exc))
            return
        self._pending_play = play_id
        self._pending_configure = configure
        self._pending_tool = tool
        if wineutil.prefix_ready(session):
            self._after_wine_ready()
            return
        self.status.setText("Setting up a Wine prefix. This can take a minute…")
        self._set_busy(True)
        self._setup_thread = WineSetupThread(self, session=session)
        self._setup_thread.ok.connect(self._after_wine_ready)
        self._setup_thread.fail.connect(self._wine_failed)
        self._setup_thread.start()

    def _after_wine_ready(self) -> None:
        self._set_busy(False)
        configure = self._pending_configure
        play_id = self._pending_play
        tool = self._pending_tool
        self._pending_configure = False
        self._pending_play = None
        self._pending_tool = None
        sid = play_id or self._selected_id()
        entry = self.cfg.entry_by_id(sid) if sid else None
        if tool and entry is not None:
            try:
                session = wineutil.session_for(entry)
                if tool == "winecfg":
                    wineutil.open_winecfg(session)
                elif tool == "winetricks":
                    wineutil.open_winetricks(session)
                elif tool == "uninstaller":
                    wineutil.launch_wine_uninstaller_gui(session)
            except Exception as exc:
                self._show_error(str(exc))
            return
        if configure:
            if entry:
                self.controller.player.configure(entry)
            return
        if play_id:
            self.controller.play_entry(play_id, manual=True)

    def _wine_failed(self, message: str) -> None:
        self._set_busy(False)
        self._show_error(f"Wine setup failed: {message}")

    def _set_busy(self, busy: bool) -> None:
        for widget in (self.import_btn, self.remove_btn, self.test_btn, self.config_btn, self.enabled_box):
            widget.setEnabled(not busy)

    def _show_error(self, message: str) -> None:
        log.warning("%s", message)
        if self.isVisible():
            QMessageBox.warning(self, "SCR Saver", message)
        elif self.tray.isVisible():
            self.tray.showMessage("SCR Saver", message, QSystemTrayIcon.MessageIcon.Warning, 5000)
        self.status.setText(message)

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible():
                self.hide()
            else:
                self._show_from_tray()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._really_quit or not self.tray.isVisible():
            self.controller.shutdown()
            event.accept()
            return
        event.ignore()
        self.hide()
        if self.cfg.enabled:
            self.tray.showMessage(
                "SCR Saver",
                "Still running in the tray and will start a screensaver when idle.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def quit_app(self) -> None:
        self._really_quit = True
        self.controller.shutdown()
        self.tray.hide()
        self.close()
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.quit()


def _fmt_seconds(value: int) -> str:
    value = max(0, int(value))
    minutes, seconds = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"
