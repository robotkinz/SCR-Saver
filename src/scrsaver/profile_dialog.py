"""Import and export UI for Wine compatibility scripts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import profile, wineutil
from .config import AppConfig
from .runners import SYSTEM_ID

log = logging.getLogger(__name__)


class _ApplyThread(QThread):
    line = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        cfg: AppConfig,
        script: profile.ProfileScript,
        prefix_name: str,
        runner_id: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._script = script
        self._prefix_name = prefix_name
        self._runner_id = runner_id

    def run(self) -> None:
        try:
            result = profile.apply_profile(
                self._cfg,
                self._script,
                self._prefix_name,
                self._runner_id,
                progress=self.line.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self.finished_ok.emit(result)


class ImportScriptDialog(QDialog):
    def __init__(
        self,
        cfg: AppConfig,
        script: profile.ProfileScript,
        parent: QWidget | None = None,
        *,
        default_prefix: str = "",
        default_runner: str = "",
        library_changed=None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.script = script
        self._library_changed = library_changed
        self._thread: _ApplyThread | None = None
        self.setWindowTitle("Import compatibility script")
        self.setMinimumSize(640, 520)
        self._build(default_prefix, default_runner)

    def _build(self, default_prefix: str, default_runner: str) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(self.script.name)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        form = QFormLayout()
        self.prefix_combo = QComboBox()
        select = default_prefix or ""
        chosen = 0
        for index, (label, name, runner_id) in enumerate(wineutil.available_prefixes(self.cfg)):
            self.prefix_combo.addItem(label, {"name": name, "runner_id": runner_id})
            if name == select or (select in ("", "wineprefix") and name in ("", "wineprefix")):
                chosen = index
        if default_runner and self.prefix_combo.count():
            data = self.prefix_combo.itemData(chosen)
            if isinstance(data, dict) and not data.get("runner_id"):
                data["runner_id"] = default_runner
        self.prefix_combo.setCurrentIndex(chosen)
        self.prefix_combo.currentIndexChanged.connect(self._refresh_path)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        form.addRow("Install into prefix", self.prefix_combo)
        form.addRow("", self.path_label)
        layout.addLayout(form)
        self._refresh_path()

        listing = QPlainTextEdit()
        listing.setReadOnly(True)
        listing.setPlainText(profile.summarize_profile(self.script))
        layout.addWidget(listing, 1)

        hint = QLabel(
            "Winetricks packages are downloaded and installed into the prefix you pick. "
            "Screensaver files are not in the script — matching items already in your "
            "library receive the listed settings. Game scripts apply recorded autoplay only."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(140)
        self.log_view.hide()
        layout.addWidget(self.log_view)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.import_btn = QPushButton("Import")
        self.import_btn.setDefault(True)
        self.import_btn.clicked.connect(self._start)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.import_btn)
        layout.addLayout(buttons)

    def _selected(self) -> tuple[str, str]:
        data = self.prefix_combo.currentData()
        if not isinstance(data, dict):
            return "", SYSTEM_ID
        return str(data.get("name") or ""), str(data.get("runner_id") or SYSTEM_ID)

    def _refresh_path(self) -> None:
        name, _runner = self._selected()
        folder = wineutil.prefix_path_for_name(name or "wineprefix", self.cfg)
        self.path_label.setText(str(folder))

    def _start(self) -> None:
        name, runner = self._selected()
        folder = wineutil.prefix_path_for_name(name or "wineprefix", self.cfg)
        verbs = self.script.wine.winetricks
        extra = ""
        if verbs:
            extra = f"\n\nWinetricks will download and install:\n" + ", ".join(verbs)
        answer = QMessageBox.question(
            self,
            "Import compatibility script",
            f'Install "{self.script.name}" into:\n{folder}{extra}',
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.import_btn.setEnabled(False)
        self.prefix_combo.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.log_view.show()
        self.log_view.appendPlainText("Starting…")
        self._thread = _ApplyThread(self.cfg, self.script, name, runner, self)
        self._thread.line.connect(self._on_line)
        self._thread.finished_ok.connect(self._on_ok)
        self._thread.failed.connect(self._on_fail)
        self._thread.start()

    def _on_line(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _on_ok(self, result: object) -> None:
        if self._library_changed is not None:
            self._library_changed()
        assert isinstance(result, profile.ApplyResult)
        QMessageBox.information(self, "Import compatibility script", _result_text(result))
        self.accept()

    def _on_fail(self, message: str) -> None:
        self.import_btn.setEnabled(True)
        self.prefix_combo.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        QMessageBox.warning(self, "Import compatibility script", message)


def _result_text(result: profile.ApplyResult) -> str:
    lines = [f"Installed into {result.prefix_path}."]
    if result.verbs_run:
        lines.append("Winetricks: " + ", ".join(result.verbs_run))
    if result.savers_updated:
        lines.append("Updated screensaver settings: " + ", ".join(result.savers_updated))
    if result.savers_missing:
        lines.append(
            "Not in your library yet (import the .scr/.exe yourself): "
            + ", ".join(result.savers_missing)
        )
    if result.games_updated:
        lines.append("Updated game autoplay: " + ", ".join(result.games_updated))
    if result.games_missing:
        lines.append(
            "Games not in your list yet (import the game first): "
            + ", ".join(result.games_missing)
        )
    if result.warnings:
        lines.append("")
        lines.extend(result.warnings)
    return "\n".join(lines)


def pick_and_import(
    cfg: AppConfig,
    parent: QWidget | None,
    *,
    default_prefix: str = "",
    default_runner: str = "",
    library_changed=None,
) -> bool:
    chosen, _filter = QFileDialog.getOpenFileName(
        parent,
        "Import compatibility script",
        str(Path.home()),
        profile.FILE_FILTER,
    )
    if not chosen:
        return False
    try:
        script = profile.load_profile(Path(chosen))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        QMessageBox.warning(parent, "Import compatibility script", str(exc))
        return False
    dialog = ImportScriptDialog(
        cfg,
        script,
        parent,
        default_prefix=default_prefix,
        default_runner=default_runner,
        library_changed=library_changed,
    )
    return dialog.exec() == QDialog.DialogCode.Accepted


def export_current_prefix(cfg: AppConfig, parent: QWidget | None, prefix_name: str) -> bool:
    script = profile.export_prefix(cfg, prefix_name)
    if not script.screensavers and not script.games and not script.wine.winetricks:
        answer = QMessageBox.question(
            parent,
            "Export compatibility script",
            "This prefix has no Winetricks packages, screensaver settings, or "
            "game autoplay recordings yet. Export an empty recipe anyway?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
    suggested = str(Path.home() / profile.default_export_name(prefix_name))
    chosen, _filter = QFileDialog.getSaveFileName(
        parent,
        "Export compatibility script",
        suggested,
        profile.FILE_FILTER,
    )
    if not chosen:
        return False
    path = Path(chosen)
    if path.suffix.lower() not in (".json",):
        path = path.with_suffix(".scrsaver.json")
    try:
        profile.save_profile(script, path)
    except OSError as exc:
        QMessageBox.warning(parent, "Export compatibility script", str(exc))
        return False
    QMessageBox.information(
        parent,
        "Export compatibility script",
        f"Wrote {path}\n\nShare this file. It lists Wine packages and screensaver "
        "settings. It does not include the demo or game files themselves.",
    )
    return True


def offer_import_after_wizard(
    cfg: AppConfig,
    parent: QWidget | None,
    prefix_name: str,
    runner_id: str,
    library_changed=None,
) -> None:
    answer = QMessageBox.question(
        parent,
        "Import compatibility script",
        f'Prefix "{prefix_name}" is ready.\n\n'
        "Import a compatibility script now? That downloads the listed Wine "
        "packages into this bottle and applies screensaver or autoplay settings.",
    )
    if answer != QMessageBox.StandardButton.Yes:
        return
    pick_and_import(
        cfg,
        parent,
        default_prefix=prefix_name,
        default_runner=runner_id,
        library_changed=library_changed,
    )
