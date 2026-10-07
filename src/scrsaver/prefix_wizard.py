from __future__ import annotations

import logging
import shutil

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from . import paths, wineutil
from .config import AppConfig, ManagedPrefix, save_config
from .runners import SYSTEM_ID, grouped_runners, get_runner

log = logging.getLogger(__name__)


class _CreateThread(QThread):
    ok = pyqtSignal()
    fail = pyqtSignal(str)

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self._session = session

    def run(self) -> None:
        try:
            wineutil.setup_prefix(self._session)
        except Exception as exc:
            self.fail.emit(str(exc))
            return
        self.ok.emit()


class NamePage(QWizardPage):
    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.setTitle("New Wine prefix")
        self.setSubTitle(
            "Create a separate Wine bottle under ~/.local/share/scrsaver/ "
            "so a demo or installer does not share the default prefix."
        )
        layout = QFormLayout(self)
        self.name_edit = QLineEdit(paths.next_default_prefix_name())
        self.runner_combo = QComboBox()
        _fill_runner_combo(self.runner_combo)
        layout.addRow("Prefix name", self.name_edit)
        layout.addRow("Wine / Proton", self.runner_combo)
        hint = QLabel(
            "The default name is wineprefix2, then wineprefix3, and so on. "
            "You can rename it, as in PlayOnLinux."
        )
        hint.setWordWrap(True)
        layout.addRow(hint)
        self.registerField("prefix_name*", self.name_edit)
        self.registerField("runner_index", self.runner_combo)

    def validatePage(self) -> bool:
        name = paths.sanitize_prefix_name(self.name_edit.text())
        if not name:
            QMessageBox.warning(self, "Wine prefix", "Enter a prefix name.")
            return False
        if name.lower() in paths.RESERVED_PREFIX_NAMES or name.lower() == "wineprefix":
            QMessageBox.warning(
                self,
                "Wine prefix",
                f'"{name}" is reserved. Choose another name (for example wineprefix2).',
            )
            return False
        dest = paths.resolve_named_prefix(name)
        if dest.exists():
            QMessageBox.warning(self, "Wine prefix", f'"{name}" already exists.')
            return False
        self.name_edit.setText(name)
        return True

    def runner_id(self) -> str:
        return str(self.runner_combo.currentData() or SYSTEM_ID)


class CreatePage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Creating prefix")
        self.setSubTitle("Wine is initializing this bottle. That can take a minute.")
        self._ready = False
        self._thread: _CreateThread | None = None
        self.status = QLabel("Waiting to start…")
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)

    def initializePage(self) -> None:
        wizard = self.wizard()
        name = paths.sanitize_prefix_name(wizard.field("prefix_name"))
        runner_id = wizard.name_page.runner_id()
        self._ready = False
        self.completeChanged.emit()
        self.status.setText(f"Creating {name} with {runner_id}…")
        try:
            session = wineutil.session_for_named_prefix(name, runner_id)
        except Exception as exc:
            self.status.setText(str(exc))
            self._ready = False
            self.completeChanged.emit()
            return
        self._thread = _CreateThread(session, self)
        self._thread.ok.connect(self._on_ok)
        self._thread.fail.connect(self._on_fail)
        self._thread.start()

    def isComplete(self) -> bool:
        return self._ready

    def _on_ok(self) -> None:
        self.status.setText("Prefix is ready.")
        self._ready = True
        self.completeChanged.emit()
        wizard = self.wizard()
        if isinstance(wizard, PrefixSetupWizard):
            wizard._record_prefix()

    def _on_fail(self, message: str) -> None:
        self.status.setText(f"Setup failed: {message}")
        self._ready = False
        self.completeChanged.emit()


class ToolsPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Prefix ready")
        self.setSubTitle("Open Wine configuration or Winetricks if this bottle needs extra components.")
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = QHBoxLayout()
        self.winecfg_btn = QPushButton("Wine configuration…")
        self.winecfg_btn.clicked.connect(self._open_winecfg)
        self.tricks_btn = QPushButton("Winetricks…")
        self.tricks_btn.clicked.connect(self._open_winetricks)
        if not shutil.which("winetricks"):
            self.tricks_btn.setEnabled(False)
            self.tricks_btn.setText("Winetricks… (not installed)")
        buttons.addWidget(self.winecfg_btn)
        buttons.addWidget(self.tricks_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)

    def initializePage(self) -> None:
        wizard = self.wizard()
        name = paths.sanitize_prefix_name(wizard.field("prefix_name"))
        runner_id = wizard.name_page.runner_id()
        runner = get_runner(runner_id)
        runner_label = runner.name if runner else runner_id
        dest = paths.resolve_named_prefix(name)
        self.summary.setText(
            f"Created {dest}\nusing {runner_label}.\n\n"
            "You can install Windows programs into this prefix later. "
            "Removing prefixes will come in a later update."
        )

    def _session(self):
        wizard = self.wizard()
        name = paths.sanitize_prefix_name(wizard.field("prefix_name"))
        runner_id = wizard.name_page.runner_id()
        return wineutil.session_for_named_prefix(name, runner_id)

    def _open_winecfg(self) -> None:
        try:
            wineutil.open_winecfg(self._session())
        except Exception as exc:
            QMessageBox.warning(self, "Wine configuration", str(exc))

    def _open_winetricks(self) -> None:
        try:
            wineutil.open_winetricks(self._session())
        except Exception as exc:
            QMessageBox.warning(self, "Winetricks", str(exc))


class PrefixSetupWizard(QWizard):
    def __init__(self, cfg: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.created_name = ""
        self.created_runner_id = ""
        self.setWindowTitle("Wine Prefix Wizard")
        self.setMinimumWidth(520)
        self.name_page = NamePage(cfg)
        self.addPage(self.name_page)
        self.addPage(CreatePage())
        self.addPage(ToolsPage())
        self.setOption(QWizard.WizardOption.NoBackButtonOnLastPage, True)

    def _record_prefix(self) -> None:
        name = paths.sanitize_prefix_name(self.field("prefix_name"))
        runner_id = self.name_page.runner_id()
        self.created_name = name
        self.created_runner_id = runner_id
        if not any(item.name == name for item in self.cfg.managed_prefixes):
            self.cfg.managed_prefixes.append(ManagedPrefix(name=name, runner_id=runner_id))
            save_config(self.cfg)
        log.info("Recorded managed prefix %s (%s)", name, runner_id)


def _fill_runner_combo(combo: QComboBox) -> None:
    combo.clear()
    first = True
    for source, runners in grouped_runners():
        if not first:
            combo.insertSeparator(combo.count())
        first = False
        for runner in runners:
            label = runner.name if source == "System" else f"{runner.name}  ({source})"
            combo.addItem(label, runner.id)
