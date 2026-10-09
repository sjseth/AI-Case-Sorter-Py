"""Catch-All breakdown — the right-hand panel (CLAUDE.md §5).

Slot 0 is where a case goes when the run cannot put it in a bin. This panel
says which headstamps those were and why, counted from the same successful
``run/result`` events the slot card uses, so the header matches the card.
Counts survive Stop/Start; ``QtMainWindow._clear_counts`` is what zeroes them.

The table is items only — no cell widgets (CLAUDE.md §5). It ranks only
headstamps that would still land in slot 0 if seen now: giving one a slot
drops its unassigned and unknown cases so the next one moves up. Below
floor, upside down and batch full stay, and a mixed row shows only that
remainder. **Top 10** (the default) paints the first 10 of that ranking
plus a greyed Other row. **ALL** paints every one of them, and the table
scrolls. The two are an exclusive pair at the upper right of the panel;
the lit one uses a neutral fill — the accent ramp where that ramp is
not a stop colour, otherwise the text ramp — and the choice lasts for
the session. The header stays
the physical bin total. A line under the table names what left the ranking
and how many of those cases are already in bin 0.

Two actions sit on the selection bar under the table, keyed on the
headstamp name so a re-sort does not lose the selection. **Assign to empty
slot** is the primary ``#action`` control. **Add to existing slot…** opens a
menu of bins that already have brass (``#4 WMA, WMA NATO``) and shares that
bin. Either one then selects the next headstamp that can still be assigned.
``&`` in a name goes through ``formatting.escape_mnemonic``. Only a
below-floor reason takes the palette's warning colour ("Hue is meaning");
``apply_palette`` re-bakes that brush because an item foreground is outside
the stylesheet.

Subscribes ``run/result``, ``run/assignment_changed`` and ``mode/changed``
on ``win.bus``. A result does not re-read the database: the label-to-slot
map is cached until an assignment, a model change, or a reset (template and
package-mode switches clear the counts, which resets this panel).

A result while the dock is open waits briefly and then repaints once, in
place: cells, the header and the buttons change only when their text does,
and the scroll position stays put. The share menu is rebuilt only when its
entries change, and never while it is open — a case arriving mid-pick used
to ``clear()`` the menu and the popup flickered shut.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QButtonGroup,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..control.catch_all import (
    BELOW_FLOOR,
    EMPTY_KEY,
    CatchAllBucket,
    CatchAllTally,
    assigned_session_line,
    fixed_by_assignment,
    percent,
    reason_summary,
    reason_tooltip,
)
from .formatting import escape_mnemonic

COLUMNS = ("#", "Headstamp", "Count", "Share", "Reason")
COL_RANK, COL_NAME, COL_COUNT, COL_SHARE, COL_REASON = range(5)

_FALLBACK_WARNING = "#f59e0b"
_FALLBACK_MUTED = "#9a9a9a"


class _Routing(NamedTuple):
    """Slots and known labels for the panel, read once and reused per result."""

    slots: dict[str, list[int]]
    known: set[str]
    empty: int | None
    # Occupied bins in slot order: ``(4, ("WMA", "WMA NATO"))``. Matches the
    # slot cards — package map, else parent names plus orphans, else headstamps.
    occupied: tuple[tuple[int, tuple[str, ...]], ...]


def _occupy(groups: dict[int, list[str]], slot: int, name: str) -> None:
    if slot <= 0 or not name:
        return
    names = groups.setdefault(slot, [])
    if name not in names:
        names.append(name)


def _occupied_pairs(groups: dict[int, list[str]]) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Slot order, names within a slot sorted case-insensitively, blanks dropped."""
    pairs: list[tuple[int, tuple[str, ...]]] = []
    for slot in sorted(groups):
        if slot <= 0:
            continue
        seen: list[str] = []
        for name in groups[slot]:
            text = str(name).strip()
            if text and text not in seen:
                seen.append(text)
        if seen:
            pairs.append((slot, tuple(sorted(seen, key=str.casefold))))
    return tuple(pairs)


def _known_and_routed(config: Any) -> tuple[set[str], dict[str, int], dict[int, list[str]]]:
    """Names ``slot_for_headstamp`` would resolve, the slot, and who occupies bins.

    One pass over the headstamp list (and the parents, when that mode is on).
    Slot 0 is known but unassigned. A label the lookup would miss is absent.
    Occupancy follows the slot cards: a child's own slot column does not
    occupy a bin while parent mode routes it through the parent.
    """
    known: set[str] = set()
    routed: dict[str, int] = {}
    occupied: dict[int, list[str]] = {}
    active = config.settings.get_active_model_id()
    if active is not None and config.use_parent_classifications:
        parents = config.parents_with_slots()
        by_id = {int(parent["id"]): parent for parent in parents}
        if by_id:
            for entry in config.headstamps_with_parents():
                name = entry.get("name")
                if not name:
                    continue
                name = str(name)
                known.add(name)
                parent_id = entry.get("parent_id")
                if parent_id is not None and int(parent_id) in by_id:
                    routed[name] = int(by_id[int(parent_id)]["slot"])
                else:
                    own = int(entry.get("slot") or 0)
                    routed[name] = own
                    _occupy(occupied, own, name)
            for parent in parents:
                name = str(parent["name"])
                slot = int(parent["slot"])
                _occupy(occupied, slot, name)
                if name in known:
                    continue
                known.add(name)
                routed[name] = slot
            return known, routed, occupied
    for entry in config.headstamps:
        name = entry.get("name")
        if not name:
            continue
        name = str(name)
        known.add(name)
        slot = int(entry.get("slot") or 0)
        routed[name] = slot
        _occupy(occupied, slot, name)
    return known, routed, occupied


def _load_routing(config: Any) -> _Routing:
    """The panel's copy of "would this label still land in slot 0?"."""
    known, routed, occupied = _known_and_routed(config)
    if config.run_package_mode:
        # Package assignments win over parent and per-headstamp slots, the
        # same way the slot cards and ``first_empty_slot`` decide occupancy.
        slots: dict[str, list[int]] = {}
        occupied = {}
        for slot, names in config.package_slot_map().items():
            slot_n = int(slot)
            if slot_n <= 0:
                continue
            for name in names:
                text = str(name)
                slots.setdefault(text, []).append(slot_n)
                _occupy(occupied, slot_n, text)
        slots = {name: sorted(group) for name, group in slots.items()}
    else:
        slots = {name: [slot] for name, slot in routed.items() if slot > 0}
    return _Routing(
        slots=slots,
        known=known,
        empty=config.first_empty_slot(),
        occupied=_occupied_pairs(occupied),
    )


def _summary(tally: CatchAllTally) -> str:
    caught = tally.catch_all_total
    total = tally.total
    return f"{caught} in catch-all of {total} sorted ({percent(caught, total)}%)"


_TOP_TEXT = "Top 10"
_ALL_TEXT = "ALL"
_MODE_TOP = 0
_MODE_ALL = 1
_ADD_TEXT = "Add to existing slot…"
_SELECT_TIP = "Select a headstamp."
_UNKNOWN_TIP = "This label isn't in the model, so it can't be assigned to a slot."
_NO_OCCUPIED_TIP = "No slot has a headstamp yet."
_ADD_TIP = "Share a bin that already has brass. Cases already in the wheel still drop in the catch-all."
# One paint for a burst of cases, long enough that a running sort does not
# redraw the open slot menu, short enough that the header still feels live.
_RESULT_REFRESH_MS = 200


class _RowPaint(NamedTuple):
    """What one table row should show. Compared against the live items."""

    texts: tuple[str, str, str, str, str]
    key: str | None
    tip: str
    selectable: bool
    reason_color: str | None
    muted_color: str | None


class CatchAllView(QWidget):
    """The breakdown table, the empty-slot assign, and the share-a-bin menu."""

    def __init__(self, win: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._win = win
        self.tally = CatchAllTally()
        # The selected headstamp's key, not its row: a new case re-sorts the
        # table and the row number moves.
        self._selected_key: str | None = None
        # Keys assigned away from the ranking, in the order it happened.
        self._session_order: list[str] = []
        # Rows on screen: the first 10, or every open key when ALL is lit.
        # ``_bucket`` searches this, so a row past the tenth can be assigned.
        self._ranked: list[CatchAllBucket] = []
        # Top 10 until the user picks ALL. Remembered for this session; the
        # view lives as long as the window does, including while the dock is closed.
        self._show_all = False
        # Set for the refresh that follows the panel's own Assign click.
        self._prefer_next_assignable = False
        # Rebuilt on assignment, model change, and reset — not on each result.
        self._routing_cache: _Routing | None = None
        # A result arrived while the slot menu was open, or a coalesced paint
        # has not run yet. The menu close and the timer both consume it.
        self._refresh_pending = False
        # Lets the assign click repaint the table while its menu is still up
        # without treating that menu as something a result may rebuild.
        self._suppress_menu_guard = False
        self._selectable_flags: Qt.ItemFlag | None = None
        # How many widget writes the paints so far have actually performed.
        # A repeat paint of unchanged data leaves this still, which is the
        # flicker fix: setters are what close the menu and reset the scroll.
        self._widget_writes = 0
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_now)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        modes = QHBoxLayout()
        modes.setContentsMargins(0, 0, 0, 0)
        modes.setSpacing(6)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self.top_ten_button = self._mode_button(_TOP_TEXT)
        self.all_button = self._mode_button(_ALL_TEXT)
        self._mode_group.addButton(self.top_ten_button, _MODE_TOP)
        self._mode_group.addButton(self.all_button, _MODE_ALL)
        self.top_ten_button.setChecked(True)
        self._mode_group.idToggled.connect(self._on_mode_toggled)
        modes.addStretch(1)
        modes.addWidget(self.top_ten_button)
        modes.addWidget(self.all_button)
        outer.addLayout(modes)

        self.summary_label = QLabel(_summary(self.tally), self)
        self.summary_label.setObjectName("catchAllSummary")
        self.summary_label.setWordWrap(True)
        outer.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setObjectName("catchAllTable")
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(1)
        # AsNeeded is the Qt default. Spell it out so a later change cannot
        # turn the bar off, and keep the size hint from growing with the row
        # count — ALL scrolls inside the dock the user already sized.
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        outer.addWidget(self.table, 1)

        self.assigned_label = QLabel("", self)
        self.assigned_label.setObjectName("catchAllAssigned")
        self.assigned_label.setWordWrap(True)
        self.assigned_label.hide()
        outer.addWidget(self.assigned_label)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self.add_button = QPushButton(_ADD_TEXT, self)
        self.add_button.setObjectName("catchAllAdd")
        add_menu = QMenu(self.add_button)
        add_menu.aboutToShow.connect(self._sync_add_menu)
        add_menu.aboutToHide.connect(self._on_add_menu_about_to_hide)
        self.add_button.setMenu(add_menu)
        bar.addWidget(self.add_button)
        self.assign_button = QPushButton("Assign to empty slot", self)
        self.assign_button.setObjectName("action")
        self.assign_button.clicked.connect(self._assign_selected)
        bar.addWidget(self.assign_button)
        outer.addLayout(bar)

        self._update_button()
        win.bus.subscribe("run/result", self._on_result)
        win.bus.subscribe("run/assignment_changed", self._on_assignment_changed)
        win.bus.subscribe("mode/changed", self._on_mode_changed)

    # ----- bus -----------------------------------------------------------------

    def _on_result(self, result: Any) -> None:
        # Same gate as the slot card: a failed cycle carries a slot but did
        # not land, and counting it would drift from the card.
        if not isinstance(result, dict) or not result.get("ok"):
            return
        self.tally.add(result)
        self._schedule_refresh()

    def _on_assignment_changed(self, _payload: Any) -> None:
        # The tally is historical. The ranking reads the slot map, which just changed.
        self._invalidate_routing()
        self.refresh()

    def _on_mode_changed(self, _payload: Any) -> None:
        # A different model has a different headstamp list. Reset (via the
        # window's own handler) also drops the cache; this covers a mode
        # event that arrives without one.
        self._invalidate_routing()
        self.refresh()

    def reset(self) -> None:
        """Zero the breakdown. The dashboard's Reset counts is the caller.

        Template switches and package-mode toggles clear the counts, so this
        is also what drops a slot map that belonged to the previous layout.
        """
        self.tally.reset()
        self._selected_key = None
        self._session_order.clear()
        self._invalidate_routing()
        self.refresh()

    def apply_palette(self) -> None:
        """Re-bake the below-floor brush from the live palette."""
        self.refresh()

    # ----- table ---------------------------------------------------------------

    def _panel_open(self) -> bool:
        """True when this dock is on screen, which is when a sort can flicker it."""
        dock = getattr(self._win, "catch_all_dock", None)
        if dock is not None and dock.isClosed():
            return False
        return self.isVisible()

    def _menu_really_open(self) -> bool:
        menu = self.add_button.menu()
        return menu is not None and menu.isVisible()

    def _schedule_refresh(self) -> None:
        """Coalesce result paints while the dock is open.

        A hidden dock paints immediately, so a closed panel stays current and
        a test that never shows the window still sees the row after the bus
        drains. An open slot menu is not painted under at all.
        """
        if self._menu_really_open():
            self._refresh_pending = True
            return
        if not self._panel_open():
            self._refresh_now()
            return
        self._refresh_pending = True
        if not self._refresh_timer.isActive():
            self._refresh_timer.start(_RESULT_REFRESH_MS)

    def _on_add_menu_about_to_hide(self) -> None:
        """The pick is over. Apply any case that arrived while the menu was up."""
        if not self._refresh_pending:
            self._sync_add_menu()
            return
        # ``isVisible()`` is still true in ``aboutToHide``. Paint anyway; the
        # menu sync itself refuses to ``clear()`` a menu that is showing.
        self._suppress_menu_guard = True
        try:
            self._refresh_now()
        finally:
            self._suppress_menu_guard = False

    def refresh(self) -> None:
        """Repaint immediately. Assignment, reset, and theme changes use this."""
        self._suppress_menu_guard = True
        try:
            self._refresh_now()
        finally:
            self._suppress_menu_guard = False

    def _refresh_now(self) -> None:
        self._refresh_timer.stop()
        if self._menu_really_open() and not self._suppress_menu_guard:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        self._paint()

    def _paint(self) -> None:
        self._set_text(self.summary_label, _summary(self.tally))
        self._sync_session_order()
        self._paint_session_line()
        self._ranked, other = self.tally.open_ranking(self._has_slot, None if self._show_all else 10)
        selected = self._first_assignable_key() if self._prefer_next_assignable else self._selected_key
        self._paint_table(selected, other)
        self._update_button()

    def _sync_session_order(self) -> None:
        """Remember who left the ranking, and forget them once the slot is gone."""
        active = [
            bucket.key
            for bucket in self.tally.buckets()
            if self._has_slot(bucket.key) and fixed_by_assignment(bucket) > 0
        ]
        active_set = set(active)
        self._session_order = [key for key in self._session_order if key in active_set]
        for key in active:
            if key not in self._session_order:
                self._session_order.append(key)

    def _paint_session_line(self) -> None:
        by_key = {bucket.key: bucket for bucket in self.tally.buckets()}
        entries: list[tuple[str, list[int], int]] = []
        for key in self._session_order:
            bucket = by_key.get(key)
            slots = self._assigned_slots(key)
            if bucket is None or not slots:
                continue
            entries.append((key, slots, fixed_by_assignment(bucket)))
        text = assigned_session_line(entries)
        self._set_text(self.assigned_label, text)
        self._set_shown(self.assigned_label, bool(text))

    def _mode_button(self, text: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName("catchAllMode")
        button.setCheckable(True)
        return button

    def _on_mode_toggled(self, mode_id: int, checked: bool) -> None:
        """One of the pair lit up. The one turning off is ignored."""
        if not checked:
            return
        show_all = mode_id == _MODE_ALL
        if show_all == self._show_all:
            return
        self._show_all = show_all
        self.refresh()

    def _wrote(self) -> None:
        self._widget_writes += 1

    def _set_text(self, widget: QLabel | QPushButton, text: str) -> None:
        if widget.text() != text:
            self._wrote()
            widget.setText(text)

    def _set_shown(self, widget: QWidget, shown: bool) -> None:
        # ``isHidden`` is the explicit flag. ``isVisible`` is false whenever
        # the dock or the window is hidden, which would make every refresh
        # call ``setVisible`` again.
        if widget.isHidden() == shown:
            self._wrote()
            widget.setVisible(shown)

    def _set_enabled(self, widget: QWidget, enabled: bool) -> None:
        if widget.isEnabled() != enabled:
            self._wrote()
            widget.setEnabled(enabled)

    def _set_tip(self, widget: QWidget, tip: str) -> None:
        if widget.toolTip() != tip:
            self._wrote()
            widget.setToolTip(tip)

    def _routing(self) -> _Routing:
        cached = self._routing_cache
        if cached is None:
            cached = _load_routing(self._win.config)
            self._routing_cache = cached
        return cached

    def _invalidate_routing(self) -> None:
        self._routing_cache = None

    def _first_assignable_key(self) -> str | None:
        """The first ranked headstamp either bar control can still act on.

        Sharing a bin is still possible when every slot is taken, so a missing
        empty slot only ends the walk when nothing is occupied either.
        """
        routing = self._routing()
        if routing.empty is None and not routing.occupied:
            return None
        for bucket in self._ranked:
            if self._has_slot(bucket.key) or not self._known(bucket.key):
                continue
            return bucket.key
        return None

    def _paint_table(self, selected: str | None, other: tuple[int, int]) -> None:
        caught = self.tally.catch_all_total
        warning = self._color("warning", _FALLBACK_WARNING).name()
        muted = self._color("text_muted", _FALLBACK_MUTED).name()
        specs: list[_RowPaint] = []
        for rank, bucket in enumerate(self._ranked, start=1):
            below_only = set(bucket.reasons) == {BELOW_FLOOR}
            specs.append(
                _RowPaint(
                    texts=(
                        str(rank),
                        bucket.key,
                        str(bucket.count),
                        f"{percent(bucket.count, caught)}%",
                        reason_summary(bucket),
                    ),
                    key=bucket.key,
                    tip=reason_tooltip(bucket),
                    selectable=True,
                    reason_color=warning if below_only else None,
                    muted_color=None,
                )
            )
        other_keys, other_cases = other
        if other_keys:
            phrase = f"Other: {other_keys} headstamps, {other_cases} cases"
            specs.append(
                _RowPaint(
                    texts=("", phrase, str(other_cases), f"{percent(other_cases, caught)}%", ""),
                    key=None,
                    tip=phrase,
                    selectable=False,
                    reason_color=None,
                    muted_color=muted,
                )
            )

        vbar = self.table.verticalScrollBar()
        hbar = self.table.horizontalScrollBar()
        v_value, h_value = vbar.value(), hbar.value()
        same_key = selected == self._selected_key

        self.table.blockSignals(True)
        if self.table.rowCount() != len(specs):
            self._wrote()
            self.table.setRowCount(len(specs))
        for row, spec in enumerate(specs):
            self._apply_row(row, spec)
        self._move_selection(selected)
        self.table.blockSignals(False)

        # A new case must not yank the list, even when the selected headstamp
        # moves to another row. Moving the highlight onto a different
        # headstamp (the click that assigns one) may.
        if same_key:
            if vbar.value() != v_value:
                self._wrote()
                vbar.setValue(v_value)
            if hbar.value() != h_value:
                self._wrote()
                hbar.setValue(h_value)

    def _selectable_item_flags(self) -> Qt.ItemFlag:
        flags = self._selectable_flags
        if flags is None:
            flags = QTableWidgetItem().flags()
            self._selectable_flags = flags
        return flags

    def _apply_row(self, row: int, spec: _RowPaint) -> None:
        flags = self._selectable_item_flags() if spec.selectable else Qt.ItemFlag.NoItemFlags
        for column, text in enumerate(spec.texts):
            item = self.table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self._wrote()
                self.table.setItem(row, column, item)
            if item.text() != text:
                self._wrote()
                item.setText(text)
            if item.toolTip() != spec.tip:
                self._wrote()
                item.setToolTip(spec.tip)
            if item.data(Qt.ItemDataRole.UserRole) != spec.key:
                self._wrote()
                item.setData(Qt.ItemDataRole.UserRole, spec.key)
            if item.flags() != flags:
                self._wrote()
                item.setFlags(flags)
            self._apply_foreground(item, column, spec)

    def _apply_foreground(self, item: QTableWidgetItem, column: int, spec: _RowPaint) -> None:
        if spec.muted_color is not None:
            wanted = spec.muted_color
        elif column == COL_REASON and spec.reason_color is not None:
            wanted = spec.reason_color
        else:
            wanted = None
        current = item.foreground()
        if wanted is None:
            if current.style() != Qt.BrushStyle.NoBrush:
                self._wrote()
                item.setForeground(QBrush())
            return
        if current.style() != Qt.BrushStyle.NoBrush and current.color().name() == wanted:
            return
        self._wrote()
        item.setForeground(QBrush(QColor(wanted)))

    def _row_for_key(self, key: str) -> int | None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_NAME)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return row
        return None

    def _move_selection(self, key: str | None) -> None:
        """Highlight ``key`` without ``setCurrentCell`` when it is already current."""
        if not key:
            self._selected_key = None
            if self.table.currentRow() >= 0:
                self._wrote()
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
            return
        row = self._row_for_key(key)
        if row is None:
            self._selected_key = None
            if self.table.currentRow() >= 0:
                self._wrote()
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
            return
        self._selected_key = key
        if self.table.currentRow() != row:
            self._wrote()
            self.table.setCurrentCell(row, COL_NAME)

    def _on_selection_changed(self) -> None:
        item = self.table.item(self.table.currentRow(), COL_NAME)
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            self._selected_key = None
        else:
            stored = item.data(Qt.ItemDataRole.UserRole)
            self._selected_key = stored if isinstance(stored, str) else None
        self._update_button()

    def _color(self, role: str, fallback: str) -> QColor:
        colors = getattr(self._win, "palette_colors", None) or {}
        return QColor(str(colors.get(role) or fallback))

    # ----- assign --------------------------------------------------------------

    def _assigned_slots(self, key: str) -> list[int]:
        return list(self._routing().slots.get(key, ()))

    def _has_slot(self, key: str) -> bool:
        return bool(self._assigned_slots(key))

    def _known(self, key: str) -> bool:
        if not key or key == EMPTY_KEY:
            return False
        return key in self._routing().known

    def _bucket(self, key: str) -> CatchAllBucket | None:
        """The on-screen row for ``key``, including a row past the tenth in ALL."""
        for bucket in self._ranked:
            if bucket.key == key:
                return bucket
        return None

    def _routed_tip(self, key: str, bucket: CatchAllBucket, assigned: list[int]) -> str:
        where = ", ".join(f"#{slot}" for slot in assigned)
        noun = "slot" if len(assigned) == 1 else "slots"
        if set(bucket.reasons) == {BELOW_FLOOR}:
            return (
                f"{key} is already routed to {noun} {where}. These cases were below the "
                "confidence floor, so they stayed in the catch-all."
            )
        return f"{key} is already routed to {noun} {where}."

    def _apply_assign_button(self, enabled: bool, text: str, tip: str) -> None:
        self._set_enabled(self.assign_button, enabled)
        self._set_text(self.assign_button, text)
        self._set_tip(self.assign_button, tip)

    def _apply_add_button(self, enabled: bool, tip: str) -> None:
        self._set_enabled(self.add_button, enabled)
        self._set_tip(self.add_button, tip)
        self._sync_add_menu()

    def _desired_menu(self) -> tuple[tuple[int, str], ...]:
        if not self.add_button.isEnabled():
            return ()
        return tuple((slot, escape_mnemonic(f"#{slot} {', '.join(names)}")) for slot, names in self._routing().occupied)

    def _sync_add_menu(self) -> None:
        """Fill the menu when its entries changed. Never while the popup is up.

        ``clear()`` on a visible menu destroys the actions the user is looking
        at and the popup closes. ``aboutToShow`` calls this before the popup
        exists, so an open is still a fresh list when the bins have changed.
        """
        menu = self.add_button.menu()
        if menu is None or menu.isVisible():
            return
        desired = self._desired_menu()
        current: list[tuple[int, str]] = []
        for action in menu.actions():
            slot = action.data()
            if not isinstance(slot, int):
                current = []
                break
            current.append((slot, action.text()))
        if tuple(current) == desired:
            return
        self._wrote()
        menu.clear()
        for slot, text in desired:
            action = menu.addAction(text)
            action.setData(slot)
            action.triggered.connect(lambda _checked=False, chosen=slot: self._add_selected_to_slot(chosen))

    def _update_button(self) -> None:
        button_text = "Assign to empty slot"
        key = self._selected_key
        bucket = self._bucket(key) if key else None
        if key is None or bucket is None:
            self._apply_assign_button(False, button_text, _SELECT_TIP)
            self._apply_add_button(False, _SELECT_TIP)
            return
        if not self._known(key):
            self._apply_assign_button(False, escape_mnemonic(f"Assign {key} to empty slot"), _UNKNOWN_TIP)
            self._apply_add_button(False, _UNKNOWN_TIP)
            return
        assigned = self._assigned_slots(key)
        if assigned:
            tip = self._routed_tip(key, bucket, assigned)
            self._apply_assign_button(False, f"→ #{assigned[0]}", tip)
            self._apply_add_button(False, tip)
            return
        empty = self._routing().empty
        if empty is None:
            self._apply_assign_button(False, escape_mnemonic(f"Assign {key} to empty slot"), "No empty slot left.")
        else:
            self._apply_assign_button(
                True,
                escape_mnemonic(f"Assign {key} to empty slot #{empty}"),
                f"Put an empty bin in slot {empty}. Cases already in the wheel still drop in the catch-all.",
            )
        if not self._routing().occupied:
            self._apply_add_button(False, _NO_OCCUPIED_TIP)
            return
        self._apply_add_button(True, _ADD_TIP)

    def _finish_assign(self, key: str) -> None:
        # The bus event that drops the cache has not been drained yet, and
        # the next line has to see the slot this click just wrote.
        self._invalidate_routing()
        # A failed assign leaves the row where it was. A successful one moves
        # the selection to the next headstamp that can still take a bin.
        # Paint even if the menu has not hidden yet: the prefer flag would be
        # gone by the time ``aboutToHide`` ran. The menu itself is not cleared
        # while it is showing.
        self._prefer_next_assignable = self._has_slot(key)
        self.refresh()
        self._prefer_next_assignable = False

    def _assign_selected(self) -> None:
        key = self._selected_key
        if not key or not self.assign_button.isEnabled():
            return
        assign = getattr(self._win, "assign_from_catch_all", None)
        if assign is None:
            return
        assign(key)
        self._finish_assign(key)

    def _add_selected_to_slot(self, slot: int) -> None:
        key = self._selected_key
        if not key or not self.add_button.isEnabled():
            return
        assign = getattr(self._win, "assign_from_catch_all", None)
        if assign is None:
            return
        assign(key, slot)
        self._finish_assign(key)


def build_catch_all_view(win: Any) -> CatchAllView:
    return CatchAllView(win)
