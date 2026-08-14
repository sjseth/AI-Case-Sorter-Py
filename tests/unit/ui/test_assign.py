"""Slot-assignment editing: the dialog, and what the dashboard does with it.

Every assertion about what was stored goes through a *fresh* ``Config`` on the
same database — the dialog is only allowed to mutate through the Config API,
which is also what keeps the active sorting template in step.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from sorter.data.config import Config
from sorter.data.repository import HeadstampParentRepo, HeadstampRepo
from sorter.ui.dialog_slot_assign import SlotAssignDialog
from sorter.ui.slot_grid import EMPTY_HINT

from .conftest import seed_model


@pytest.fixture
def editor(window, config):
    """Open an assignment dialog wired to the dashboard the way the app wires it."""

    def _open(slot: int) -> SlotAssignDialog:
        dialog = SlotAssignDialog(config, slot, window)
        dialog.changed.connect(window.slot_grid.refresh_assignments)
        return dialog

    return _open


def stored_slots(config) -> dict[str, int]:
    """Headstamp -> slot, read back from the DB through a new Config."""
    return {e["name"]: int(e["slot"]) for e in Config(config.db).load().headstamps}


# ----- opening ---------------------------------------------------------------


def test_clicking_a_card_opens_that_slots_editor(window, monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(window, "open_slot_editor", opened.append)

    QTest.mouseClick(window.slot_grid.cards[3], Qt.MouseButton.LeftButton)

    assert opened == [3]


def test_the_catch_all_has_nothing_to_configure(window) -> None:
    window.open_slot_editor(0)

    assert "ends up here" in window.statusBar().currentMessage()


def test_editor_lists_every_headstamp_of_the_active_model(config, editor) -> None:
    seed_model(config, {"9mm FC": 1, ".223 LC": 0})

    dialog = editor(2)

    assert sorted(dialog.checkboxes) == [".223 LC", "9mm FC"]
    assert not any(box.isChecked() for box in dialog.checkboxes.values())


def test_filter_narrows_the_rows(config, editor) -> None:
    seed_model(config, {"9mm FC": 0, ".223 LC": 0})
    dialog = editor(2)

    dialog.filter_edit.setText("223")

    assert list(dialog.checkboxes) == [".223 LC"]


# ----- standard mode ---------------------------------------------------------


def test_assigning_moves_a_headstamp_off_its_previous_slot(window, config, editor) -> None:
    seed_model(config, {"9mm FC": 1})

    dialog = editor(2)
    dialog.checkboxes["9mm FC"].click()

    assert stored_slots(config) == {"9mm FC": 2}
    assert window.slot_grid.cards[2].names_label.text() == "9mm FC"
    assert window.slot_grid.cards[1].names_label.text() == EMPTY_HINT


def test_a_row_says_which_other_slot_holds_it(config, editor) -> None:
    seed_model(config, {"9mm FC": 4})

    dialog = editor(2)

    assert dialog.findChild(QLabel, "rowHint").text() == "in slot #4"


def test_unticking_returns_a_headstamp_to_the_catch_all(window, config, editor) -> None:
    seed_model(config, {"9mm FC": 2})
    dialog = editor(2)
    assert dialog.checkboxes["9mm FC"].isChecked()

    dialog.checkboxes["9mm FC"].click()

    assert stored_slots(config) == {"9mm FC": 0}
    assert window.slot_grid.cards[2].names_label.text() == EMPTY_HINT


def test_an_edit_lands_in_the_active_sorting_template(config, editor) -> None:
    # Config.sync_active_slot_template is what makes this true; the dialog
    # never touches the templates itself.
    seed_model(config, {"9mm FC": 0})

    editor(3).checkboxes["9mm FC"].click()

    assert config.active_slot_template().assignments["headstamps"] == {"9mm FC": 3}


def test_ai_config_mode_edits_the_settings_backed_headstamps(window, config, editor) -> None:
    config.add_headstamp("9mm RP", 0)

    editor(5).checkboxes["9mm RP"].click()

    assert stored_slots(config) == {"9mm RP": 5}
    assert window.slot_grid.cards[5].names_label.text() == "9mm RP"


# ----- package mode ----------------------------------------------------------


def test_package_mode_puts_one_headstamp_in_several_slots(window, config, editor) -> None:
    seed_model(config, {"9mm FC": 0, ".223 LC": 0})
    config.set_run_package_mode(True)

    editor(1).checkboxes["9mm FC"].click()
    editor(2).checkboxes["9mm FC"].click()
    editor(2).checkboxes[".223 LC"].click()

    assert config.slots_for_headstamp_package("9mm FC") == [1, 2]
    window.slot_grid.refresh_assignments()
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"
    assert window.slot_grid.cards[2].names_label.text() == ".223 LC, 9mm FC"


def test_package_mode_rows_are_ticked_from_the_package_map(config, editor) -> None:
    seed_model(config, {"9mm FC": 0})
    config.set_run_package_mode(True)
    config.set_package_slot_headstamp(3, "9mm FC", True)

    assert editor(3).checkboxes["9mm FC"].isChecked()
    assert not editor(4).checkboxes["9mm FC"].isChecked()


# ----- parent classifications ------------------------------------------------


def test_parent_mode_routes_through_the_parent_row(config, editor) -> None:
    model_id = seed_model(config, {"9mm FC": 0})
    parent = HeadstampParentRepo(config.db).add(model_id, "9mm")
    headstamp = HeadstampRepo(config.db).list_for_model(model_id)[0]
    HeadstampRepo(config.db).set_parent(headstamp.id, parent.id)
    config.set_use_parent_classifications(True)

    dialog = editor(2)

    # The child routes via its parent, so only the parent row is offered.
    assert list(dialog.checkboxes) == ["9mm"]
    dialog.checkboxes["9mm"].click()
    assert [p["slot"] for p in config.parents_with_slots()] == [2]
