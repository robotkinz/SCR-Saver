from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QKeySequence, QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import paths, wineutil
from .config import AppConfig, save_config
from .keys import sequence_to_name
from .library import import_scr_file
from .pe import NotScreensaverError
from .prefix_manager import PrefixManagerDialog
from .prefix_wizard import PrefixSetupWizard
from .runners import SYSTEM_ID, runner_display

log = logging.getLogger(__name__)


class AdditionalSettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, reload_library, apply_config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._reload_library = reload_library
        self._apply_config = apply_config
        self._loading = False
        self.setWindowTitle("Additional Settings")
        self.setMinimumWidth(480)
        self._build()
        self._load()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "These options apply to every installed screensaver. "
            "Use this window for audio, fullscreen video, interactive keys, "
            "and Windows installers that are not a plain .scr file."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        audio = QGroupBox("Audio")
        audio_form = QFormLayout(audio)
        self.mute_box = QCheckBox("Mute screensaver audio")
        self.mute_box.toggled.connect(self._save)
        self.duck_slider = QSlider(Qt.Orientation.Horizontal)
        self.duck_slider.setRange(0, 100)
        self.duck_slider.setTickInterval(10)
        self.duck_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.duck_slider.valueChanged.connect(self._on_duck_changed)
        self.duck_label = QLabel()
        duck_row = QHBoxLayout()
        duck_row.addWidget(self.duck_slider, 1)
        duck_row.addWidget(self.duck_label)
        audio_form.addRow(self.mute_box)
        audio_form.addRow("When other audio is playing, lower screensaver to", duck_row)
        self.one_audio_box = QCheckBox(
            "When using All screens, only one display plays screensaver audio"
        )
        self.one_audio_box.toggled.connect(self._on_one_audio_toggled)
        self.audio_source_combo = QComboBox()
        self.audio_source_combo.currentIndexChanged.connect(self._save)
        audio_form.addRow(self.one_audio_box)
        audio_form.addRow("Play screensaver audio from", self.audio_source_combo)
        layout.addWidget(audio)

        pace = QGroupBox("Frame cap (all savers)")
        pace_form = QFormLayout(pace)
        self.fps_combo = QComboBox()
        self.fps_combo.addItem("Off (no frame cap)", 0)
        for fps in (8, 10, 12, 15, 20, 24, 30, 60):
            self.fps_combo.addItem(f"{fps} FPS", fps)
        self.fps_combo.currentIndexChanged.connect(self._save)
        pace_form.addRow("Smooth frame cap", self.fps_combo)
        pace_hint = QLabel(
            "Applies to every screensaver and demo. Paces OpenGL/Direct3D so motion "
            "stays even. Screensaver audio is muted while a cap is on, because sound "
            "usually does not track a lowered frame rate well. Prefer this over CPU "
            "quota for 3D Maze."
        )
        pace_hint.setWordWrap(True)
        pace_form.addRow(pace_hint)
        layout.addWidget(pace)

        video = QGroupBox("Displays")
        video_form = QFormLayout(video)
        self.skip_fs = QCheckBox("Do not run a screensaver on a display that has fullscreen video")
        self.skip_fs.toggled.connect(self._save)
        video_form.addRow(self.skip_fs)
        layout.addWidget(video)

        keys = QGroupBox("Interactive demos")
        keys_form = QFormLayout(keys)
        self.key_edit = QKeySequenceEdit()
        if hasattr(self.key_edit, "setMaximumSequenceLength"):
            self.key_edit.setMaximumSequenceLength(1)
        self.key_edit.editingFinished.connect(self._on_key_changed)
        keys_form.addRow("Exit shortcut", self.key_edit)
        hint = QLabel("Click the field, then press the key you want. Default is Escape.")
        hint.setWordWrap(True)
        keys_form.addRow(hint)
        layout.addWidget(keys)

        prefix_box = QGroupBox("Wine prefixes")
        prefix_layout = QVBoxLayout(prefix_box)
        prefix_layout.addWidget(
            QLabel(
                "Not every screensaver or demo will run in the default bottle. "
                "Create extra prefixes as wineprefix2, wineprefix3, and so on "
                f"under {paths.data_dir()}."
            )
        )
        wizard_btn = QPushButton("Wine Prefix Wizard…")
        wizard_btn.clicked.connect(self._open_prefix_wizard)
        manage_btn = QPushButton("Manage prefixes…")
        manage_btn.clicked.connect(self._open_prefix_manager)
        prefix_btns = QHBoxLayout()
        prefix_btns.addWidget(wizard_btn)
        prefix_btns.addWidget(manage_btn)
        prefix_btns.addStretch(1)
        prefix_layout.addLayout(prefix_btns)
        layout.addWidget(prefix_box)

        installer = QGroupBox("Windows installer")
        inst_layout = QVBoxLayout(installer)
        inst_layout.addWidget(
            QLabel(
                "Choose which Wine prefix receives the program. "
                "Create a new prefix first if this installer should not share a bottle "
                "with other demos. New screensavers from that install use that prefix "
                "and its Wine or Proton version."
            )
        )
        form = QFormLayout()
        self.prefix_combo = QComboBox()
        self.prefix_combo.currentIndexChanged.connect(self._on_install_prefix_changed)
        form.addRow("Install into prefix", self.prefix_combo)
        inst_layout.addLayout(form)
        run_btn = QPushButton("Run Windows installer…")
        run_btn.clicked.connect(self._run_installer)
        inst_layout.addWidget(run_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(installer)
        self._fill_prefix_combo()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh_one_audio_widgets()
        self._fill_prefix_combo()

    def _multi_screen_all_displays(self) -> bool:
        if self.cfg.display_mode != "all":
            return False
        app = QGuiApplication.instance()
        return bool(app is not None and len(app.screens()) > 1)

    def _fill_audio_sources(self) -> None:
        current = self.cfg.audio_source_display or "primary"
        self.audio_source_combo.clear()
        self.audio_source_combo.addItem("Primary screen", "primary")
        app = QGuiApplication.instance()
        primary = app.primaryScreen() if app is not None else None
        screens = list(app.screens()) if app is not None else []
        for screen in screens:
            name = screen.name() or "Unknown"
            geo = screen.geometry()
            label = f"{name}  ({geo.width()}×{geo.height()})"
            if primary is not None and screen is primary:
                label += "  — primary"
            self.audio_source_combo.addItem(label, f"output:{name}")
        index = self.audio_source_combo.findData(current)
        if index < 0 and current.startswith("output:"):
            wanted = current.split(":", 1)[1]
            self.audio_source_combo.addItem(f"{wanted}  (disconnected)", current)
            index = self.audio_source_combo.findData(current)
        self.audio_source_combo.setCurrentIndex(max(0, index))

    def _refresh_one_audio_widgets(self) -> None:
        available = self._multi_screen_all_displays()
        self.one_audio_box.setEnabled(available)
        self.audio_source_combo.setEnabled(available and self.one_audio_box.isChecked())
        if available:
            self.one_audio_box.setToolTip(
                "Stops overlapping sound when a copy of the screensaver runs on every monitor."
            )
        else:
            self.one_audio_box.setToolTip(
                "Available when Display is set to All screens and more than one monitor is attached."
            )

    def _on_one_audio_toggled(self, checked: bool) -> None:
        self.audio_source_combo.setEnabled(self._multi_screen_all_displays() and checked)
        self._save()

    def _fill_prefix_combo(self, select_name: str | None = None) -> None:
        current = select_name
        if current is None and hasattr(self, "prefix_combo"):
            current = str(self.prefix_combo.currentData() or "")
        self.prefix_combo.blockSignals(True)
        self.prefix_combo.clear()
        for label, name, runner_id in wineutil.available_prefixes(self.cfg):
            self.prefix_combo.addItem(label, {"name": name, "runner_id": runner_id})
        self.prefix_combo.insertSeparator(self.prefix_combo.count())
        self.prefix_combo.addItem("Create new prefix…", {"name": "__new__", "runner_id": ""})
        index = 0
        if current and current != "__new__":
            for i in range(self.prefix_combo.count()):
                data = self.prefix_combo.itemData(i)
                if isinstance(data, dict) and data.get("name") == current:
                    index = i
                    break
        self.prefix_combo.setCurrentIndex(index)
        self.prefix_combo.blockSignals(False)

    def _on_install_prefix_changed(self) -> None:
        data = self.prefix_combo.currentData()
        if isinstance(data, dict) and data.get("name") == "__new__":
            self._create_prefix_for_installer()

    def _create_prefix_for_installer(self) -> str | None:
        wizard = PrefixSetupWizard(self.cfg, self)
        if wizard.exec() != QDialog.DialogCode.Accepted or not wizard.created_name:
            self._fill_prefix_combo()
            return None
        self._fill_prefix_combo(select_name=wizard.created_name)
        return wizard.created_name

    def _load(self) -> None:
        self._loading = True
        self.mute_box.blockSignals(True)
        self.duck_slider.blockSignals(True)
        self.skip_fs.blockSignals(True)
        self.one_audio_box.blockSignals(True)
        self.audio_source_combo.blockSignals(True)
        self.fps_combo.blockSignals(True)
        self.mute_box.setChecked(self.cfg.mute_screensaver_audio)
        self.duck_slider.setValue(int(self.cfg.duck_percent))
        self.one_audio_box.setChecked(self.cfg.one_audio_source)
        self._fill_audio_sources()
        self.skip_fs.setChecked(self.cfg.skip_fullscreen_video)
        self.key_edit.setKeySequence(QKeySequence(self.cfg.interactive_exit_key or "Escape"))
        fps_idx = self.fps_combo.findData(int(self.cfg.fps_limit or 0))
        self.fps_combo.setCurrentIndex(max(0, fps_idx))
        self.mute_box.blockSignals(False)
        self.duck_slider.blockSignals(False)
        self.skip_fs.blockSignals(False)
        self.one_audio_box.blockSignals(False)
        self.audio_source_combo.blockSignals(False)
        self.fps_combo.blockSignals(False)
        self._on_duck_changed(self.duck_slider.value())
        self._refresh_one_audio_widgets()
        self._loading = False

    def _on_duck_changed(self, value: int) -> None:
        self.duck_label.setText(f"{int(value)}%")
        if not self._loading:
            self._save()

    def _on_key_changed(self) -> None:
        name = sequence_to_name(self.key_edit.keySequence())
        self.key_edit.blockSignals(True)
        self.key_edit.setKeySequence(QKeySequence(name))
        self.key_edit.blockSignals(False)
        self._save()

    def _save(self) -> None:
        if self._loading:
            return
        self.cfg.mute_screensaver_audio = self.mute_box.isChecked()
        self.cfg.duck_percent = int(self.duck_slider.value())
        self.cfg.one_audio_source = self.one_audio_box.isChecked()
        self.cfg.audio_source_display = str(self.audio_source_combo.currentData() or "primary")
        self.cfg.skip_fullscreen_video = self.skip_fs.isChecked()
        self.cfg.fps_limit = int(self.fps_combo.currentData() or 0)
        self.cfg.interactive_exit_key = sequence_to_name(self.key_edit.keySequence())
        save_config(self.cfg)
        self._apply_config()

    def _open_prefix_wizard(self) -> None:
        wizard = PrefixSetupWizard(self.cfg, self)
        wizard.exec()
        self._fill_prefix_combo(select_name=wizard.created_name or None)

    def _open_prefix_manager(self) -> None:
        parent = self.parent()
        idle = getattr(getattr(parent, "controller", None), "idle", None)
        PrefixManagerDialog(
            self.cfg,
            self,
            library_changed=self._reload_library,
            idle=idle,
        ).exec()
        self._fill_prefix_combo()
        self._reload_library()

    def _selected_install_prefix(self) -> tuple[str, str] | None:
        data = self.prefix_combo.currentData()
        if not isinstance(data, dict):
            return "", SYSTEM_ID
        name = str(data.get("name") or "")
        if name == "__new__":
            created = self._create_prefix_for_installer()
            if not created:
                return None
            data = self.prefix_combo.currentData()
            if not isinstance(data, dict):
                return created, SYSTEM_ID
            return str(data.get("name") or created), str(data.get("runner_id") or SYSTEM_ID)
        return name, str(data.get("runner_id") or SYSTEM_ID)

    def _run_installer(self) -> None:
        selected = self._selected_install_prefix()
        if selected is None:
            return
        prefix_name, runner_id = selected
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Windows installer",
            str(Path.home() / "Downloads"),
            "Windows executables (*.exe *.EXE);;All files (*)",
        )
        if not path:
            return
        try:
            if prefix_name:
                session = wineutil.session_for_named_prefix(prefix_name, runner_id)
            else:
                session = wineutil.session_for(None)
            wineutil.setup_prefix(session)
        except Exception as exc:
            QMessageBox.warning(self, "SCR Saver", f"Could not prepare Wine: {exc}")
            return
        before = wineutil.list_prefix_screensavers(session.prefix)
        try:
            wineutil.launch_exe(session, Path(path))
        except Exception as ext:
            QMessageBox.warning(self, "SCR Saver", str(ext))
            return
        where = prefix_name or "wineprefix"
        QMessageBox.information(
            self,
            "Windows installer",
            "Complete the Windows installer in the Wine window.\n\n"
            "When it has finished, click OK to import any new screensavers. "
            f"They will use {where} ({runner_display(runner_id)}).",
        )
        after = wineutil.list_prefix_screensavers(session.prefix)
        new_files = [item for item in after if item.resolve() not in {p.resolve() for p in before}]
        if not new_files:
            picked, _ = QFileDialog.getOpenFileName(
                self,
                "No new .scr file was found. Choose a file from the prefix",
                str(session.prefix / "drive_c"),
                "Screensavers (*.scr *.SCR *.exe *.EXE);;All files (*)",
            )
            if picked:
                new_files = [Path(picked)]
        if not new_files:
            QMessageBox.information(self, "SCR Saver", "No new screensaver files were imported.")
            return
        imported = 0
        errors: list[str] = []
        stored_prefix = session.prefix.name
        for source in new_files:
            try:
                import_scr_file(
                    self.cfg,
                    source,
                    wine_runner="" if runner_id == SYSTEM_ID else runner_id,
                    wine_prefix=stored_prefix,
                )
                imported += 1
            except FileExistsError as exc:
                errors.append(str(exc))
            except (OSError, NotScreensaverError) as exc:
                errors.append(f"{source.name}: {exc}")
        save_config(self.cfg)
        self._reload_library()
        message = (
            f"Imported {imported} screensaver{'s' if imported != 1 else ''} "
            f"into {stored_prefix} using {runner_display(runner_id)}."
        )
        if errors:
            message += "\n\n" + "\n".join(errors)
        QMessageBox.information(self, "SCR Saver", message)
