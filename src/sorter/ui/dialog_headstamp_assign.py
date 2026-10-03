"""Headstamp-first slot assignment: every headstamp in one table, its slot set on the row.

The inverse of ``dialog_slot_assign`` (one bin, tick what goes in it), opened
from the Sort page's "Assign by headstamp…" button. Laying out a big model
becomes one keyboard pass: type a filter, ↓ or Enter into the list, type the
slot number, type the next name.

Keys in the table:

* A digit sets the current row's slot as soon as the number is unambiguous.
  With 8 slots every digit is; with 16, ``1`` waits for a second digit or
  Enter while ``2``–``9`` land at once. No timers.
* ``0``, Delete or Backspace send the row back to the Catch-All — slot 0 *is*
  "not routed", there is no separate unassigned state.
* Any other printable key starts a new search in the filter.

Every write goes through the same Config calls the per-slot dialog makes, so
the active sorting template follows (``sync_active_slot_template``) and
``changed`` repaints the dashboard's cards while this is still open. An edit
updates its cell in place; only the filter rebuilds or reorders the rows.

Modes mirror ``dialog_slot_assign``: with parent classifications on, parent
groups (plus ungrouped headstamps) carry the slot, and a Contains column —
also searched by the filter — lists each group's headstamps. Package mode is
many-to-many, so a typed slot toggles membership and the Slot column lists
every slot the headstamp fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QAbstractItemModel, QEvent, QModelIndex, QObject, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSpinBox,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .formatting import EMPTY_VALUE
from .name_filter import words_match
from .slot_grid import DEFAULT_SLOT_QUANTITY

NAME_COLUMN, SLOT_COLUMN, CONTAINS_COLUMN = 0, 1, 2
CATCH_ALL = "Catch-All"
DIGITS = "0123456789"

STANDARD_HINT = (
    "Type to filter, then ↓ or Enter to reach the list and type a slot number. "
    "0, Delete or Backspace sends a headstamp back to the Catch-All."
)
PARENT_HINT = (
    "Parent classifications are on: a parent group carries the slot and its headstamps "
    "route with it. Type to filter, then ↓ or Enter to reach the list and type a slot "
    "number. 0, Delete or Backspace sends a row back to the Catch-All."
)
PACKAGE_HINT = (
    "Package mode: a headstamp can fill several slots. Typing a slot number adds that "
    "slot, or takes it off again; 0, Delete or Backspace clears them all."
)


@dataclass
class AssignRow:
    """One assignable line: a headstamp, or a parent group in parent mode."""

    kind: str  # "headstamp" or "parent"
    name: str
    slots: list[int]  # empty = Catch-All
    parent_id: int | None = None
    contains: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.name


def slot_text(slots: list[int]) -> str:
    return ", ".join(str(slot) for slot in slots) if slots else EMPTY_VALUE


def _routed(slot: Any) -> list[int]:
    return [int(slot)] if int(slot) > 0 else []


class HeadstampAssignDialog(QDialog):
    """Every headstamp and its slot, editable from the keyboard. Writes are immediate."""

    changed = Signal()

    def __init__(self, config: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        # The visible rows, in table order.
        self.rows: list[AssignRow] = []
        # Digits typed toward a slot number that could still grow (e.g. "1" of 12).
        self.pending = ""

        self.setWindowTitle("Assign by headstamp")
        self.setMinimumSize(460, 480)
        column = QVBoxLayout(self)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("dialogHint")
        self.hint_label.setWordWrap(True)
        column.addWidget(self.hint_label)

        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText("Filter headstamps…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        self.filter_edit.installEventFilter(self)
        column.addWidget(self.filter_edit)

        self.table = AssignTable(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Headstamp", "Slot", "Contains"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Keys are ours (handle_table_key); the spinbox opens on double-click or F2.
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setItemDelegateForColumn(SLOT_COLUMN, SlotDelegate(self))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SLOT_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(CONTAINS_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        self.table.currentCellChanged.connect(self._on_current_changed)
        column.addWidget(self.table, 1)

        self.count_label = QLabel("", self)
        self.count_label.setObjectName("dialogHint")
        column.addWidget(self.count_label)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("dialogHint")
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        self.refresh()
        self.filter_edit.setFocus()

    # ----- reading ------------------------------------------------------------

    def mode(self) -> str:
        """'package', 'parent' or 'standard' — the routing the run would use."""
        if self.config.run_package_mode:
            return "package"
        if self.config.use_parent_classifications and self.config.parents_with_slots():
            return "parent"
        return "standard"

    def highest_slot(self) -> int:
        """The top bin number; ``slot_quantity`` counts the Catch-All, as in SlotGrid."""
        return max(1, int(self.config.serial.get("slot_quantity", DEFAULT_SLOT_QUANTITY))) - 1

    def _read_rows(self) -> list[AssignRow]:
        """Every assignable row, fresh from Config, alphabetical."""
        mode = self.mode()
        headstamps = self.config.headstamps_with_parents()
        if mode == "package":
            slot_map = self.config.package_slot_map()
            rows = [
                AssignRow(
                    "headstamp", h["name"], sorted(s for s, names in slot_map.items() if s > 0 and h["name"] in names)
                )
                for h in headstamps
            ]
        elif mode == "parent":
            members: dict[int, list[str]] = {}
            for h in headstamps:
                if h["parent_id"] is not None:
                    members.setdefault(int(h["parent_id"]), []).append(h["name"])
            rows = [
                AssignRow(
                    "parent",
                    p["name"],
                    _routed(p["slot"]),
                    parent_id=int(p["id"]),
                    contains=sorted(members.get(int(p["id"]), []), key=str.casefold),
                )
                for p in self.config.parents_with_slots()
            ]
            # Children route through their parent, so only orphans carry a slot here.
            rows += [
                AssignRow("headstamp", h["name"], _routed(h["slot"])) for h in headstamps if h["parent_id"] is None
            ]
        else:
            rows = [AssignRow("headstamp", h["name"], _routed(h["slot"])) for h in headstamps]
        return sorted(rows, key=lambda row: row.name.casefold())

    def current_row(self) -> AssignRow | None:
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    # ----- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the table from Config through the filter, the top match current.

        Only opening and a filter change get here — an edit re-reads its cells
        in place (``_after_change``). So the current row is never carried over:
        a row edited a moment ago that still matches the next search would
        otherwise stay current, and the next digit would overwrite *its* slot
        instead of landing on the match the user just searched for.
        """
        mode = self.mode()
        self.hint_label.setText({"package": PACKAGE_HINT, "parent": PARENT_HINT}.get(mode, STANDARD_HINT))
        self.table.setColumnHidden(CONTAINS_COLUMN, mode != "parent")

        everything = self._read_rows()
        needle = self.filter_edit.text()
        self.rows = [row for row in everything if words_match(needle, row.name, *row.contains)]
        self.pending = ""

        self.table.setRowCount(0)
        self.table.setRowCount(len(self.rows))
        editable = mode != "package"
        for index, row in enumerate(self.rows):
            name = QTableWidgetItem(row.name)
            name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if row.kind == "parent":
                name.setToolTip("Parent group — its headstamps route with it")
            slot = QTableWidgetItem(slot_text(row.slots))
            slot.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            slot_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            slot.setFlags(slot_flags | Qt.ItemFlag.ItemIsEditable if editable else slot_flags)
            contains = QTableWidgetItem(", ".join(row.contains))
            contains.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            contains.setToolTip("\n".join(row.contains))
            self.table.setItem(index, NAME_COLUMN, name)
            self.table.setItem(index, SLOT_COLUMN, slot)
            self.table.setItem(index, CONTAINS_COLUMN, contains)

        if self.rows:
            # The top match is current, so Enter-then-digit from the filter lands on it.
            self.table.setCurrentCell(0, NAME_COLUMN)
        self._show_counts(everything)

    def _show_counts(self, everything: list[AssignRow]) -> None:
        unrouted = sum(1 for row in everything if not row.slots)
        shown = (
            f"Showing {len(self.rows)} of {len(everything)}"
            if len(self.rows) != len(everything)
            else f"{len(everything)} rows"
        )
        self.count_label.setText(f"{shown} · {unrouted} still go to the Catch-All")

    def _set_slot_cell(self, index: int, text: str) -> None:
        item = self.table.item(index, SLOT_COLUMN)
        if item is not None:
            item.setText(text)

    def _on_filter_changed(self, _text: str) -> None:
        self.refresh()

    # ----- keyboard -----------------------------------------------------------

    def focus_table(self) -> None:
        """From the filter into the list, on the top match unless a row is already current."""
        if not self.rows:
            return
        if self.table.currentRow() < 0:
            self.table.setCurrentCell(0, NAME_COLUMN)
        self.table.setFocus()

    def start_search(self, text: str) -> None:
        """A letter typed in the list is a new search, not a refinement of the old one."""
        self.filter_edit.setText(text)
        self.filter_edit.setFocus()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.filter_edit and event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            # Enter is consumed here: ignored by QLineEdit, it would reach the Close button.
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.focus_table()
                return True
        return super().eventFilter(watched, event)

    def handle_table_key(self, event: QKeyEvent) -> bool:
        """The list's own keys; True means handled. Anything else is the view's."""
        modifiers = (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        )
        if event.modifiers() & modifiers:
            return False
        key, text = event.key(), event.text()
        index = self.table.currentRow()
        if len(text) == 1 and text in DIGITS:
            if index >= 0:
                self._type_digit(index, text)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Handled even with nothing pending: it must never reach the Close button.
            if self.pending and index >= 0:
                self._commit_pending(index)
            return True
        if key == Qt.Key.Key_Escape and self.pending:
            self._cancel_pending(index)
            return True
        if key == Qt.Key.Key_Backspace and self.pending:
            self.pending = self.pending[:-1]
            self._show_pending(index)
            return True
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._cancel_pending(index)
            if index >= 0:
                self.assign(index, 0)
            return True
        if key == Qt.Key.Key_F2:
            self._edit_slot(index)
            return True
        if text and text.isprintable() and not text.isspace():
            self.start_search(text)
            return True
        return False

    def _type_digit(self, index: int, digit: str) -> None:
        self.pending += digit
        number, top = int(self.pending), self.highest_slot()
        if number > top:
            self._cancel_pending(index)
            self.status_label.setText(
                f"There is no slot {number} — this machine's slots run 1 to {top}."
                if top
                else "This machine has only the Catch-All."
            )
        elif number == 0 or number * 10 > top:
            self._commit_pending(index)
        else:
            self._show_pending(index)

    def _show_pending(self, index: int) -> None:
        if 0 <= index < len(self.rows):
            self._set_slot_cell(index, f"{self.pending}…" if self.pending else slot_text(self.rows[index].slots))
        if self.pending:
            self.status_label.setText(
                f"Slot {self.pending}… — type another digit, or Enter for slot {int(self.pending)}."
            )
        else:
            self.status_label.clear()

    def _cancel_pending(self, index: int) -> None:
        self.pending = ""
        self._show_pending(index)

    def _commit_pending(self, index: int) -> None:
        number = int(self.pending)
        self.pending = ""
        self.assign(index, number)

    def _on_current_changed(self, _row: int, _column: int, previous_row: int, _previous_column: int) -> None:
        # Moving off a row abandons a half-typed number on it.
        if self.pending:
            self._cancel_pending(previous_row)

    def _edit_slot(self, index: int) -> None:
        if self.mode() == "package" or not 0 <= index < len(self.rows):
            return
        item = self.table.item(index, SLOT_COLUMN)
        if item is not None:
            self.table.editItem(item)

    def _on_double_clicked(self, row: int, _column: int) -> None:
        self._edit_slot(row)

    # ----- mutations ----------------------------------------------------------

    def assign(self, index: int, slot: int) -> None:
        """Route row ``index`` to ``slot`` (0 = Catch-All); in package mode, toggle it."""
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        slot = int(slot)
        if self.mode() == "package":
            if slot == 0:
                for held in row.slots:
                    self.config.set_package_slot_headstamp(held, row.name, False)
            else:
                self.config.set_package_slot_headstamp(slot, row.name, slot not in row.slots)
        elif row.kind == "parent" and row.parent_id is not None:
            self.config.set_parent_slot(row.parent_id, slot)
        else:
            self.config.set_headstamp_slot(row.name, slot)
        self._after_change(index)

    def _after_change(self, index: int) -> None:
        """Re-read every visible row's slot in place — no rebuild, no reorder."""
        fresh = {row.key: row for row in self._read_rows()}
        for position, row in enumerate(self.rows):
            updated = fresh.get(row.key)
            if updated is not None:
                self.rows[position] = updated
                self._set_slot_cell(position, slot_text(updated.slots))
        self._show_counts(list(fresh.values()))
        row = self.rows[index]
        where = ("slots " if len(row.slots) > 1 else "slot ") + slot_text(row.slots) if row.slots else CATCH_ALL
        self.status_label.setText(f"{row.name} → {where}")
        self.changed.emit()


class AssignTable(QTableWidget):
    """The list, handing its keys to the dialog before the view's defaults."""

    def __init__(self, dialog: HeadstampAssignDialog) -> None:
        super().__init__(dialog)
        self._dialog = dialog

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._dialog.handle_table_key(event):
            event.accept()
            return
        super().keyPressEvent(event)


class SlotDelegate(QStyledItemDelegate):
    """The mouse path: a spinbox on the Slot cell, committed through ``assign``."""

    def __init__(self, dialog: HeadstampAssignDialog) -> None:
        super().__init__(dialog)
        self._dialog = dialog

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QWidget:
        spin = QSpinBox(parent)
        spin.setRange(0, self._dialog.highest_slot())
        spin.setSpecialValueText(CATCH_ALL)
        return spin

    def setEditorData(self, editor: QWidget, index: QModelIndex | QPersistentModelIndex) -> None:
        if isinstance(editor, QSpinBox) and 0 <= index.row() < len(self._dialog.rows):
            slots = self._dialog.rows[index.row()].slots
            editor.setValue(slots[0] if slots else 0)

    def setModelData(
        self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        # Config is the model here; assign() writes it and repaints the cell.
        if not isinstance(editor, QSpinBox) or not 0 <= index.row() < len(self._dialog.rows):
            return
        editor.interpretText()
        slots = self._dialog.rows[index.row()].slots
        if editor.value() != (slots[0] if slots else 0):
            self._dialog.assign(index.row(), editor.value())


def build_headstamp_assign_dialog(win: Any) -> HeadstampAssignDialog:
    """The dialog over the window's config; the caller wires ``changed`` and opens it."""
    return HeadstampAssignDialog(win.config, win)
