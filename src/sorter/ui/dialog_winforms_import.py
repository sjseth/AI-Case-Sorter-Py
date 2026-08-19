"""Import an existing WinForms ("AI Brass Sorter") installation.

Two ways in, one dialog. On first run `maybe_offer_first_run` opens it against
whatever `winforms_import.find_installation` turned up; Settings → Import from
Windows opens it against a folder the user picks. The checkbox list is the same
either way — issue #98 asks for a per-item choice rather than an all-or-nothing
migration, so the dialog's job is to say what each item would cost and let the
user decline any of it.

Threading follows CLAUDE.md §8: the import runs on a worker thread and only ever
puts a message on a ``queue.Queue``; a main-thread ``QTimer`` drains it into the
widgets. Nothing here touches a widget off the main thread.

Seam discipline (§5): ``notify`` and ``ask_directory`` are instance attributes,
not methods, so an offscreen test can replace them and nothing blocks on a
native modal.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..data import winforms_import
from ..data.winforms_import import (
    ImportOptions,
    ImportResult,
    LegacySurvey,
)

TITLE = "Import from the Windows app"
# Settings section name — also a GUIDE.md heading, since help_viewer slugifies
# section names straight to an anchor.
SECTION_NAME = "Import from Windows"
# Dynamic property the Settings button carries so a test can find it.
IMPORT_BUTTON_ROLE = "winformsImport"

FIRST_RUN_INTRO = (
    "An installation of the Windows app (AI Brass Sorter) was found on this "
    "computer. Its models, training images and settings can be copied across "
    "so you don't have to set everything up again."
)
SETTINGS_INTRO = "Copy models, training images and settings out of an installation of the Windows app."
# Said plainly and up front, because "import" reads as "move" to plenty of people.
NON_DESTRUCTIVE = "Nothing in the Windows app is changed, moved or deleted — everything is copied."

_NOT_FOUND = (
    "No installation of the Windows app was found in the usual place. "
    "If you have one somewhere else, choose its folder — the one containing "
    "'Data' and 'training'."
)


def _muted(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("mutedLabel")
    label.setWordWrap(True)
    return label


def describe_item(key: str, found: LegacySurvey) -> tuple[str, str]:
    """(label, hint) for one checkbox, with the counts this install would bring.

    The counts are the whole point of surveying before importing: "Training
    images" alone says nothing, "12,255 images" tells the user why the copy is
    about to take a few minutes.
    """
    if key == winforms_import.ITEM_MODELS:
        trainable = sum(1 for m in found.models if m.has_usable_checkpoint)
        hint = f"{len(found.models)} model(s), {trainable} with a trained model file"
        return "Models", hint
    if key == winforms_import.ITEM_IMAGES:
        return "Training images", f"{found.total_images} image(s) — the slowest part of the copy"
    if key == winforms_import.ITEM_HEADSTAMPS:
        return "Headstamps and slot assignments", f"{found.total_headstamps} headstamp(s), with their bin layout"
    if key == winforms_import.ITEM_IMAGE_PROC:
        return "Image-processing settings", "Crop tuning from the Windows app"
    if key == winforms_import.ITEM_SERIAL:
        return "Serial / board settings", "Port, baud rate and the board's init values"
    if key == winforms_import.ITEM_AI_CONFIG:
        return "AI Config", "Endpoint, model and prompt for classifying over HTTP"
    return key, ""


class WinFormsImportDialog(QDialog):
    """Pick a folder, tick what to bring across, watch it happen."""

    def __init__(
        self,
        win: Any,
        root: Path | None,
        parent: QWidget | None = None,
        *,
        first_run: bool = False,
    ) -> None:
        super().__init__(parent)
        self._win = win
        self._root = Path(root) if root else None
        self._survey: LegacySurvey | None = None
        self._first_run = first_run
        self._running = False
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.result_summary: ImportResult | None = None

        self.notify: Callable[[str, str], None] = self._notify
        self.ask_directory: Callable[[], str | None] = self._ask_directory

        self.setWindowTitle(TITLE)
        self.setMinimumWidth(520)
        self._build_ui()
        self._reload_survey()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(100)

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)
        column.setSpacing(10)

        column.addWidget(_muted(FIRST_RUN_INTRO if self._first_run else SETTINGS_INTRO, self))

        folder_row = QHBoxLayout()
        self.folder_label = QLabel("", self)
        self.folder_label.setWordWrap(True)
        folder_row.addWidget(self.folder_label, 1)
        self.browse_button = QPushButton("Choose folder…", self)
        self.browse_button.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.browse_button)
        column.addLayout(folder_row)

        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("sidebarSeparator")
        column.addWidget(line)

        self.checkboxes: dict[str, QCheckBox] = {}
        self.hints: dict[str, QLabel] = {}
        # Ticked by default: the three that make up "my setup". The settings
        # blocks are opt-in — they overwrite what the user may already have
        # tuned here, and unlike a model there is no second copy to fall back on.
        defaults = {
            winforms_import.ITEM_MODELS: True,
            winforms_import.ITEM_IMAGES: True,
            winforms_import.ITEM_HEADSTAMPS: True,
            winforms_import.ITEM_IMAGE_PROC: False,
            winforms_import.ITEM_SERIAL: False,
            winforms_import.ITEM_AI_CONFIG: False,
        }
        for key, checked in defaults.items():
            box = QCheckBox("", self)
            box.setChecked(checked)
            self.checkboxes[key] = box
            column.addWidget(box)
            hint = _muted("", self)
            hint.setContentsMargins(24, 0, 0, 4)
            self.hints[key] = hint
            column.addWidget(hint)
        self.checkboxes[winforms_import.ITEM_MODELS].toggled.connect(self._sync_dependents)

        self.warning_label = _muted("", self)
        self.warning_label.setObjectName("warningLabel")
        column.addWidget(self.warning_label)

        self.status_label = _muted("", self)
        column.addWidget(self.status_label)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)  # the copy has no useful total up front
        self.progress.setVisible(False)
        column.addWidget(self.progress)

        self.buttons = QDialogButtonBox(self)
        self.import_button = self.buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setObjectName("action")
        self.close_button = self.buttons.addButton(
            "Not now" if self._first_run else "Close",
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        self.import_button.clicked.connect(self.start_import)
        self.close_button.clicked.connect(self.reject)
        column.addWidget(self.buttons)

    # ----- the source folder --------------------------------------------------

    def _reload_survey(self) -> None:
        """Re-read the chosen folder and repaint every count."""
        self._survey = None
        if self._root is not None:
            try:
                self._survey = winforms_import.survey(self._root)
            except (OSError, ValueError) as exc:
                self.folder_label.setText(str(self._root))
                self.warning_label.setText(str(exc))
                self._set_items_enabled(False)
                self.import_button.setEnabled(False)
                return

        if self._survey is None:
            self.folder_label.setText(_NOT_FOUND)
            self.warning_label.setText("")
            self._set_items_enabled(False)
            self.import_button.setEnabled(False)
            return

        self.folder_label.setText(str(self._root))
        for key, box in self.checkboxes.items():
            label, hint = describe_item(key, self._survey)
            box.setText(label)
            self.hints[key].setText(hint)
        # Grey out what this install simply doesn't have, rather than offering
        # a tick that would import nothing.
        self.checkboxes[winforms_import.ITEM_MODELS].setEnabled(bool(self._survey.models))
        self.checkboxes[winforms_import.ITEM_IMAGES].setEnabled(bool(self._survey.total_images))
        self.checkboxes[winforms_import.ITEM_HEADSTAMPS].setEnabled(bool(self._survey.total_headstamps))
        self.checkboxes[winforms_import.ITEM_IMAGE_PROC].setEnabled(self._survey.has_image_processing)
        self.checkboxes[winforms_import.ITEM_SERIAL].setEnabled(self._survey.has_serial)
        self.checkboxes[winforms_import.ITEM_AI_CONFIG].setEnabled(self._survey.has_ai_config)
        for box in self.checkboxes.values():
            if not box.isEnabled():
                box.setChecked(False)
        self.warning_label.setText("\n".join(self._survey.warnings))
        self.import_button.setEnabled(not self._survey.is_empty)
        self._sync_dependents()

    def _sync_dependents(self) -> None:
        """Images and headstamps hang off a model row — no models, no either."""
        if winforms_import.ITEM_MODELS not in self.checkboxes:
            return
        models_on = self.checkboxes[winforms_import.ITEM_MODELS].isChecked()
        survey_in = self._survey
        for key in (winforms_import.ITEM_IMAGES, winforms_import.ITEM_HEADSTAMPS):
            box = self.checkboxes[key]
            available = survey_in is not None and bool(
                survey_in.total_images if key == winforms_import.ITEM_IMAGES else survey_in.total_headstamps
            )
            box.setEnabled(models_on and available)
            if not box.isEnabled():
                box.setChecked(False)

    def _set_items_enabled(self, enabled: bool) -> None:
        for box in self.checkboxes.values():
            box.setEnabled(enabled)

    def _choose_folder(self) -> None:
        chosen = self.ask_directory()
        if not chosen:
            return
        self._root = Path(chosen)
        self._reload_survey()

    def selected_options(self) -> ImportOptions:
        return ImportOptions(
            models=self.checkboxes[winforms_import.ITEM_MODELS].isChecked(),
            training_images=self.checkboxes[winforms_import.ITEM_IMAGES].isChecked(),
            headstamps=self.checkboxes[winforms_import.ITEM_HEADSTAMPS].isChecked(),
            image_processing=self.checkboxes[winforms_import.ITEM_IMAGE_PROC].isChecked(),
            serial=self.checkboxes[winforms_import.ITEM_SERIAL].isChecked(),
            ai_config=self.checkboxes[winforms_import.ITEM_AI_CONFIG].isChecked(),
        )

    # ----- running it ---------------------------------------------------------

    def start_import(self) -> None:
        if self._running or self._root is None:
            return
        options = self.selected_options()
        if not options.any_selected():
            self.notify(TITLE, "Tick at least one thing to import.")
            return

        self._running = True
        self._set_items_enabled(False)
        self.browse_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.progress.setVisible(True)
        self.status_label.setText("Reading the Windows app's data…")

        root = self._root
        db = self._win.db
        config = self._win.config
        events = self._events

        def _work() -> None:
            try:
                result = winforms_import.import_installation(
                    root,
                    db=db,
                    config=config,
                    options=options,
                    progress=lambda message: events.put(("progress", message)),
                )
            except Exception as exc:  # surfaced in the dialog, never swallowed
                events.put(("error", str(exc)))
                return
            events.put(("done", result))

        threading.Thread(target=_work, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                return
            if kind == "progress":
                self.status_label.setText(str(payload))
            elif kind == "error":
                self._failed(str(payload))
            elif kind == "done":
                self._succeeded(payload)

    def _failed(self, message: str) -> None:
        self._running = False
        self.progress.setVisible(False)
        self.status_label.setText("")
        self.browse_button.setEnabled(True)
        self._reload_survey()
        self.notify("Import failed", message)

    def _succeeded(self, result: ImportResult) -> None:
        self._running = False
        self.result_summary = result
        self.progress.setVisible(False)
        self.status_label.setText("")
        self._win.after_winforms_import(result)
        self.notify("Import complete", summarize(result))
        self.accept()

    # ----- seams --------------------------------------------------------------

    def _notify(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _ask_directory(self) -> str | None:
        path = QFileDialog.getExistingDirectory(self, "Choose the Windows app's folder")
        return path or None

    def closeEvent(self, event: Any) -> None:
        self._timer.stop()
        super().closeEvent(event)

    def reject(self) -> None:
        # A running import owns the DB and the file copies; closing the dialog
        # out from under it would leave a half-copied model with no UI to say so.
        if self._running:
            return
        self._timer.stop()
        super().reject()


def summarize(result: ImportResult) -> str:
    """Plain-language "here's what landed", for the completion box."""
    lines: list[str] = []
    if result.models_imported:
        lines.append(f"{result.models_imported} model(s) imported")
    if result.models_updated:
        lines.append(f"{result.models_updated} model(s) updated")
    if result.checkpoints_copied:
        lines.append(f"{result.checkpoints_copied} trained model file(s) copied")
    if result.images_copied:
        lines.append(f"{result.images_copied} training image(s) copied")
    if result.headstamps_imported:
        lines.append(f"{result.headstamps_imported} headstamp(s) imported")
    if result.parents_imported:
        lines.append(f"{result.parents_imported} parent classification(s) imported")
    if result.slots_assigned:
        lines.append(f"{result.slots_assigned} slot assignment(s) restored")
    if result.serial_imported:
        lines.append("Serial / board settings imported")
    if result.image_processing_imported:
        lines.append("Image-processing settings imported")
    if result.ai_config_imported:
        lines.append("AI Config imported")
    if not lines:
        lines.append("Nothing needed importing — everything was already here.")
    if result.warnings:
        lines.append("")
        lines.extend(result.warnings)
    return "\n".join(lines)


# ----- entry points -----------------------------------------------------------


def open_import_dialog(win: Any, root: Path | None, *, first_run: bool = False) -> WinFormsImportDialog:
    dialog = WinFormsImportDialog(win, root, win, first_run=first_run)
    dialog.exec()
    return dialog


def maybe_offer_first_run(win: Any) -> WinFormsImportDialog | None:
    """Offer the import once, on a launch where there is something to offer.

    Returns None — silently — for the overwhelmingly common case of a user who
    never ran the Windows app. The offer is marked as made either way, since
    Settings → Import from Windows keeps it reachable forever.
    """
    root = winforms_import.should_offer_first_run(win.db)
    if root is None:
        return None
    winforms_import.mark_first_run_offered(win.db)
    return open_import_dialog(win, root, first_run=True)


def build_winforms_import_section(win: Any) -> QWidget:
    """Settings → Import from Windows: the same dialog, on a folder you pick."""
    page = QWidget()
    column = QVBoxLayout(page)
    column.setSpacing(10)
    column.setAlignment(Qt.AlignmentFlag.AlignTop)

    title = QLabel(TITLE, page)
    title.setObjectName("sectionTitle")
    column.addWidget(title)
    column.addWidget(_muted(SETTINGS_INTRO, page))
    column.addWidget(_muted(NON_DESTRUCTIVE, page))

    detected = winforms_import.find_installation()
    column.addWidget(_muted(f"Found: {detected}" if detected else _NOT_FOUND, page))

    button = QPushButton("Import from the Windows app…", page)
    # `action` paints it as the primary; the second name is how a test finds
    # it (`findChild`) without an attribute stuffed onto the page.
    button.setObjectName("action")
    button.setProperty("role", IMPORT_BUTTON_ROLE)
    button.clicked.connect(lambda: open_import_dialog(win, winforms_import.find_installation()))
    # In a row with a trailing stretch, not straight into the column: a primary
    # button stretched to the full width of the settings pane reads as a banner.
    button_row = QHBoxLayout()
    button_row.addWidget(button)
    button_row.addStretch(1)
    column.addLayout(button_row)
    return page
