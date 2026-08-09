"""Slot-details cell-state logic (regression for the 'stuck headstamp' bug).

Skipped where the GUI stack (tkinter / cv2) isn't importable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

from sorter.ui.tab_run import SlotDetailsPanel


def test_catch_all_is_read_only() -> None:
    assert SlotDetailsPanel._cell_state(0, 0) == (False, "disabled", "")
    assert SlotDetailsPanel._cell_state(0, 4) == (False, "disabled", "")


def test_assigned_to_current_slot_is_checked() -> None:
    assert SlotDetailsPanel._cell_state(2, 2) == (True, "normal", "")


def test_unassigned_is_enabled_and_unchecked() -> None:
    assert SlotDetailsPanel._cell_state(1, 0) == (False, "normal", "")


def test_assigned_elsewhere_is_disabled_with_hint() -> None:
    # A headstamp belongs to exactly one slot: when viewing a different slot it
    # is locked out (greyed) and must be unassigned from its slot before moving.
    checked, state, hint = SlotDetailsPanel._cell_state(1, 3)
    assert checked is False
    assert state == "disabled"
    assert hint == "in slot #3"
