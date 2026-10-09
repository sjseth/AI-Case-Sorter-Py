"""The Catch-All dock: live tally, assign bar, and the slot-0 card that opens it."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence

from sorter.data.repository import HeadstampParentRepo, HeadstampRepo
from sorter.ui.catch_all_view import _ADD_TEXT, COL_NAME, COL_REASON, COLUMNS

from .conftest import seed_model


def _post(window, **fields: object) -> None:
    result = {"ok": True, "slot": 0, "label": "BPS", "parent": None, "reason": "unassigned"}
    result.update(fields)
    window.bus.post("run/result", result)
    window.bus.drain()


def _row(view, key: str) -> int:
    for row in range(view.table.rowCount()):
        item = view.table.item(row, COL_NAME)
        if item is not None and item.text() == key:
            return row
    raise AssertionError(f"{key} is not in the table")


def _select(view, key: str) -> None:
    view.table.setCurrentCell(_row(view, key), COL_NAME)


def _menu_texts(view) -> list[str]:
    menu = view.add_button.menu()
    assert menu is not None
    return [action.text() for action in menu.actions()]


def _names(view) -> list[str]:
    names = []
    for row in range(view.table.rowCount()):
        item = view.table.item(row, COL_NAME)
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsSelectable:
            names.append(item.text())
    return names


def test_a_fresh_panel_is_empty_and_closed(window) -> None:
    view = window.catch_all_view
    assert window.catch_all_dock.isClosed()
    assert window.catch_all_dock.windowTitle() == "Catch-All"
    assert view.summary_label.text() == "0 in catch-all of 0 sorted (0%)"
    assert [view.table.horizontalHeaderItem(i).text() for i in range(len(COLUMNS))] == list(COLUMNS)
    assert not view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign to empty slot"
    assert view.assign_button.toolTip() == "Select a headstamp."
    assert view.assign_button.objectName() == "action"
    assert not view.add_button.isEnabled()
    assert view.add_button.text() == _ADD_TEXT
    assert view.add_button.objectName() == "catchAllAdd"
    assert view.add_button.toolTip() == "Select a headstamp."
    assert _menu_texts(view) == []
    assert view.assigned_label.isHidden()
    assert view.top_ten_button.text() == "Top 10"
    assert view.all_button.text() == "ALL"
    assert view.top_ten_button.objectName() == "catchAllMode"
    assert view.all_button.objectName() == "catchAllMode"
    assert view.top_ten_button.isChecked()
    assert not view.all_button.isChecked()
    assert view._mode_group.exclusive()
    assert not view._show_all
    assert view.table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    row = view.layout().itemAt(0).layout()
    assert row.itemAt(0).spacerItem() is not None
    assert row.itemAt(1).widget() is view.top_ten_button
    assert row.itemAt(2).widget() is view.all_button


def test_the_header_matches_the_slot_card_and_ignores_failures(window) -> None:
    _post(window, label="BPS")
    _post(window, label="BPS")
    _post(window, slot=2, label="WIN", reason="routed")
    window.bus.post("run/result", {"ok": False, "slot": 0, "label": "X"})
    window.bus.drain()

    view = window.catch_all_view
    assert window.slot_grid.cards[0].count_label.text() == "2"
    assert window.master_count_label.text() == "3"
    assert view.tally.catch_all_total == 2
    assert view.tally.total == 3
    assert view.summary_label.text() == "2 in catch-all of 3 sorted (67%)"
    assert view.table.item(_row(view, "BPS"), 2).text() == "2"
    assert view.table.item(_row(view, "BPS"), 3).text() == "100%"


def test_top_ten_and_a_greyed_unselectable_other_row(window) -> None:
    for _ in range(6):
        _post(window, label="L00")
    for index in range(1, 11):
        _post(window, label=f"L{index:02d}")

    view = window.catch_all_view
    assert view.table.rowCount() == 11
    assert view.table.item(0, COL_NAME).text() == "L00"
    assert view.table.item(9, COL_NAME).text() == "L09"
    other = view.table.item(10, COL_NAME)
    assert other.text() == "Other: 1 headstamps, 1 cases"
    assert other.flags() == Qt.ItemFlag.NoItemFlags
    assert other.foreground().color().name() == QColor(window.palette_colors["text_muted"]).name()
    for row in range(view.table.rowCount()):
        for column in range(view.table.columnCount()):
            assert view.table.cellWidget(row, column) is None
    view.table.setCurrentCell(10, COL_NAME)
    assert view._selected_key is None
    assert not view.assign_button.isEnabled()


def test_a_parent_rows_tooltip_lists_child_labels(window) -> None:
    _post(window, label="WIN", parent="Brass", reason="unassigned")
    _post(window, label="FC", parent="Brass", reason="below_floor")

    view = window.catch_all_view
    item = view.table.item(_row(view, "Brass"), COL_REASON)
    assert "Labels:" in item.toolTip()
    assert "WIN: 1" in item.toolTip()
    assert "FC: 1" in item.toolTip()
    # Mixed reasons are not painted as a below-floor warning.
    assert item.foreground().style() == Qt.BrushStyle.NoBrush


def test_only_a_below_floor_reason_uses_the_warning_color_and_follows_the_theme(window) -> None:
    _post(window, label="BPS", reason="below_floor")
    _post(window, label="IK", reason="unassigned")
    view = window.catch_all_view

    def reason_brush(key: str):
        return view.table.item(_row(view, key), COL_REASON).foreground()

    assert reason_brush("BPS").color().name() == QColor(window.palette_colors["warning"]).name()
    assert reason_brush("IK").style() == Qt.BrushStyle.NoBrush

    window.set_theme("Light")
    assert reason_brush("BPS").color().name() == "#b45309"
    assert reason_brush("BPS").color().name() == QColor(window.palette_colors["warning"]).name()
    assert reason_brush("IK").style() == Qt.BrushStyle.NoBrush

    window.set_theme("Dark")
    assert reason_brush("BPS").color().name() == "#f59e0b"


def test_counts_survive_stop_and_start_and_reset_clears_them(window) -> None:
    _post(window, label="BPS")
    window.bus.post("run/stopped", None)
    window.bus.post("run/started", None)
    window.bus.drain()
    assert window.catch_all_view.tally.catch_all_total == 1
    assert window.slot_grid.cards[0].count_label.text() == "1"

    window.reset_counts()
    assert window.catch_all_view.tally.total == 0
    assert window.catch_all_view.tally.catch_all_total == 0
    assert window.catch_all_view.summary_label.text() == "0 in catch-all of 0 sorted (0%)"
    assert window.slot_grid.cards[0].count_label.text() == "0"


def test_selection_follows_the_headstamp_when_the_order_changes(window) -> None:
    _post(window, label="BPS")
    _post(window, label="IK")
    view = window.catch_all_view
    _select(view, "BPS")
    assert view._selected_key == "BPS"

    _post(window, label="IK")
    _post(window, label="IK")
    assert view.table.item(0, COL_NAME).text() == "IK"
    assert view.table.item(view.table.currentRow(), COL_NAME).text() == "BPS"
    assert view._selected_key == "BPS"


def test_an_ampersand_in_the_name_is_escaped_on_the_button_only(window, config) -> None:
    seed_model(config, {"S&B": 0})
    _post(window, label="S&B")
    view = window.catch_all_view
    assert view.table.item(_row(view, "S&B"), COL_NAME).text() == "S&B"
    _select(view, "S&B")
    assert view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign S&&B to empty slot #1"
    assert QKeySequence.mnemonic(view.assign_button.text()).isEmpty()


def test_assign_button_states(window, config) -> None:
    seed_model(config, {"WIN": 4, "BPS": 0, "UPSIDE DOWN": 0})
    _post(window, label="GHOST", reason="unknown")
    _post(window, label="WIN", reason="below_floor")
    _post(window, label="BPS", reason="below_floor")
    _post(window, label="UPSIDE DOWN", reason="special")
    view = window.catch_all_view

    _select(view, "GHOST")
    assert not view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign GHOST to empty slot"
    assert "isn't in the model" in view.assign_button.toolTip()
    assert not view.add_button.isEnabled()
    assert "isn't in the model" in view.add_button.toolTip()
    assert _menu_texts(view) == []

    _select(view, "WIN")
    assert not view.assign_button.isEnabled()
    assert view.assign_button.text() == "→ #4"
    assert "confidence floor" in view.assign_button.toolTip()
    assert "slot #4" in view.assign_button.toolTip()
    assert not view.add_button.isEnabled()
    assert view.add_button.toolTip() == view.assign_button.toolTip()

    # Below the floor, but not yet routed: still assignable, and WIN occupies #4.
    _select(view, "BPS")
    assert view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign BPS to empty slot #1"
    assert view.add_button.isEnabled()
    assert _menu_texts(view) == ["#4 WIN"]

    _select(view, "UPSIDE DOWN")
    assert view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign UPSIDE DOWN to empty slot #1"
    assert view.add_button.isEnabled()
    assert _menu_texts(view) == ["#4 WIN"]

    _post(window, label="")
    _select(view, "(empty)")
    assert not view.assign_button.isEnabled()
    assert "isn't in the model" in view.assign_button.toolTip()
    assert not view.add_button.isEnabled()
    assert "isn't in the model" in view.add_button.toolTip()


def test_assign_is_disabled_when_every_slot_is_taken(window, config) -> None:
    assignments = {f"H{slot}": slot for slot in range(1, 8)}
    assignments["BPS"] = 0
    seed_model(config, assignments)
    _post(window, label="BPS")
    view = window.catch_all_view
    _select(view, "BPS")
    assert not view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign BPS to empty slot"
    assert view.assign_button.toolTip() == "No empty slot left."
    assert view.add_button.isEnabled()
    assert _menu_texts(view) == [f"#{slot} H{slot}" for slot in range(1, 8)]


def test_an_unknown_label_disables_sharing_even_when_nothing_is_occupied(window) -> None:
    _post(window, label="GHOST", reason="unknown")
    view = window.catch_all_view
    _select(view, "GHOST")
    assert not view.add_button.isEnabled()
    assert "isn't in the model" in view.add_button.toolTip()
    assert view.add_button.toolTip() != "No slot has a headstamp yet."


def test_sharing_is_disabled_when_no_slot_is_occupied(window, config) -> None:
    seed_model(config, {"BPS": 0})
    _post(window, label="BPS")
    view = window.catch_all_view
    _select(view, "BPS")
    assert view.assign_button.isEnabled()
    assert not view.add_button.isEnabled()
    assert view.add_button.toolTip() == "No slot has a headstamp yet."
    assert _menu_texts(view) == []


def test_adding_to_an_existing_slot_shares_it_and_selects_the_next_row(window, config, caplog) -> None:
    seed_model(config, {"WMA": 4, "WMA NATO": 4, "SIG": 0, "IK": 0})
    for _ in range(12):
        _post(window, label="SIG")
    _post(window, label="IK")
    view = window.catch_all_view
    _select(view, "SIG")
    assert view.assign_button.isEnabled()
    assert view.assign_button.objectName() == "action"
    assert _menu_texts(view) == ["#4 WMA, WMA NATO"]
    events: list[dict] = []
    window.bus.subscribe("run/assignment_changed", events.append)

    with caplog.at_level(logging.INFO, logger="sorter.ui.app"):
        view.add_button.menu().actions()[0].trigger()
    window.bus.drain()

    assert config.slot_for_headstamp("SIG") == 4
    assert config.slot_for_headstamp("WMA") == 4
    assert config.slot_for_headstamp("WMA NATO") == 4
    assert "slot assignment: 'SIG' -> slot 4 (source=catch_all, running=False)" in caplog.text
    assert window.statusBar().currentMessage() == (
        "SIG → Slot 4, sharing that bin. Cases already in the wheel still drop in the catch-all."
    )
    assert events and events[-1] == {"label": "SIG", "slot": 4, "source": "catch_all"}
    assert _names(view) == ["IK"]
    assert view.summary_label.text() == "13 in catch-all of 13 sorted (100%)"
    assert view.assigned_label.text() == "Assigned this session: SIG → #4 (12 already in bin 0)"
    assert view._selected_key == "IK"
    assert view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign IK to empty slot #1"
    assert window.slot_grid.cards[4].names_label.text() == "SIG, WMA, WMA NATO"


def test_an_ampersand_in_a_slot_name_is_escaped_on_the_menu(window, config) -> None:
    seed_model(config, {"S&B": 4, "SIG": 0})
    _post(window, label="SIG")
    view = window.catch_all_view
    _select(view, "SIG")
    assert _menu_texts(view) == ["#4 S&&B"]
    assert QKeySequence.mnemonic(_menu_texts(view)[0]).isEmpty()


def test_adding_when_every_slot_is_taken_selects_the_next_unassigned_row(window, config) -> None:
    assignments = {f"H{slot}": slot for slot in range(1, 8)}
    assignments["BPS"] = 0
    assignments["IK"] = 0
    seed_model(config, assignments)
    _post(window, label="BPS")
    _post(window, label="IK")
    view = window.catch_all_view
    _select(view, "BPS")
    assert not view.assign_button.isEnabled()

    view.add_button.menu().actions()[0].trigger()

    assert config.slot_for_headstamp("BPS") == 1
    assert config.slot_for_headstamp("H1") == 1
    assert _names(view) == ["IK"]
    assert view._selected_key == "IK"
    assert view.add_button.isEnabled()
    assert not view.assign_button.isEnabled()


def test_parent_mode_menu_lists_orphans_not_children_and_writes_the_parent(window, config) -> None:
    mid = seed_model(config, {"WIN": 3, "FC": 5, "SIG": 0})
    brass = HeadstampParentRepo(config.db).add(mid, "Brass")
    win = next(h for h in HeadstampRepo(config.db).list_for_model(mid) if h.name == "WIN")
    HeadstampRepo(config.db).set_parent(win.id, brass.id)
    config.set_use_parent_classifications(True)
    _post(window, label="WIN", parent="Brass", reason="unassigned")
    _post(window, label="SIG", reason="unassigned")
    view = window.catch_all_view
    _select(view, "Brass")
    # WIN's own slot column is not a bin while parent mode routes through Brass.
    assert _menu_texts(view) == ["#5 FC"]

    view.add_button.menu().actions()[0].trigger()

    assert config.slot_for_headstamp("WIN") == 5
    assert config.slot_for_headstamp("FC") == 5
    parent = HeadstampParentRepo(config.db).get(brass.id)
    assert parent is not None and parent.slot == 5
    child = next(h for h in HeadstampRepo(config.db).list_for_model(mid) if h.name == "WIN")
    assert child.slot == 3
    assert _names(view) == ["SIG"]
    assert view._selected_key == "SIG"


def test_package_mode_menu_adds_the_headstamp_beside_the_existing_name(window, config) -> None:
    seed_model(config, {"CBC": 0, "FC": 0, "SIG": 0})
    config.set_run_package_mode(True)
    config.set_package_slot_headstamp(1, "CBC", True)
    _post(window, label="FC")
    _post(window, label="SIG")
    view = window.catch_all_view
    _select(view, "FC")
    assert _menu_texts(view) == ["#1 CBC"]

    view.add_button.menu().actions()[0].trigger()

    assert config.slots_for_headstamp_package("FC") == [1]
    assert config.slots_for_headstamp_package("CBC") == [1]
    assert _names(view) == ["SIG"]
    assert view._selected_key == "SIG"


def test_an_external_assignment_drops_the_row_and_keeps_the_bin_total(window, config) -> None:
    seed_model(config, {"BPS": 0, "IK": 0})
    for _ in range(4):
        _post(window, label="BPS")
    _post(window, label="IK")
    view = window.catch_all_view
    _select(view, "BPS")

    config.set_headstamp_slot("BPS", 6)
    window.bus.post("run/assignment_changed", {"label": "BPS", "slot": 6, "source": "editor"})
    window.bus.drain()

    assert view.summary_label.text() == "5 in catch-all of 5 sorted (100%)"
    assert window.slot_grid.cards[0].count_label.text() == "5"
    assert _names(view) == ["IK"]
    assert view._selected_key is None
    assert view.assign_button.text() == "Assign to empty slot"
    assert view.assigned_label.text() == "Assigned this session: BPS → #6 (4 already in bin 0)"
    assert not view.assigned_label.isHidden()

    config.set_headstamp_slot("BPS", 0)
    window.bus.post("run/assignment_changed", {"label": "BPS", "slot": 0, "source": "editor"})
    window.bus.drain()

    assert _names(view) == ["BPS", "IK"]
    assert view.table.item(_row(view, "BPS"), 2).text() == "4"
    assert view.assigned_label.isHidden()


def test_assigning_mid_run_fills_an_empty_slot_and_says_so(window, config, caplog) -> None:
    seed_model(config, {"BPS": 0, "WIN": 3})
    _post(window, label="BPS", reason="unassigned")
    view = window.catch_all_view
    _select(view, "BPS")
    window._is_running = True
    events: list[dict] = []
    window.bus.subscribe("run/assignment_changed", events.append)

    with caplog.at_level(logging.INFO, logger="sorter.ui.app"):
        view.assign_button.click()
    window.bus.drain()

    assert config.slot_for_headstamp("BPS") == 1
    assert "slot assignment: 'BPS' -> slot 1 (source=catch_all, running=True)" in caplog.text
    assert window.statusBar().currentMessage() == (
        "BPS → Slot 1. Put an empty bin there; cases already in the wheel still drop in the catch-all."
    )
    assert events and events[-1] == {"label": "BPS", "slot": 1, "source": "catch_all"}
    assert _names(view) == []
    assert view.summary_label.text() == "1 in catch-all of 1 sorted (100%)"
    assert window.slot_grid.cards[0].count_label.text() == "1"
    assert view.assigned_label.text() == "Assigned this session: BPS → #1 (1 already in bin 0)"
    assert view._selected_key is None
    assert view.assign_button.text() == "Assign to empty slot"
    assert "BPS" in window.slot_grid.cards[1].names_label.text()


def test_assigning_a_parent_row_writes_the_parent_slot(window, config) -> None:
    mid = seed_model(config, {"WIN": 3, "FC": 0})
    brass = HeadstampParentRepo(config.db).add(mid, "Brass")
    win = next(h for h in HeadstampRepo(config.db).list_for_model(mid) if h.name == "WIN")
    HeadstampRepo(config.db).set_parent(win.id, brass.id)
    config.set_use_parent_classifications(True)
    _post(window, label="WIN", parent="Brass", reason="unassigned")
    view = window.catch_all_view
    _select(view, "Brass")
    assert view.assign_button.text() == "Assign Brass to empty slot #1"

    view.assign_button.click()

    assert config.slot_for_headstamp("WIN") == 1
    parent = HeadstampParentRepo(config.db).get(brass.id)
    assert parent is not None and parent.slot == 1
    child = next(h for h in HeadstampRepo(config.db).list_for_model(mid) if h.name == "WIN")
    assert child.slot == 3
    assert _names(view) == []
    assert view.assigned_label.text() == "Assigned this session: Brass → #1 (1 already in bin 0)"
    assert view.assign_button.text() == "Assign to empty slot"


def test_a_mixed_row_keeps_only_what_a_slot_does_not_fix(window, config) -> None:
    seed_model(config, {"BPS": 0, "IK": 0})
    for _ in range(3):
        _post(window, label="BPS", reason="unassigned")
    for _ in range(2):
        _post(window, label="BPS", reason="below_floor")
    _post(window, label="IK", reason="unassigned")
    view = window.catch_all_view
    _select(view, "BPS")

    view.assign_button.click()

    assert view.summary_label.text() == "6 in catch-all of 6 sorted (100%)"
    assert window.slot_grid.cards[0].count_label.text() == "6"
    assert _names(view) == ["BPS", "IK"]
    assert view.table.item(_row(view, "BPS"), 2).text() == "2"
    assert view.table.item(_row(view, "BPS"), COL_REASON).text() == "Below floor"
    assert view.assigned_label.text() == "Assigned this session: BPS → #1 (3 already in bin 0)"
    # The leftover row cannot be assigned again, so the click moves on.
    assert view._selected_key == "IK"
    assert view.assign_button.text() == "Assign IK to empty slot #2"

    config.set_headstamp_slot("BPS", 0)
    window.bus.post("run/assignment_changed", {"label": "BPS", "slot": 0, "source": "editor"})
    window.bus.drain()

    assert view.table.item(_row(view, "BPS"), 2).text() == "5"
    assert view.assigned_label.isHidden()


def test_assigning_from_the_panel_selects_the_new_top_row(window, config) -> None:
    seed_model(config, {"BPS": 0, "IK": 0, "SIG": 0})
    for _ in range(3):
        _post(window, label="BPS")
    for _ in range(2):
        _post(window, label="IK")
    _post(window, label="SIG")
    view = window.catch_all_view
    _select(view, "BPS")

    view.assign_button.click()

    assert _names(view) == ["IK", "SIG"]
    assert view._selected_key == "IK"
    assert view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign IK to empty slot #2"

    view.assign_button.click()

    assert _names(view) == ["SIG"]
    assert view._selected_key == "SIG"
    assert view.assigned_label.text() == ("Assigned this session: BPS → #1 (3 already in bin 0), IK → #2 (2)")


def test_results_reuse_the_slot_map_until_an_assignment(window, config) -> None:
    seed_model(config, {"BPS": 0, "IK": 0})
    view = window.catch_all_view
    with patch.object(config.headstamps_repo, "list_for_model", wraps=config.headstamps_repo.list_for_model) as listed:
        _post(window, label="BPS")
        warmed = listed.call_count
        assert warmed > 0
        _post(window, label="IK")
        _post(window, label="BPS")
        assert listed.call_count == warmed

        config.set_headstamp_slot("BPS", 6)
        window.bus.post("run/assignment_changed", {"label": "BPS", "slot": 6, "source": "editor"})
        window.bus.drain()
    assert "BPS" not in _names(view)
    assert "IK" in _names(view)


def test_an_open_slot_menu_survives_results_and_a_refresh(window, config) -> None:
    seed_model(config, {"WMA": 4, "WMA NATO": 4, "SIG": 0})
    _post(window, label="SIG")
    view = window.catch_all_view
    _select(view, "SIG")
    menu = view.add_button.menu()
    assert menu is not None
    menu.show()
    assert menu.isVisible()
    actions = menu.actions()
    assert [action.text() for action in actions] == ["#4 WMA, WMA NATO"]
    summary = view.summary_label.text()
    items = [view.table.item(0, column) for column in range(view.table.columnCount())]

    _post(window, label="SIG")
    _post(window, label="SIG")

    assert menu.isVisible()
    assert menu.actions() == actions
    assert view.summary_label.text() == summary
    assert view._refresh_pending
    assert [view.table.item(0, column) for column in range(view.table.columnCount())] == items

    # An immediate repaint (theme, reset) may rewrite cells. It must not
    # tear the popup down or replace the actions the user is pointing at.
    view.refresh()
    assert menu.isVisible()
    assert menu.actions() == actions

    menu.close()
    assert not menu.isVisible()
    assert view.summary_label.text() == "3 in catch-all of 3 sorted (100%)"
    assert view.table.item(0, 2).text() == "3"


def test_a_repeat_refresh_does_not_touch_unchanged_widgets(window, config) -> None:
    seed_model(config, {"WMA": 4, "SIG": 0, "IK": 0})
    _post(window, label="SIG")
    _post(window, label="IK")
    view = window.catch_all_view
    _select(view, "SIG")
    menu = view.add_button.menu()
    assert menu is not None
    actions = menu.actions()
    items = [
        view.table.item(row, column)
        for row in range(view.table.rowCount())
        for column in range(view.table.columnCount())
    ]
    scroll = view.table.verticalScrollBar().value()
    writes = view._widget_writes

    view.refresh()

    assert view._widget_writes == writes
    assert menu.actions() == actions
    assert menu.isVisible() is False
    assert [
        view.table.item(row, column)
        for row in range(view.table.rowCount())
        for column in range(view.table.columnCount())
    ] == items
    assert view.table.verticalScrollBar().value() == scroll
    assert view.summary_label.text() == "2 in catch-all of 2 sorted (100%)"


def test_results_on_an_open_panel_coalesce_into_one_paint(qapp, window) -> None:
    _post(window, label="BPS")
    view = window.catch_all_view
    window.resize(1200, 800)
    window.show()
    window.reveal_dock(window.catch_all_dock)
    qapp.processEvents()
    assert view._panel_open()

    window.bus.post("run/result", {"ok": True, "slot": 0, "label": "BPS", "parent": None, "reason": "unassigned"})
    window.bus.post("run/result", {"ok": True, "slot": 0, "label": "BPS", "parent": None, "reason": "unassigned"})
    window.bus.drain()

    assert view.summary_label.text() == "1 in catch-all of 1 sorted (100%)"
    assert view._refresh_timer.isActive()
    view._refresh_timer.timeout.emit()
    view._refresh_timer.stop()
    assert view.summary_label.text() == "3 in catch-all of 3 sorted (100%)"
    assert view.table.item(0, 2).text() == "3"


def test_a_new_case_keeps_the_table_scroll_position(window, config) -> None:
    seed_model(config, {"BPS": 0})
    _post(window, label="BPS")
    view = window.catch_all_view
    bar = view.table.verticalScrollBar()
    bar.setRange(0, 40)
    bar.setValue(17)

    _post(window, label="BPS")

    assert bar.value() == 17


def _near(got: QColor, want: QColor, tol: int = 8) -> bool:
    return max(abs(got.red() - want.red()), abs(got.green() - want.green()), abs(got.blue() - want.blue())) <= tol


def _fill(button) -> QColor:
    """A pixel in the left padding, clear of the border and the label."""
    image = button.grab().toImage()
    assert image.width() > 16 and image.height() > 4
    return image.pixelColor(8, image.height() // 2)


def _seed_past_ten(config) -> None:
    assignments = {f"H{index:02d}": 0 for index in range(13)}
    assignments["WMA"] = 4
    seed_model(config, assignments)


def test_all_lists_every_row_and_does_not_resize_the_dock(qapp, window, config) -> None:
    _seed_past_ten(config)
    for index in range(13):
        _post(window, label=f"H{index:02d}")
    view = window.catch_all_view
    assert _names(view) == [f"H{index:02d}" for index in range(10)]
    assert view.table.item(10, COL_NAME).text() == "Other: 3 headstamps, 3 cases"

    window.resize(1200, 800)
    window.show()
    window.reveal_dock(window.catch_all_dock)
    qapp.processEvents()
    size = window.catch_all_dock.size()
    assert size.height() > 0
    margin = view.layout().contentsMargins().right()
    assert abs(view.all_button.geometry().right() - (view.width() - margin)) <= 2
    assert view.top_ten_button.x() > margin
    assert view.top_ten_button.geometry().right() <= view.all_button.x()
    accent = QColor(window.palette_colors["accent"])
    assert _near(_fill(view.top_ten_button), accent)
    assert not _near(_fill(view.all_button), accent)

    view.all_button.click()
    qapp.processEvents()

    assert window.catch_all_dock.size() == size
    assert view.all_button.isChecked()
    assert not view.top_ten_button.isChecked()
    assert _names(view) == [f"H{index:02d}" for index in range(13)]
    assert view.table.rowCount() == 13
    assert _near(_fill(view.all_button), accent)
    assert not _near(_fill(view.top_ten_button), accent)
    view.table.setFixedHeight(48)
    qapp.processEvents()
    assert view.table.verticalScrollBar().maximum() > 0

    view.top_ten_button.click()
    assert view.top_ten_button.isChecked()
    assert not view._show_all
    assert view.table.rowCount() == 11
    assert view.table.item(10, COL_NAME).text() == "Other: 3 headstamps, 3 cases"


def test_assign_and_add_reach_a_row_past_the_tenth(window, config) -> None:
    _seed_past_ten(config)
    for index in range(13):
        _post(window, label=f"H{index:02d}")
    view = window.catch_all_view
    assert "H12" not in _names(view)

    view.all_button.click()
    _select(view, "H12")
    assert view.assign_button.isEnabled()
    assert view.assign_button.text() == "Assign H12 to empty slot #1"
    view.assign_button.click()
    assert config.slot_for_headstamp("H12") == 1
    assert "H12" not in _names(view)
    assert view.all_button.isChecked()

    _select(view, "H11")
    assert view.add_button.isEnabled()
    menu = view.add_button.menu()
    assert menu is not None
    chosen = next(action for action in menu.actions() if action.data() == 4)
    chosen.trigger()
    assert config.slot_for_headstamp("H11") == 4
    assert "H11" not in _names(view)
    assert "H11" in window.slot_grid.cards[4].names_label.text()

    view.top_ten_button.click()
    assert "H10" not in _names(view)
    assert view.table.item(view.table.rowCount() - 1, COL_NAME).text() == "Other: 1 headstamps, 1 cases"


def test_all_mode_keeps_scroll_and_an_open_menu(window, config) -> None:
    _seed_past_ten(config)
    for index in range(13):
        _post(window, label=f"H{index:02d}")
    view = window.catch_all_view
    view.all_button.click()
    _select(view, "H12")
    bar = view.table.verticalScrollBar()
    bar.setRange(0, 80)
    bar.setValue(23)
    menu = view.add_button.menu()
    assert menu is not None
    menu.show()
    actions = menu.actions()

    _post(window, label="H00")
    _post(window, label="H00")

    assert menu.isVisible()
    assert menu.actions() == actions
    assert view.summary_label.text() == "13 in catch-all of 13 sorted (100%)"
    assert bar.value() == 23
    assert view._selected_key == "H12"

    menu.close()
    assert view.summary_label.text() == "15 in catch-all of 15 sorted (100%)"
    assert view.table.item(_row(view, "H00"), 2).text() == "3"
    assert bar.value() == 23
    assert view._selected_key == "H12"
    writes = view._widget_writes
    items = [
        view.table.item(row, column)
        for row in range(view.table.rowCount())
        for column in range(view.table.columnCount())
    ]
    view.refresh()
    assert view._widget_writes == writes
    assert bar.value() == 23
    assert [
        view.table.item(row, column)
        for row in range(view.table.rowCount())
        for column in range(view.table.columnCount())
    ] == items


def test_the_list_mode_survives_reset_and_a_theme_change(window) -> None:
    _post(window, label="BPS")
    view = window.catch_all_view
    view.all_button.click()
    window.set_theme("Light")
    assert view.all_button.isChecked()
    assert view._show_all
    assert _names(view) == ["BPS"]

    window.reset_counts()

    assert view.all_button.isChecked()
    assert view._show_all
    assert _names(view) == []
    view.top_ten_button.click()
    assert view.top_ten_button.isChecked()
    assert not view._show_all


def test_reset_clears_the_assigned_line(window, config) -> None:
    seed_model(config, {"BPS": 0})
    _post(window, label="BPS")
    view = window.catch_all_view
    _select(view, "BPS")
    view.assign_button.click()
    assert not view.assigned_label.isHidden()

    window.reset_counts()

    assert view.tally.catch_all_total == 0
    assert view.assigned_label.isHidden()
    assert _names(view) == []
