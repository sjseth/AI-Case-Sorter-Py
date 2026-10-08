"""The headstamp-first assignment view (#129): every headstamp, its slot set on the row.

Opened the way the Sort page opens it, so each test also exercises the
entry point and the live repaint of the slot cards. What was stored is read
back through a *fresh* ``Config`` — the dialog may only mutate through the
Config API, which is what keeps the active sorting template in step.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSpinBox, QStyleOptionViewItem, QTableWidgetItem

from sorter.data.config import Config
from sorter.data.repository import HeadstampParentRepo, HeadstampRepo
from sorter.ui.dialog_headstamp_assign import (
    CONTAINS_COLUMN,
    NAME_COLUMN,
    PACKAGE_HINT,
    PARENT_HINT,
    SLOT_COLUMN,
    HeadstampAssignDialog,
)
from sorter.ui.formatting import EMPTY_VALUE
from sorter.ui.slot_grid import EMPTY_HINT

from .conftest import seed_model

NINE_MM = {"WIN 9MM LUGER": 0, "FC 9MM LUGER": 0, "WIN .45 AUTO": 0, "S B 9MM LUGER": 0}


@pytest.fixture
def assigner(window):
    """Open the view through the Sort page's own entry point."""

    def _open() -> HeadstampAssignDialog:
        window.open_headstamp_assign()
        dialog = window.headstamp_assign_dialog
        assert dialog is not None
        return dialog

    return _open


def stored_slots(config) -> dict[str, int]:
    return {e["name"]: int(e["slot"]) for e in Config(config.db).load().headstamps}


def cell(dialog: HeadstampAssignDialog, row: int, column: int) -> QTableWidgetItem:
    item = dialog.table.item(row, column)
    assert item is not None
    return item


def table(dialog: HeadstampAssignDialog) -> dict[str, str]:
    """Name -> the Slot cell's text, in table order."""
    rows = range(dialog.table.rowCount())
    return {cell(dialog, r, NAME_COLUMN).text(): cell(dialog, r, SLOT_COLUMN).text() for r in rows}


def select(dialog: HeadstampAssignDialog, name: str) -> None:
    dialog.table.setCurrentCell([row.name for row in dialog.rows].index(name), NAME_COLUMN)


def type_keys(dialog: HeadstampAssignDialog, *keys: Qt.Key) -> None:
    for key in keys:
        QTest.keyClick(dialog.table, key)


# ----- the entry point -------------------------------------------------------


def test_the_sort_page_button_opens_the_view(window) -> None:
    seed_model(window.config, {"9mm FC": 0})

    QTest.mouseClick(window.assign_by_headstamp_button, Qt.MouseButton.LeftButton)

    dialog = window.headstamp_assign_dialog
    assert isinstance(dialog, HeadstampAssignDialog)
    assert dialog.isVisible()
    assert dialog.focusWidget() is dialog.filter_edit


def test_closing_the_view_lets_it_go(window, assigner) -> None:
    dialog = assigner()

    dialog.reject()

    assert window.headstamp_assign_dialog is None


def test_every_headstamp_is_listed_with_its_slot(config, assigner) -> None:
    seed_model(config, {"WIN 9MM LUGER": 3, "FC 9MM LUGER": 0})

    assert table(assigner()) == {"FC 9MM LUGER": EMPTY_VALUE, "WIN 9MM LUGER": "3"}


# ----- finding a row ---------------------------------------------------------


def test_filter_matches_every_word_in_any_order(config, assigner) -> None:
    seed_model(config, NINE_MM)
    dialog = assigner()

    dialog.filter_edit.setText("win 9")
    assert list(table(dialog)) == ["WIN 9MM LUGER"]

    dialog.filter_edit.setText("  luger   9MM ")
    assert list(table(dialog)) == ["FC 9MM LUGER", "S B 9MM LUGER", "WIN 9MM LUGER"]


def test_enter_in_the_filter_lands_on_the_top_match(config, assigner) -> None:
    seed_model(config, NINE_MM)
    dialog = assigner()
    dialog.filter_edit.setText("luger")

    QTest.keyClick(dialog.filter_edit, Qt.Key.Key_Return)

    assert dialog.focusWidget() is dialog.table
    assert dialog.current_row().name == "FC 9MM LUGER"
    # Close is the dialog's button; Enter must not reach it.
    assert dialog.isVisible()


def test_a_letter_in_the_list_starts_a_new_search(config, assigner) -> None:
    seed_model(config, NINE_MM)
    dialog = assigner()
    dialog.filter_edit.setText("fc")
    dialog.focus_table()

    QTest.keyClicks(dialog.table, "w")

    assert dialog.filter_edit.text() == "w"
    assert dialog.focusWidget() is dialog.filter_edit


# ----- setting a slot from the keyboard --------------------------------------


def test_a_digit_routes_the_current_row_and_repaints_the_cards(window, config, assigner) -> None:
    seed_model(config, {"WIN 9MM LUGER": 1, "FC 9MM LUGER": 0})
    dialog = assigner()
    select(dialog, "WIN 9MM LUGER")

    type_keys(dialog, Qt.Key.Key_3)

    assert stored_slots(config) == {"WIN 9MM LUGER": 3, "FC 9MM LUGER": 0}
    assert table(dialog)["WIN 9MM LUGER"] == "3"
    # Live, while the dialog is still open.
    assert window.slot_grid.cards[3].names_label.text() == "WIN 9MM LUGER"
    assert window.slot_grid.cards[1].names_label.text() == EMPTY_HINT


def test_a_whole_layout_in_one_keyboard_pass(config, assigner) -> None:
    seed_model(config, NINE_MM)
    dialog = assigner()

    for search, key in (("win 9", Qt.Key.Key_1), ("fc", Qt.Key.Key_2), ("45", Qt.Key.Key_3)):
        dialog.filter_edit.setText(search)
        QTest.keyClick(dialog.filter_edit, Qt.Key.Key_Return)
        QTest.keyClick(dialog.table, key)

    assert stored_slots(config) == {"WIN 9MM LUGER": 1, "FC 9MM LUGER": 2, "WIN .45 AUTO": 3, "S B 9MM LUGER": 0}


def test_the_next_search_lands_on_its_top_match_not_the_row_just_edited(config, assigner) -> None:
    # The edited row still matches the next search; it must not stay current,
    # or the next digit overwrites it instead of routing what was searched for.
    seed_model(config, {"WIN 9MM LUGER": 0, "WIN 9MM LUGER +P": 0})
    dialog = assigner()
    QTest.keyClicks(dialog.filter_edit, "win 9")
    QTest.keyClick(dialog.filter_edit, Qt.Key.Key_Return)
    type_keys(dialog, Qt.Key.Key_Down, Qt.Key.Key_5)

    QTest.keyClicks(dialog.table, "w")
    QTest.keyClicks(dialog.filter_edit, "in 9mm luger")
    QTest.keyClick(dialog.filter_edit, Qt.Key.Key_Return)
    type_keys(dialog, Qt.Key.Key_3)

    assert stored_slots(config) == {"WIN 9MM LUGER": 3, "WIN 9MM LUGER +P": 5}


def test_an_edit_lands_in_the_active_sorting_template(config, assigner) -> None:
    seed_model(config, {"9mm FC": 0})
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_4)

    assert config.active_slot_template().assignments["headstamps"] == {"9mm FC": 4}


@pytest.mark.parametrize("key", [Qt.Key.Key_0, Qt.Key.Key_Delete, Qt.Key.Key_Backspace])
def test_zero_delete_and_backspace_send_a_row_to_the_catch_all(window, config, assigner, key) -> None:
    seed_model(config, {"9mm FC": 5})
    dialog = assigner()

    type_keys(dialog, key)

    assert stored_slots(config) == {"9mm FC": 0}
    assert table(dialog)["9mm FC"] == EMPTY_VALUE
    assert window.slot_grid.cards[5].names_label.text() == EMPTY_HINT


def test_a_slot_the_machine_does_not_have_is_refused(config, assigner) -> None:
    seed_model(config, {"9mm FC": 2})
    dialog = assigner()  # 8 slots: the Catch-All plus 1-7

    type_keys(dialog, Qt.Key.Key_9)

    assert stored_slots(config) == {"9mm FC": 2}
    assert "no slot 9" in dialog.status_label.text()


def test_an_edit_does_not_reorder_the_rows(config, assigner) -> None:
    seed_model(config, {"A": 0, "B": 0, "C": 0})
    dialog = assigner()
    select(dialog, "B")

    type_keys(dialog, Qt.Key.Key_2, Qt.Key.Key_Down, Qt.Key.Key_3)

    assert table(dialog) == {"A": EMPTY_VALUE, "B": "2", "C": "3"}


def test_enter_in_the_list_never_closes_the_view(config, assigner) -> None:
    seed_model(config, {"9mm FC": 0})
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_Return, Qt.Key.Key_Enter)

    assert dialog.isVisible()


# ----- slot numbers past 9 ---------------------------------------------------


@pytest.fixture
def sixteen_slots(config):
    config.serial["slot_quantity"] = 16  # the Catch-All plus 1-15
    return config


def test_a_digit_that_could_still_grow_waits(sixteen_slots, assigner) -> None:
    seed_model(sixteen_slots, {"9mm FC": 0})
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_1)
    assert stored_slots(sixteen_slots) == {"9mm FC": 0}
    assert table(dialog)["9mm FC"] == "1…"

    type_keys(dialog, Qt.Key.Key_2)
    assert stored_slots(sixteen_slots) == {"9mm FC": 12}
    assert table(dialog)["9mm FC"] == "12"


def test_enter_settles_a_number_that_could_still_grow(sixteen_slots, assigner) -> None:
    seed_model(sixteen_slots, {"9mm FC": 0})
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_1, Qt.Key.Key_Return)

    assert stored_slots(sixteen_slots) == {"9mm FC": 1}


def test_a_digit_that_cannot_grow_lands_at_once(sixteen_slots, assigner) -> None:
    seed_model(sixteen_slots, {"9mm FC": 0})
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_7)

    assert stored_slots(sixteen_slots) == {"9mm FC": 7}


@pytest.mark.parametrize("key", [Qt.Key.Key_Escape, Qt.Key.Key_Backspace, Qt.Key.Key_Down])
def test_a_half_typed_number_can_be_abandoned(sixteen_slots, assigner, key) -> None:
    seed_model(sixteen_slots, {"A": 4, "B": 0})
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_1, key)

    # Backspace/Escape take the digit back rather than unassigning; nothing closes.
    assert stored_slots(sixteen_slots) == {"A": 4, "B": 0}
    assert table(dialog) == {"A": "4", "B": EMPTY_VALUE}
    assert dialog.pending == ""
    assert dialog.isVisible()


# ----- the mouse path --------------------------------------------------------


def test_the_slot_spinbox_commits_through_config(config, assigner) -> None:
    seed_model(config, {"9mm FC": 0})
    dialog = assigner()
    index = dialog.table.model().index(0, SLOT_COLUMN)
    delegate = dialog.table.itemDelegateForColumn(SLOT_COLUMN)

    editor = delegate.createEditor(dialog.table.viewport(), QStyleOptionViewItem(), index)
    assert isinstance(editor, QSpinBox)
    assert editor.maximum() == 7
    editor.setValue(6)
    delegate.setModelData(editor, dialog.table.model(), index)

    assert stored_slots(config) == {"9mm FC": 6}
    assert table(dialog)["9mm FC"] == "6"


# ----- modes -----------------------------------------------------------------


def test_ai_config_mode_edits_the_settings_backed_headstamps(window, config, assigner) -> None:
    config.add_headstamp("9mm RP", 0)
    dialog = assigner()

    type_keys(dialog, Qt.Key.Key_5)

    assert stored_slots(config) == {"9mm RP": 5}
    assert window.slot_grid.cards[5].names_label.text() == "9mm RP"


@pytest.fixture
def parent_model(config):
    """Two Winchester loads grouped under a parent, and one ungrouped headstamp."""
    model_id = seed_model(config, {"WIN 9MM LUGER": 0, "WIN .45 AUTO": 0, "FC 9MM LUGER": 0})
    parent = HeadstampParentRepo(config.db).add(model_id, "Winchester")
    repo = HeadstampRepo(config.db)
    for headstamp in repo.list_for_model(model_id):
        if headstamp.name.startswith("WIN"):
            repo.set_parent(headstamp.id, parent.id)
    config.set_use_parent_classifications(True)
    return config


def test_parent_mode_lists_groups_and_ungrouped_headstamps(parent_model, assigner) -> None:
    dialog = assigner()

    assert dialog.hint_label.text() == PARENT_HINT
    assert list(table(dialog)) == ["FC 9MM LUGER", "Winchester"]
    assert not dialog.table.isColumnHidden(CONTAINS_COLUMN)
    assert cell(dialog, 1, CONTAINS_COLUMN).text() == "WIN .45 AUTO, WIN 9MM LUGER"


def test_parent_mode_finds_a_group_by_one_of_its_headstamps(parent_model, assigner) -> None:
    dialog = assigner()

    dialog.filter_edit.setText("9mm win")

    assert list(table(dialog)) == ["Winchester"]


def test_parent_mode_routes_through_the_group(window, parent_model, assigner) -> None:
    dialog = assigner()
    dialog.filter_edit.setText("winchester")

    type_keys(dialog, Qt.Key.Key_2)

    assert [p["slot"] for p in parent_model.parents_with_slots()] == [2]
    assert parent_model.active_slot_template().assignments["parents"] == {"Winchester": 2}
    assert window.slot_grid.cards[2].names_label.text() == "Winchester"


def test_standard_mode_hides_the_contains_column(config, assigner) -> None:
    seed_model(config, {"9mm FC": 0})

    assert assigner().table.isColumnHidden(CONTAINS_COLUMN)


@pytest.fixture
def package_model(config):
    seed_model(config, {"9mm FC": 0, ".223 LC": 0})
    config.set_run_package_mode(True)
    return config


def test_package_mode_digits_toggle_slots(window, package_model, assigner) -> None:
    dialog = assigner()
    assert dialog.hint_label.text() == PACKAGE_HINT
    select(dialog, "9mm FC")

    type_keys(dialog, Qt.Key.Key_1, Qt.Key.Key_3)
    assert package_model.slots_for_headstamp_package("9mm FC") == [1, 3]
    assert table(dialog)["9mm FC"] == "1, 3"
    assert window.slot_grid.cards[3].names_label.text() == "9mm FC"

    type_keys(dialog, Qt.Key.Key_1)
    assert package_model.slots_for_headstamp_package("9mm FC") == [3]
    assert package_model.active_slot_template("package").assignments == {"slots": {"3": ["9mm FC"]}}


def test_package_mode_zero_clears_every_slot(package_model, assigner) -> None:
    package_model.set_package_slot_headstamp(2, "9mm FC", True)
    package_model.set_package_slot_headstamp(4, "9mm FC", True)
    dialog = assigner()
    select(dialog, "9mm FC")

    type_keys(dialog, Qt.Key.Key_0)

    assert package_model.slots_for_headstamp_package("9mm FC") == []
    assert table(dialog)["9mm FC"] == EMPTY_VALUE


def test_package_mode_has_no_single_slot_spinbox(package_model, assigner) -> None:
    dialog = assigner()

    flags = cell(dialog, 0, SLOT_COLUMN).flags()

    assert not flags & Qt.ItemFlag.ItemIsEditable
