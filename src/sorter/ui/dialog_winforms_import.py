"""Import an existing WinForms ("AI Brass Sorter") installation.

Two ways in, one dialog. On first run `maybe_offer_first_run` opens it against
whatever `winforms_import.find_installation` turned up; Settings → Import from
Windows opens it against a folder the user picks. The picker is the same either
way — issue #98 asks for a per-item choice rather than an all-or-nothing
migration, so the dialog's job is to say what each item would cost and let the
user decline any of it.

**Why a tree and not a list of checkboxes.** A real install accumulates years of
models, most of which the user has no interest in carrying forward (sjseth,
reviewing #125: "I actually only wanted to import a couple models… they may have
a lot of junk in the old system"). A flat "Models / Training images / Headstamps"
triple can only answer all-or-nothing, so the choice is per model, and per model
which of its images, headstamps and trained checkpoint come with it.

The tree is also what makes the **inheritance** honest: those three hang off a
model row, so they are its children rather than siblings — untick the model and
its whole branch goes with it, which is a structure the user can see instead of
a rule the dialog has to explain. There is deliberately no leaf for the model's
own row: it is the branch.

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
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..data import winforms_import
from ..data.winforms_import import (
    ImportOptions,
    ImportResult,
    LegacyModel,
    LegacySurvey,
    ModelSelection,
)
from .community_page import format_size

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
# Said on the model row itself, because it is the answer to "will this tread on
# the models I already have here?" — and the answer is per model.
NEW_MODEL = "new model here"
UPDATES_MODEL = "updates '{name}'"
# Short form of `winforms_import.MLNET_WARNING`, for the row itself; the full
# sentence stays in the warning line under the tree.
MLNET_MARK = "ML.NET model, retrain needed"

# Stands in for an unticked model while totting up what the selection costs.
_NOTHING = ModelSelection(images=False, headstamps=False, checkpoint=False)


def _muted(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("mutedLabel")
    label.setWordWrap(True)
    return label


def describe_item(key: str, found: LegacySurvey) -> tuple[str, str]:
    """(label, detail) for one top-level row, with what this install would bring.

    The counts are the whole point of surveying before importing: "Training
    images" alone says nothing, "12,255 images" tells the user why the copy is
    about to take a few minutes.
    """
    if key == winforms_import.ITEM_MODELS:
        trainable = sum(1 for m in found.models if m.has_usable_checkpoint)
        return "Models", f"{len(found.models)} in the Windows app, {trainable} with a trained model file"
    if key == winforms_import.ITEM_IMAGE_PROC:
        return "Image-processing settings", "Crop tuning from the Windows app"
    if key == winforms_import.ITEM_SERIAL:
        return "Serial / board settings", "Port, baud rate and the board's init values"
    if key == winforms_import.ITEM_AI_CONFIG:
        return "AI Config", "Endpoint, model and prompt for classifying over HTTP"
    return key, ""


def describe_model(entry: LegacyModel) -> tuple[str, str]:
    """(label, detail) for one model's row.

    Everything needed to decide whether this model is worth carrying forward,
    on the row itself — the counts included, because "38 images" is exactly how
    a user recognises an abandoned experiment without expanding anything. The
    fate comes last and is the answer to "will this tread on what I have here".
    """
    fate = UPDATES_MODEL.format(name=entry.updates) if entry.updates else NEW_MODEL
    detail = [entry.cartridge_name, f"{entry.image_count} image(s)", f"{entry.headstamp_count} headstamp(s)"]
    if entry.has_usable_checkpoint:
        detail.append(format_size(entry.checkpoint_bytes))
    elif entry.checkpoint_kind == "mlnet":
        # There *is* a checkpoint, it just can't classify here — without this
        # the missing "Trained model file" row reads as "never trained".
        detail.append(MLNET_MARK)
    detail.append(fate)
    return entry.name, " · ".join(detail)


def describe_part(part: str, entry: LegacyModel) -> tuple[str, str]:
    """(label, detail) for one model's images / headstamps / checkpoint row."""
    if part == winforms_import.PART_IMAGES:
        return "Training images", f"{entry.image_count}"
    if part == winforms_import.PART_HEADSTAMPS:
        return "Headstamps and slot assignments", f"{entry.headstamp_count}"
    if part == winforms_import.PART_CHECKPOINT:
        return "Trained model file", format_size(entry.checkpoint_bytes)
    return part, ""


def _state(checked: bool) -> Qt.CheckState:
    return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked


def _children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    return [item.child(i) for i in range(item.childCount())]


def _set_branch(item: QTreeWidgetItem, state: Qt.CheckState) -> None:
    """Set `item` and everything under it, skipping what the user can't reach.

    A disabled row is one this install has nothing behind — carrying a tick into
    it would make `selected_options` claim something that isn't there.
    """
    if not item.isDisabled():
        item.setCheckState(0, state)
    for child in _children(item):
        _set_branch(child, state)


def _refresh_ancestors(item: QTreeWidgetItem) -> None:
    """Recompute every parent above `item` from the children it actually has."""
    parent = item.parent()
    while parent is not None:
        states = {child.checkState(0) for child in _children(parent) if not child.isDisabled()}
        if states == {Qt.CheckState.Checked}:
            parent.setCheckState(0, Qt.CheckState.Checked)
        elif states == {Qt.CheckState.Unchecked}:
            parent.setCheckState(0, Qt.CheckState.Unchecked)
        elif states:
            parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
        parent = parent.parent()


def available_parts(entry: LegacyModel) -> tuple[str, ...]:
    """The parts this legacy model actually has something to offer for.

    A part with nothing behind it gets no row at all rather than a disabled one:
    every tick in the tree then means something, and the check propagation never
    has to reason about a child the user cannot reach.
    """
    parts: list[str] = []
    if entry.image_count:
        parts.append(winforms_import.PART_IMAGES)
    if entry.headstamp_count:
        parts.append(winforms_import.PART_HEADSTAMPS)
    if entry.has_usable_checkpoint:
        parts.append(winforms_import.PART_CHECKPOINT)
    return tuple(parts)


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
        # Wide enough for a model row's second column — name, cartridge, counts,
        # checkpoint size and what the import would do to the library. Narrower
        # and the last of those elides, which is the half a user is deciding on.
        self.setMinimumWidth(760)
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

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("importTree")
        self.tree.setHeaderHidden(True)
        # Two columns: what it is, then what it would bring. One column would
        # elide a model's counts away exactly when they matter — the name is
        # what has to survive, so it gets a column of its own.
        self.tree.setColumnCount(2)
        self.tree.setMinimumHeight(240)
        self.tree.itemChanged.connect(self._on_item_changed)
        # Column 0 is sized to what is *visible*, and a model's parts are a
        # level deeper than anything measured while its branch was closed —
        # without this they open elided to "Training ima…".
        self.tree.itemExpanded.connect(lambda _item: self.tree.resizeColumnToContents(0))
        column.addWidget(self.tree, 1)

        # Populated by `_reload_survey`; every row is addressed through these
        # rather than by walking the tree, so a test can tick one model.
        self.items: dict[str, QTreeWidgetItem] = {}
        self.model_items: dict[int, QTreeWidgetItem] = {}
        self.part_items: dict[tuple[int, str], QTreeWidgetItem] = {}

        # Picking two models out of fifteen should not cost thirteen clicks.
        select_row = QHBoxLayout()
        select_row.addWidget(QLabel("Models:", self))
        self.all_models_button = QPushButton("Select all", self)
        self.all_models_button.clicked.connect(lambda: self._set_all_models(True))
        self.no_models_button = QPushButton("Select none", self)
        self.no_models_button.clicked.connect(lambda: self._set_all_models(False))
        select_row.addWidget(self.all_models_button)
        select_row.addWidget(self.no_models_button)
        select_row.addStretch(1)
        column.addLayout(select_row)

        # What the current ticks add up to. A partial selection otherwise gives
        # the user no idea what they have just signed up to wait for.
        self.selection_label = _muted("", self)
        column.addWidget(self.selection_label)

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
        """Re-read the chosen folder and rebuild the tree from it."""
        self._survey = None
        problem = ""
        if self._root is not None:
            try:
                # `db` is what lets each model row say whether it would create a
                # model here or refresh one that already exists.
                self._survey = winforms_import.survey(self._root, db=self._win.db)
            except (OSError, ValueError) as exc:
                problem = str(exc)

        self._rebuild_tree()
        if self._survey is None:
            self.folder_label.setText(str(self._root) if self._root is not None else _NOT_FOUND)
            self.warning_label.setText(problem)
            self.import_button.setEnabled(False)
            self._set_items_enabled(False)
            return

        self.folder_label.setText(str(self._root))
        self.warning_label.setText("\n".join(self._survey.warnings))
        self._set_items_enabled(True)
        self.import_button.setEnabled(not self._survey.is_empty)
        self._refresh_selection_label()

    # ----- the tree -----------------------------------------------------------

    def _rebuild_tree(self) -> None:
        """Throw the rows away and build them from the current survey.

        Signals stay blocked throughout: every `setCheckState` here would
        otherwise re-enter `_on_item_changed` and propagate against a tree that
        is only half built.
        """
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            self.items = {}
            self.model_items = {}
            self.part_items = {}
            found = self._survey
            if found is None:
                return

            models_row = self._add_item(winforms_import.ITEM_MODELS, checked=bool(found.models))
            models_row.setDisabled(not found.models)
            for entry in found.models:
                self._add_model(models_row, entry)
            # Models open, each model closed: picking *which* models is the
            # first job, and a 15-model install with every branch open would
            # push the settings rows below the fold. Each model's own row
            # already carries its counts, so nothing is hidden by this.
            models_row.setExpanded(True)

            # Opt-in, not ticked by default: these overwrite values the user may
            # already have tuned here, and unlike a model there is no second copy
            # to fall back on.
            for key, available in (
                (winforms_import.ITEM_IMAGE_PROC, found.has_image_processing),
                (winforms_import.ITEM_SERIAL, found.has_serial),
                (winforms_import.ITEM_AI_CONFIG, found.has_ai_config),
            ):
                row = self._add_item(key, checked=False)
                # Nothing behind it, so nothing to offer — greyed out rather
                # than a tick that would import nothing.
                row.setDisabled(not available)
            self.tree.resizeColumnToContents(0)
        finally:
            self.tree.blockSignals(False)

    def _add_item(self, key: str, *, checked: bool) -> QTreeWidgetItem:
        found = self._survey
        assert found is not None  # only called from _rebuild_tree, past its guard
        row = QTreeWidgetItem(self.tree, list(describe_item(key, found)))
        row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        row.setCheckState(0, _state(checked))
        self.items[key] = row
        return row

    def _add_model(self, parent: QTreeWidgetItem, entry: LegacyModel) -> None:
        row = QTreeWidgetItem(parent, list(describe_model(entry)))
        row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        row.setCheckState(0, Qt.CheckState.Checked)
        self.model_items[entry.legacy_id] = row
        for part in available_parts(entry):
            leaf = QTreeWidgetItem(row, list(describe_part(part, entry)))
            leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            leaf.setCheckState(0, Qt.CheckState.Checked)
            self.part_items[(entry.legacy_id, part)] = leaf

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Push a tick down the branch and recompute the ones above it.

        Ticking a model means its whole branch; unticking it means none of it.
        A parent left showing part of its branch is `PartiallyChecked`, which is
        set here rather than through `ItemIsAutoTristate` so there is exactly one
        place that decides what a parent's state means.
        """
        if column != 0:
            return
        self.tree.blockSignals(True)
        try:
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                _set_branch(item, state)
            _refresh_ancestors(item)
        finally:
            self.tree.blockSignals(False)
        self._refresh_selection_label()

    def _set_all_models(self, checked: bool) -> None:
        models_row = self.items.get(winforms_import.ITEM_MODELS)
        if models_row is None or models_row.isDisabled():
            return
        self.tree.blockSignals(True)
        try:
            _set_branch(models_row, _state(checked))
        finally:
            self.tree.blockSignals(False)
        self._refresh_selection_label()

    def _refresh_selection_label(self) -> None:
        """Say what the current ticks add up to, in the units of the wait."""
        found = self._survey
        if found is None:
            self.selection_label.setText("")
            return
        chosen = self.selected_options()
        models = chosen.per_model or {}
        images = sum(e.image_count for e in found.models if (models.get(e.legacy_id) or _NOTHING).images)
        checkpoints = sum(
            1 for e in found.models if e.has_usable_checkpoint and (models.get(e.legacy_id) or _NOTHING).checkpoint
        )
        if not models:
            self.selection_label.setText("No models selected.")
            return
        parts = [f"{len(models)} of {len(found.models)} model(s)"]
        if images:
            parts.append(f"{images} image(s)")
        if checkpoints:
            parts.append(f"{checkpoints} trained model file(s)")
        self.selection_label.setText("Will import: " + ", ".join(parts) + ".")

    def _set_items_enabled(self, enabled: bool) -> None:
        # The tree is one control: a running import must not let the user
        # re-tick the selection it is halfway through acting on.
        self.tree.setEnabled(enabled)
        self.all_models_button.setEnabled(enabled)
        self.no_models_button.setEnabled(enabled)

    def _choose_folder(self) -> None:
        chosen = self.ask_directory()
        if not chosen:
            return
        self._root = Path(chosen)
        self._reload_survey()

    def selected_options(self) -> ImportOptions:
        """Read the tree back as an `ImportOptions`.

        The per-model map is the authority on models; the three app-level flags
        are set from it only so a summary or a log line reads sensibly.
        """
        per_model: dict[int, ModelSelection] = {}
        for legacy_id, row in self.model_items.items():
            if row.checkState(0) == Qt.CheckState.Unchecked:
                continue
            per_model[legacy_id] = ModelSelection(
                images=self._part_checked(legacy_id, winforms_import.PART_IMAGES),
                headstamps=self._part_checked(legacy_id, winforms_import.PART_HEADSTAMPS),
                checkpoint=self._part_checked(legacy_id, winforms_import.PART_CHECKPOINT),
            )
        return ImportOptions(
            models=bool(per_model),
            training_images=any(s.images for s in per_model.values()),
            headstamps=any(s.headstamps for s in per_model.values()),
            image_processing=self._item_checked(winforms_import.ITEM_IMAGE_PROC),
            serial=self._item_checked(winforms_import.ITEM_SERIAL),
            ai_config=self._item_checked(winforms_import.ITEM_AI_CONFIG),
            per_model=per_model,
        )

    def _part_checked(self, legacy_id: int, part: str) -> bool:
        """Is this part ticked? False when the model has no such row to tick."""
        row = self.part_items.get((legacy_id, part))
        return row is not None and row.checkState(0) == Qt.CheckState.Checked

    def _item_checked(self, key: str) -> bool:
        row = self.items.get(key)
        return row is not None and row.checkState(0) == Qt.CheckState.Checked

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
