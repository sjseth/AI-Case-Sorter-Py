"""Assignment editor for one slot — the Qt form of the Tk Run tab's slot details.

Opened by clicking a slot card. Every tick writes straight through to
``Config`` (which keeps the active sorting template in lock-step), then the
rows are re-read from the DB — nothing about the assignment state is cached
here, and the dashboard refreshes off the ``changed`` signal.

Three routing modes, the same three the Tk panel renders:

* **standard** — a headstamp belongs to exactly one slot. Ticking it here
  moves it off whichever slot it was in; the row says which one that is.
* **parent** — parent groups (plus ungrouped headstamps) carry the slot,
  because that is what routing reads once parent classifications are on.
* **package** — many-to-many: the same headstamp may fill several bins.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
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

CATCH_ALL_HINT = "Anything we can't classify or that isn't mapped to a slot ends up here."
PACKAGE_HINT = (
    "Tick a headstamp to batch it into this slot. The same headstamp can fill "
    "several slots — they're filled one batch at a time."
)
PARENT_HINT = "Tick a parent group (or an ungrouped headstamp) to route it here."
STANDARD_HINT = "Tick a headstamp to route it here. A headstamp belongs to one slot at a time."


class SlotAssignDialog(QDialog):
    """Per-slot headstamp assignment. Mutations are immediate, not on OK."""

    changed = Signal()

    def __init__(self, config: Any, slot: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.slot = int(slot)
        # label -> checkbox, rebuilt on every render; parent rows share it.
        self.checkboxes: dict[str, QCheckBox] = {}

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
        self.filter_edit.textChanged.connect(lambda _text: self.refresh())
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

        self.refresh()

    # ----- rendering ----------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild every row from a fresh read of Config."""
        self._clear_rows()
        needle = self.filter_edit.text().strip().casefold()
        headstamps = sorted(self.config.headstamps_with_parents(), key=lambda h: h["name"].casefold())

        if self.slot == 0:
            self.hint_label.setText(CATCH_ALL_HINT)
            for entry in self._matching(headstamps, needle):
                self._add_row(entry["name"], checked=False, enabled=False)
        elif self.config.run_package_mode:
            self.hint_label.setText(PACKAGE_HINT)
            assigned = set(self.config.headstamps_in_package_slot(self.slot))
            for entry in self._matching(headstamps, needle):
                name = entry["name"]
                self._add_row(
                    name,
                    checked=name in assigned,
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
        for parent in sorted(parents, key=lambda p: p["name"].casefold()):
            name = parent["name"]
            if needle and needle not in name.casefold():
                continue
            pid = int(parent["id"])
            self._add_assignable_row(
                name,
                int(parent["slot"]),
                lambda checked, i=pid: self._toggle_parent(i, checked),
            )
        # Children route through their parent, so only orphans carry a slot here.
        self._render_standard([h for h in headstamps if h["parent_id"] is None], needle)

    def _render_standard(self, headstamps: list[dict], needle: str) -> None:
        for entry in self._matching(headstamps, needle):
            name = entry["name"]
            self._add_assignable_row(
                name,
                int(entry["slot"]),
                lambda checked, n=name: self._toggle_headstamp(n, checked),
            )

    @staticmethod
    def _matching(entries: list[dict], needle: str) -> list[dict]:
        return [e for e in entries if not needle or needle in e["name"].casefold()]

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
        box = QCheckBox(label, row)
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
