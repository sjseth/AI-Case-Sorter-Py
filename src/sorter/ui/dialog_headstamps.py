"""Per-model headstamp manager: headstamps, slots, and parent classifications.

The only place a *local* model's headstamps can be managed (the AI Config page
manages the other mode's list).

Three semantics to get right, because getting them wrong loses data:

* **Renaming a headstamp renames its training images.** ``{old}__{ticks}.jpg``
  becomes ``{new}__{ticks}.jpg`` — the filename *is* the label (CLAUDE.md §6),
  so a rename that skipped the files would silently re-label the training set.
  Only ``models/<id>/images/`` is touched; a file whose target name already
  exists is left alone rather than overwritten.
* **Parent *assignments* stage until Save**, so an auto-suggest run can be
  reviewed before it is written; parent and headstamp CRUD commit immediately.
  Closing with staged edits asks first.
* **Auto-suggest groups by leading alpha prefix** (``prefix_groups``), never
  overrides an assignment that already exists, and creates the parent rows it
  needs even when every child turns out to be spoken for.

Two things about the mechanics:

* **Slots are editable here and write through immediately**, like every other
  slot surface. Each write posts ``run/assignment_changed`` and re-syncs the
  active sorting template (only when this model is the active one: the
  template API is scoped to the active model).
* **A rename's file work runs on a QThread**, reporting back through a queued
  signal — a well-used model has thousands of images.

``notify`` / ``confirm`` / ``ask_text`` are instance attributes so tests drive
every path without a modal.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..data import image_store
from ..data.models import Headstamp, HeadstampParent
from ..data.repository import HeadstampParentRepo, HeadstampRepo, ModelRepo, SettingsRepo
from ..paths import model_images_dir

# A headstamp name becomes a filename prefix, and `__` is the field separator.
INVALID_NAME_CHARS = re.compile(r"""[#<>:'"/\\|*?,]""")

NONE_LABEL = "(none)"
UNASSIGNED_SLOT = "—"
# QSS can't reach a tree item, so the suggestion marker is text — which also
# survives a theme with no spare hue.
SUGGESTED_MARK = "  (suggested)"

COLUMNS = ("Headstamp", "Slot", "Parent")
PARENT_HINT = "Parent assignments are staged — press Save to write them."
RENAME_HINT = "Renaming a headstamp also renames its training images on disk."


def clean_name(name: str) -> str:
    """Invalid characters become ``-``; ``__`` collapses."""
    cleaned = INVALID_NAME_CHARS.sub("-", name or "")
    return cleaned.replace("__", "_").strip()


def prefix_groups(names: list[str]) -> dict[str, list[str]]:
    """Group names by leading alpha prefix, keeping groups of two or more.

    The auto-suggest rule: the prefix is the leading
    ``[A-Za-z]+`` run, upper-cased, and it becomes the parent's name. Names
    that don't start with a letter are skipped entirely.
    """
    groups: dict[str, list[str]] = {}
    for name in names:
        match = re.match(r"[A-Za-z]+", name)
        if not match:
            continue
        groups.setdefault(match.group(0).upper(), []).append(name)
    return {k: v for k, v in groups.items() if len(v) >= 2}


class _ImageRenameWorker(QThread):
    """Re-prefixes one label's training images off the UI thread."""

    done = Signal(str, str, int)  # old name, new name, files renamed

    def __init__(self, images_dir: Path, old: str, new: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dir = images_dir
        self._old = old
        self._new = new

    def run(self) -> None:
        renamed = 0
        for path in image_store.list_images(self._dir):
            if image_store.parse_headstamp(path) != self._old:
                continue
            # reclassify() skips an existing target and swallows OSError:
            # renaming a training set is best-effort, never all-or-nothing.
            if image_store.reclassify(path, self._new) is not None:
                renamed += 1
        self.done.emit(self._old, self._new, renamed)


class HeadstampManagerDialog(QDialog):
    """One model's headstamp list, their slots, and their parent groups."""

    changed = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        config: Any,
        model_id: int,
        *,
        bus: Any = None,
        images_dir: Path | str | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.db = config.db
        self.model_id = int(model_id)
        self.bus = bus
        self.headstamps = HeadstampRepo(self.db)
        self.parents = HeadstampParentRepo(self.db)
        self.settings = SettingsRepo(self.db)
        self.images_dir = Path(images_dir) if images_dir is not None else model_images_dir(self.model_id)

        model = ModelRepo(self.db).get(self.model_id)
        self.setWindowTitle(f"Headstamps — {model.name}" if model is not None else "Headstamps")
        self.resize(820, 520)

        # Staged parent assignments: headstamp id -> parent id | None. Written
        # on Save, never before.
        self._assignments: dict[int, int | None] = {}
        self._suggested: set[int] = set()
        self._dirty = False  # staged assignments not yet written
        self._mutated = False  # something *was* written; the app should re-read
        self._syncing = False  # guards the slot/parent editors while they follow the selection
        self._rows: list[Headstamp] = []
        self._parents_cache: list[HeadstampParent] = []
        self._workers: list[_ImageRenameWorker] = []

        # Attributes, not methods: a test replaces them so nothing modal opens.
        self.notify: Callable[[str, str], None] = self._warn
        self.confirm: Callable[[str, str], bool] = self._ask
        self.ask_text: Callable[[str, str, str], str | None] = self._ask_text

        self._build_ui()
        self._load_assignments()
        self.refresh()

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._build_parents_group())
        splitter.addWidget(self._build_headstamps_group())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        column.addWidget(splitter, 1)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("rowHint")
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_button = QPushButton("Save", self)
        self.save_button.setObjectName("action")
        self.save_button.clicked.connect(self.save)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)
        column.addLayout(buttons)

    def _build_parents_group(self) -> QGroupBox:
        box = QGroupBox("Parents", self)
        column = QVBoxLayout(box)

        self.parent_list = QListWidget(box)
        self.parent_list.currentRowChanged.connect(lambda _row: self._update_parent_buttons())
        column.addWidget(self.parent_list, 1)

        add_row = QHBoxLayout()
        self.parent_edit = QLineEdit(box)
        self.parent_edit.setPlaceholderText("New parent")
        self.parent_edit.returnPressed.connect(self.add_parent)
        self.parent_add_button = QPushButton("Add", box)
        self.parent_add_button.clicked.connect(self.add_parent)
        add_row.addWidget(self.parent_edit, 1)
        add_row.addWidget(self.parent_add_button)
        column.addLayout(add_row)

        edit_row = QHBoxLayout()
        self.parent_rename_button = QPushButton("Rename…", box)
        self.parent_rename_button.clicked.connect(self.rename_parent)
        self.parent_delete_button = QPushButton("Delete", box)
        self.parent_delete_button.setObjectName("danger")
        self.parent_delete_button.clicked.connect(self.delete_parent)
        edit_row.addWidget(self.parent_rename_button)
        edit_row.addWidget(self.parent_delete_button)
        column.addLayout(edit_row)

        self.suggest_button = QPushButton("Auto-suggest parents", box)
        self.suggest_button.clicked.connect(self.auto_suggest)
        column.addWidget(self.suggest_button)
        return box

    def _build_headstamps_group(self) -> QGroupBox:
        box = QGroupBox("Headstamps", self)
        column = QVBoxLayout(box)

        self.tree = QTreeWidget(box)
        self.tree.setObjectName("headstampTable")
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.tree.currentItemChanged.connect(lambda _cur, _prev: self._on_selection_changed())
        self.tree.itemDoubleClicked.connect(lambda _item, _col: self.rename_headstamp())
        column.addWidget(self.tree, 1)

        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("New", box))
        self.name_edit = QLineEdit(box)
        self.name_edit.setPlaceholderText("Headstamp name")
        self.name_edit.returnPressed.connect(self.add_headstamp)
        self.add_button = QPushButton("Add headstamp", box)
        self.add_button.setObjectName("action")
        self.add_button.clicked.connect(self.add_headstamp)
        add_row.addWidget(self.name_edit, 1)
        add_row.addWidget(self.add_button)
        column.addLayout(add_row)

        action_row = QHBoxLayout()
        self.rename_button = QPushButton("Rename…", box)
        self.rename_button.clicked.connect(self.rename_headstamp)
        self.delete_button = QPushButton("Delete", box)
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete_headstamp)
        action_row.addWidget(self.rename_button)
        action_row.addWidget(self.delete_button)
        action_row.addSpacing(12)
        action_row.addWidget(QLabel("Slot", box))
        self.slot_spin = QSpinBox(box)
        self.slot_spin.setRange(0, int(self.config.serial.get("slot_quantity", 8)))
        self.slot_spin.valueChanged.connect(self._on_slot_changed)
        action_row.addWidget(self.slot_spin)
        action_row.addSpacing(12)
        action_row.addWidget(QLabel("Parent", box))
        self.parent_combo = QComboBox(box)
        self.parent_combo.setMinimumWidth(150)
        self.parent_combo.currentIndexChanged.connect(self._on_parent_changed)
        action_row.addWidget(self.parent_combo)
        action_row.addStretch(1)
        column.addLayout(action_row)

        hint = QLabel(f"{PARENT_HINT}  {RENAME_HINT}", box)
        hint.setObjectName("dialogHint")
        hint.setWordWrap(True)
        column.addWidget(hint)
        return box

    # ----- message hooks ------------------------------------------------------

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _ask_text(self, title: str, label: str, initial: str) -> str | None:
        text, ok = QInputDialog.getText(self, title, label, text=initial)
        return text if ok else None

    def _confirm_clean(self, raw: str | None) -> str | None:
        """A cleaned name, or None when it's empty or the user declined the cleanup."""
        if raw is None:
            return None
        name = raw.strip()
        cleaned = clean_name(name)
        if not cleaned:
            return None
        if cleaned != name and not self.confirm(
            "Clean name",
            f"The name contains invalid characters and will be changed to:\n\n{cleaned}\n\nContinue?",
        ):
            return None
        return cleaned

    # ----- data ---------------------------------------------------------------

    def _load_assignments(self) -> None:
        self._assignments = {hs.id: hs.parent_id for hs in self.headstamps.list_for_model(self.model_id)}
        self._suggested.clear()
        self._dirty = False

    def refresh(self) -> None:
        self._refresh_parents()
        self._refresh_headstamps()
        self._update_status()

    def _refresh_parents(self) -> None:
        selected = self.selected_parent_id()
        self._parents_cache = self.parents.list_for_model(self.model_id)
        self.parent_list.blockSignals(True)
        self.parent_list.clear()
        for parent in self._parents_cache:
            item = QListWidgetItem(parent.name)
            item.setData(Qt.ItemDataRole.UserRole, parent.id)
            self.parent_list.addItem(item)
            if parent.id == selected:
                self.parent_list.setCurrentItem(item)
        self.parent_list.blockSignals(False)
        self._refresh_parent_combo()
        self._update_parent_buttons()

    def _refresh_parent_combo(self) -> None:
        self._syncing = True
        self.parent_combo.clear()
        self.parent_combo.addItem(NONE_LABEL, None)
        for parent in self._parents_cache:
            self.parent_combo.addItem(parent.name, parent.id)
        self._syncing = False

    def _refresh_headstamps(self) -> None:
        selected = self.selected_headstamp_id()
        self._rows = self.headstamps.list_for_model(self.model_id)
        self.tree.clear()
        for headstamp in self._rows:
            # Only seed rows we haven't staged an edit for — a refresh must not
            # discard unsaved assignments.
            self._assignments.setdefault(headstamp.id, headstamp.parent_id)
            item = QTreeWidgetItem(self.tree, self._row_values(headstamp))
            item.setData(0, Qt.ItemDataRole.UserRole, headstamp.id)
            if headstamp.id == selected:
                self.tree.setCurrentItem(item)
        first = self.tree.topLevelItem(0)
        if self.tree.currentItem() is None and first is not None:
            self.tree.setCurrentItem(first)
        self._on_selection_changed()

    def _row_values(self, headstamp: Headstamp) -> list[str]:
        slot = int(headstamp.slot)
        return [
            headstamp.name,
            str(slot) if slot else UNASSIGNED_SLOT,
            self._parent_cell(headstamp.id),
        ]

    def _parent_cell(self, headstamp_id: int) -> str:
        name = self._parent_name(self._assignments.get(headstamp_id)) or NONE_LABEL
        return name + (SUGGESTED_MARK if headstamp_id in self._suggested else "")

    def _parent_name(self, parent_id: int | None) -> str | None:
        if parent_id is None:
            return None
        return next((p.name for p in self._parents_cache if p.id == parent_id), None)

    # ----- selection ----------------------------------------------------------

    def selected_headstamp_id(self) -> int | None:
        item = self.tree.currentItem()
        return None if item is None else int(item.data(0, Qt.ItemDataRole.UserRole))

    def selected_headstamp(self) -> Headstamp | None:
        headstamp_id = self.selected_headstamp_id()
        return next((h for h in self._rows if h.id == headstamp_id), None)

    def selected_parent_id(self) -> int | None:
        item = self.parent_list.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    def selected_parent(self) -> HeadstampParent | None:
        parent_id = self.selected_parent_id()
        return next((p for p in self._parents_cache if p.id == parent_id), None)

    def _on_selection_changed(self) -> None:
        headstamp = self.selected_headstamp()
        for widget in (self.rename_button, self.delete_button, self.slot_spin, self.parent_combo):
            widget.setEnabled(headstamp is not None)
        self._syncing = True
        self.slot_spin.setValue(int(headstamp.slot) if headstamp is not None else 0)
        staged = self._assignments.get(headstamp.id) if headstamp is not None else None
        index = self.parent_combo.findData(staged)
        self.parent_combo.setCurrentIndex(max(0, index))
        self._syncing = False

    def _update_parent_buttons(self) -> None:
        has_parent = self.selected_parent_id() is not None
        self.parent_rename_button.setEnabled(has_parent)
        self.parent_delete_button.setEnabled(has_parent)

    def _update_status(self) -> None:
        count = len(self._suggested)
        self.status_label.setText(
            f"{count} suggestion{'' if count == 1 else 's'} applied — review and Save" if count else ""
        )

    # ----- write-through helpers ----------------------------------------------

    def _announce(self) -> None:
        """Nudge the Sort grid — it re-reads assignments off this topic."""
        self._mutated = True
        if self.bus is not None:
            self.bus.post("run/assignment_changed", {"source": "headstamps", "model_id": self.model_id})

    def _sync_template(self) -> None:
        """Keep the active sorting template in lock-step (CLAUDE.md §4).

        Only when this model is the active one: the template API resolves its
        scope from the active model, so syncing for any other model would
        overwrite the active model's template with this one's assignments.
        """
        if self.settings.get_active_model_id() == self.model_id:
            self.config.sync_active_slot_template("standard")

    # ----- headstamp CRUD -----------------------------------------------------

    def add_headstamp(self) -> None:
        name = self._confirm_clean(self.name_edit.text())
        if not name:
            return
        existing = {h.name.casefold() for h in self.headstamps.list_for_model(self.model_id)}
        if name.casefold() in existing:
            self.notify("Duplicate", "A headstamp with that name already exists.")
            return
        try:
            with self.db.transaction():
                headstamp = self.headstamps.add(self.model_id, name)
        except Exception as exc:
            self.notify("Add failed", str(exc))
            return
        self._assignments[headstamp.id] = None
        self.name_edit.clear()
        self._announce()
        self._refresh_headstamps()
        self._select_headstamp(headstamp.id)
        self.status_label.setText(f"Added '{name}'.")

    def _select_headstamp(self, headstamp_id: int) -> None:
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item is not None and int(item.data(0, Qt.ItemDataRole.UserRole)) == headstamp_id:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return

    def rename_headstamp(self) -> None:
        headstamp = self.selected_headstamp()
        if headstamp is None:
            return
        name = self._confirm_clean(self.ask_text("Rename headstamp", "New name:", headstamp.name))
        if not name or name == headstamp.name:
            return
        existing = {h.name.casefold(): h.id for h in self.headstamps.list_for_model(self.model_id)}
        if existing.get(name.casefold(), headstamp.id) != headstamp.id:
            self.notify("Duplicate", "A headstamp with that name already exists.")
            return
        with self.db.transaction():
            self.headstamps.rename(headstamp.id, name)
        # The template payload is name-keyed, so a rename has to re-snapshot it
        # or the layout still points at a name nothing answers to, and the
        # assignment silently drops next time the template applies.
        self._sync_template()
        self._announce()
        self._refresh_headstamps()
        self.status_label.setText(f"Renamed '{headstamp.name}' to '{name}' — renaming images…")
        self._start_image_rename(headstamp.name, name)

    def delete_headstamp(self) -> None:
        headstamp = self.selected_headstamp()
        if headstamp is None:
            return
        if not self.confirm("Delete headstamp", f'Delete headstamp "{headstamp.name}"?'):
            return
        with self.db.transaction():
            self.headstamps.delete(headstamp.id)
        self._assignments.pop(headstamp.id, None)
        self._suggested.discard(headstamp.id)
        self._sync_template()
        self._announce()
        self._refresh_headstamps()
        self._update_status()
        self.status_label.setText(f"Deleted '{headstamp.name}'. Its training images were left on disk.")

    def _on_slot_changed(self, value: int) -> None:
        headstamp = self.selected_headstamp()
        if self._syncing or headstamp is None or int(headstamp.slot) == int(value):
            return
        with self.db.transaction():
            self.headstamps.update_slot(headstamp.id, int(value))
        self._sync_template()
        self._announce()
        self._refresh_headstamps()
        self.status_label.setText(f"'{headstamp.name}' → slot {value}." if value else f"'{headstamp.name}' unassigned.")

    # ----- training-image renames (worker thread) -----------------------------

    @property
    def renaming(self) -> bool:
        return any(worker.isRunning() for worker in self._workers)

    def _start_image_rename(self, old: str, new: str) -> None:
        worker = _ImageRenameWorker(self.images_dir, old, new, self)
        worker.done.connect(self._on_images_renamed)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._workers.append(worker)
        worker.start()

    def _on_worker_finished(self, worker: _ImageRenameWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def _on_images_renamed(self, old: str, new: str, count: int) -> None:
        self.status_label.setText(
            f"Renamed '{old}' to '{new}' — {count} training image{'' if count == 1 else 's'} renamed."
        )

    # ----- parent CRUD --------------------------------------------------------

    def add_parent(self) -> None:
        name = self._confirm_clean(self.parent_edit.text())
        if not name:
            return
        if self.parents.find_by_name(self.model_id, name) is not None:
            self.notify("Duplicate", "A parent with that name already exists.")
            return
        with self.db.transaction():
            self.parents.add(self.model_id, name)
        self.parent_edit.clear()
        self._mutated = True
        self._refresh_parents()
        self._refresh_headstamps()
        self.status_label.setText(f"Added parent '{name}'.")

    def rename_parent(self) -> None:
        parent = self.selected_parent()
        if parent is None:
            return
        name = self._confirm_clean(self.ask_text("Rename parent", "New name:", parent.name))
        if not name or name == parent.name:
            return
        other = self.parents.find_by_name(self.model_id, name)
        if other is not None and other.id != parent.id:
            self.notify("Duplicate", "A parent with that name already exists.")
            return
        with self.db.transaction():
            self.parents.rename(parent.id, name)
        self._sync_template()
        self._announce()
        self._refresh_parents()
        self._refresh_headstamps()
        self.status_label.setText(f"Renamed parent '{parent.name}' to '{name}'.")

    def delete_parent(self) -> None:
        parent = self.selected_parent()
        if parent is None:
            return
        if not self.confirm(
            "Delete parent",
            f'Delete parent "{parent.name}"? Its child headstamps will be unlinked but not deleted.',
        ):
            return
        # Staged assignments pointing at it go too, so Save can't resurrect it.
        for headstamp_id, parent_id in list(self._assignments.items()):
            if parent_id == parent.id:
                self._assignments[headstamp_id] = None
        self._suggested.clear()
        with self.db.transaction():
            self.parents.delete(parent.id)
        self._sync_template()
        self._announce()
        self.refresh()
        self.status_label.setText(f"Deleted parent '{parent.name}'.")

    def _on_parent_changed(self, _index: int) -> None:
        """Stage an assignment. Nothing is written until Save."""
        headstamp = self.selected_headstamp()
        if self._syncing or headstamp is None:
            return
        parent_id = self.parent_combo.currentData()
        if parent_id == self._assignments.get(headstamp.id):
            return
        self._assignments[headstamp.id] = parent_id
        self._dirty = True
        self._suggested.discard(headstamp.id)
        item = self.tree.currentItem()
        if item is not None:
            item.setText(2, self._parent_cell(headstamp.id))
        self._update_status()

    # ----- auto-suggest -------------------------------------------------------

    def auto_suggest(self) -> None:
        rows = self.headstamps.list_for_model(self.model_id)
        if len(rows) < 2:
            self.notify("Auto-suggest", "Not enough headstamps to suggest parents.")
            return
        groups = prefix_groups(sorted(r.name for r in rows))
        if not groups:
            self.notify("Auto-suggest", "No common prefixes found to suggest parent groups.")
            return
        by_name = {r.name: r for r in rows}
        applied = 0
        with self.db.transaction():
            for prefix, members in groups.items():
                parent = self.parents.find_by_name(self.model_id, prefix)
                if parent is None:
                    parent = self.parents.add(self.model_id, prefix)
                for child in members:
                    headstamp = by_name.get(child)
                    # Never override an assignment the user already made.
                    if headstamp is None or self._assignments.get(headstamp.id) is not None:
                        continue
                    self._assignments[headstamp.id] = parent.id
                    self._suggested.add(headstamp.id)
                    applied += 1
        self._mutated = True  # the parent rows themselves are written either way
        self.refresh()
        if applied:
            self._dirty = True
        else:
            self.notify("Auto-suggest", "All headstamps already have parents assigned.")

    # ----- save / close -------------------------------------------------------

    def save(self) -> None:
        valid = {p.id for p in self._parents_cache}
        with self.db.transaction():
            for headstamp_id in list(self._assignments):
                parent_id = self._assignments[headstamp_id]
                if parent_id is not None and parent_id not in valid:
                    parent_id = None
                    self._assignments[headstamp_id] = None
                self.headstamps.set_parent(headstamp_id, parent_id)
        self._suggested.clear()
        self._dirty = False
        self._sync_template()
        self._announce()
        self._refresh_headstamps()
        self._update_status()
        self.status_label.setText("Saved.")

    def may_close(self) -> bool:
        return not self._dirty or self.confirm(
            "Unsaved changes",
            "You have unsaved parent assignments. Close without saving?",
        )

    def reject(self) -> None:
        """The single close funnel — Esc, the Close button and the window X.

        Qt's ``QDialog.closeEvent`` calls ``reject()`` and ignores the close
        event if the dialog is still visible afterwards, so refusing here
        refuses every route in.
        """
        if not self.may_close():
            return
        for worker in list(self._workers):
            worker.wait(2000)
        if self._mutated:
            # The Train page's label list and the Models row counts follow the
            # headstamp set.
            if self.bus is not None:
                self.bus.post("mode/changed", {"reason": "headstamps", "model_id": self.model_id})
            self.changed.emit()
        super().reject()
