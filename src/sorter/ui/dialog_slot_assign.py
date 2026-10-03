"""Assignment editor for one slot.

Opened by clicking a slot card. Every tick writes straight through to
``Config`` (which keeps the active sorting template in lock-step), then the
rows are re-read from the DB — nothing about the assignment state is cached
here, and the dashboard refreshes off the ``changed`` signal.

Three routing modes:

* **standard** — a headstamp belongs to exactly one slot. Ticking it here
  moves it off whichever slot it was in; the row says which one that is.
* **parent** — parent groups (plus ungrouped headstamps) carry the slot,
  because that is what routing reads once parent classifications are on.
* **package** — many-to-many: the same headstamp may fill several bins.

Finding a row in a long list: the filter matches every whitespace-separated
word (``win 9`` finds ``WIN 9MM LUGER``), and Enter in it ticks or unticks the
row when exactly one is left. Rows are ordered this slot's first, then the
unassigned, then those in another slot — ranked when the dialog opens or the
filter changes, never on a tick, so a row doesn't jump out from under the
pointer.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .formatting import escape_mnemonic

CATCH_ALL_HINT = "Anything we can't classify or that isn't mapped to a slot ends up here."
PACKAGE_HINT = (
    "Tick a headstamp to batch it into this slot. The same headstamp can fill "
    "several slots — they're filled one batch at a time."
)
PARENT_HINT = "Tick a parent group (or an ungrouped headstamp) to route it here."
STANDARD_HINT = "Tick a headstamp to route it here. A headstamp belongs to one slot at a time."

# Row groups, top to bottom.
RANK_THIS_SLOT, RANK_UNASSIGNED, RANK_ELSEWHERE = 0, 1, 2


class SlotAssignDialog(QDialog):
    """Per-slot headstamp assignment. Mutations are immediate, not on OK."""

    changed = Signal()

    def __init__(self, config: Any, slot: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.slot = int(slot)
        # label -> checkbox, rebuilt on every render; parent rows share it.
        self.checkboxes: dict[str, QCheckBox] = {}
        # (kind, name) -> RANK_*, frozen by _resort() so a tick doesn't reorder.
        self._rank: dict[tuple[str, str], int] = {}

        self.setWindowTitle("Catch-All" if self.slot == 0 else f"Slot #{self.slot}")
        self.setMinimumWidth(380)
        column = QVBoxLayout(self)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("dialogHint")
        self.hint_label.setWordWrap(True)
        column.addWidget(self.hint_label)

        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText("Filter headstamps…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _text: self.refresh(resort=True))
        self.filter_edit.returnPressed.connect(self._toggle_single_match)
        column.addWidget(self.filter_edit)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget(scroll)
        self._rows = QVBoxLayout(host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(2)
        scroll.setWidget(host)
        column.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

        self.refresh(resort=True)
        self.filter_edit.setFocus()

    # ----- rendering ----------------------------------------------------------

    def refresh(self, *, resort: bool = False) -> None:
        """Rebuild every row from a fresh read of Config.

        ``resort`` re-ranks the rows by assignment; a tick leaves it off so the
        order stays where the user is looking.
        """
        if resort:
            self._resort()
        self._clear_rows()
        needle = self.filter_edit.text()
        headstamps = sorted(self.config.headstamps_with_parents(), key=lambda h: h["name"].casefold())

        if self.slot == 0:
            self.hint_label.setText(CATCH_ALL_HINT)
            for entry in self._matching(headstamps, needle):
                self._add_row(entry["name"], checked=False, enabled=False)
        elif self.config.run_package_mode:
            self.hint_label.setText(PACKAGE_HINT)
            slot_map = self.config.package_slot_map()
            for entry in self._ordered("headstamp", self._matching(headstamps, needle)):
                name = entry["name"]
                others = [s for s in sorted(slot_map) if s not in (0, self.slot) and name in slot_map[s]]
                self._add_row(
                    name,
                    checked=name in slot_map.get(self.slot, []),
                    hint="in " + ", ".join(f"slot #{s}" for s in others) if others else "",
                    on_toggle=lambda checked, n=name: self._toggle_package(n, checked),
                )
        else:
            parents = self.config.parents_with_slots()
            if parents and self.config.use_parent_classifications:
                self.hint_label.setText(PARENT_HINT)
                self._render_parent_mode(parents, headstamps, needle)
            else:
                self.hint_label.setText(STANDARD_HINT)
                self._render_standard(headstamps, needle)

        self._rows.addStretch(1)

    def _render_parent_mode(self, parents: list[dict], headstamps: list[dict], needle: str) -> None:
        parents = sorted(parents, key=lambda p: p["name"].casefold())
        for parent in self._ordered("parent", self._matching(parents, needle)):
            name = parent["name"]
            pid = int(parent["id"])
            self._add_assignable_row(
                name,
                int(parent["slot"]),
                lambda checked, i=pid: self._toggle_parent(i, checked),
            )
        # Children route through their parent, so only orphans carry a slot here.
        self._render_standard([h for h in headstamps if h["parent_id"] is None], needle)

    def _render_standard(self, headstamps: list[dict], needle: str) -> None:
        for entry in self._ordered("headstamp", self._matching(headstamps, needle)):
            name = entry["name"]
            self._add_assignable_row(
                name,
                int(entry["slot"]),
                lambda checked, n=name: self._toggle_headstamp(n, checked),
            )

    @staticmethod
    def _matching(entries: list[dict], needle: str) -> list[dict]:
        """Entries whose name contains every whitespace-separated word of ``needle``."""
        words = needle.casefold().split()
        return [e for e in entries if all(w in e["name"].casefold() for w in words)]

    def _resort(self) -> None:
        """Rank every row by where it is assigned right now."""

        def rank(slots: set[int]) -> int:
            if self.slot in slots:
                return RANK_THIS_SLOT
            return RANK_ELSEWHERE if slots - {0} else RANK_UNASSIGNED

        ranks: dict[tuple[str, str], int] = {}
        if self.config.run_package_mode:
            # Many-to-many: "assigned" means in any package slot at all.
            held: dict[str, set[int]] = {}
            for slot, names in self.config.package_slot_map().items():
                for name in names:
                    held.setdefault(name, set()).add(slot)
            for entry in self.config.headstamps_with_parents():
                ranks["headstamp", entry["name"]] = rank(held.get(entry["name"], set()))
        else:
            for entry in self.config.headstamps_with_parents():
                ranks["headstamp", entry["name"]] = rank({int(entry["slot"])})
            for parent in self.config.parents_with_slots():
                ranks["parent", parent["name"]] = rank({int(parent["slot"])})
        self._rank = ranks

    def _ordered(self, kind: str, entries: list[dict]) -> list[dict]:
        """``entries`` (already alphabetical) grouped by their frozen rank."""
        return sorted(entries, key=lambda e: self._rank.get((kind, e["name"]), RANK_UNASSIGNED))

    def _add_assignable_row(self, name: str, assigned: int, on_toggle: Any) -> None:
        """One single-slot row: ticked here, or ticked elsewhere and labelled so."""
        elsewhere = assigned if assigned not in (0, self.slot) else 0
        self._add_row(
            name,
            checked=assigned == self.slot,
            hint=f"in slot #{elsewhere}" if elsewhere else "",
            on_toggle=on_toggle,
        )

    def _add_row(
        self,
        label: str,
        *,
        checked: bool,
        enabled: bool = True,
        hint: str = "",
        on_toggle: Any = None,
    ) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        box = QCheckBox(escape_mnemonic(label), row)
        box.setChecked(checked)
        box.setEnabled(enabled and on_toggle is not None)
        if on_toggle is not None:
            # `clicked`, not `toggled`: only a real click may write to Config.
            box.clicked.connect(on_toggle)
        layout.addWidget(box)
        if hint:
            note = QLabel(hint, row)
            note.setObjectName("rowHint")
            layout.addWidget(note)
        layout.addStretch(1)
        self._rows.addWidget(row)
        self.checkboxes[label] = box

    def _clear_rows(self) -> None:
        # deleteLater, never a direct delete: a row is cleared from inside its
        # own checkbox's signal handler.
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.checkboxes.clear()

    # ----- keyboard -----------------------------------------------------------

    def _toggle_single_match(self) -> None:
        """Enter in the filter: tick or untick the row when it is the only one left."""
        boxes = [box for box in self.checkboxes.values() if box.isEnabled()]
        if len(boxes) == 1:
            boxes[0].click()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Enter in the filter is _toggle_single_match's alone; it must never
        # reach a default button and close the dialog mid-edit.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.focusWidget() is self.filter_edit:
            event.accept()
            return
        super().keyPressEvent(event)

    # ----- mutations ----------------------------------------------------------

    def _toggle_headstamp(self, name: str, checked: bool) -> None:
        if self.slot == 0:
            return
        self.config.set_headstamp_slot(name, self.slot if checked else 0)
        self._after_change()

    def _toggle_parent(self, parent_id: int, checked: bool) -> None:
        if self.slot == 0:
            return
        self.config.set_parent_slot(parent_id, self.slot if checked else 0)
        self._after_change()

    def _toggle_package(self, name: str, checked: bool) -> None:
        if self.slot == 0:
            return
        self.config.set_package_slot_headstamp(self.slot, name, checked)
        self._after_change()

    def _after_change(self) -> None:
        self.refresh()
        self.changed.emit()
