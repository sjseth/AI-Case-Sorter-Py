"""The sorting-template bar and its two dialogs.

The dialogs are driven by calling their button handlers directly — never
``exec()`` — and their ``notify``/``confirm`` hooks are replaced so nothing
modal can open.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from sorter.ui.dialog_template import EditTemplateDialog, NewTemplateDialog
from sorter.ui.slot_grid import EMPTY_HINT

from .conftest import seed_model


def combo_items(window) -> list[str]:
    return [window.template_combo.itemText(i) for i in range(window.template_combo.count())]


def select_template(window, name: str) -> None:
    """Pick a template the way a user does — index in, `activated` out."""
    window.template_combo.setCurrentIndex(combo_items(window).index(name))
    window._on_template_selected(window.template_combo.currentIndex())


def new_dialog(window, config, name: str, *, copy_current: bool = True) -> NewTemplateDialog:
    mode = config.slot_template_mode()
    dialog = NewTemplateDialog(config, mode, config.active_slot_template(mode).name, window)
    dialog.notify = lambda title, text: pytest.fail(f"unexpected dialog: {title} — {text}")
    dialog.name_edit.setText(name)
    dialog.copy_radio.setChecked(copy_current)
    dialog.blank_radio.setChecked(not copy_current)
    return dialog


def edit_dialog(window, config, *, can_delete: bool = True) -> EditTemplateDialog:
    mode = config.slot_template_mode()
    dialog = EditTemplateDialog(
        config,
        config.active_slot_template(mode),
        can_delete=can_delete,
        parent=window,
    )
    dialog.notify = lambda title, text: pytest.fail(f"unexpected dialog: {title} — {text}")
    dialog.confirm = lambda title, text: True
    return dialog


# ----- the bar ---------------------------------------------------------------


def test_the_bar_seeds_a_default_template(window) -> None:
    assert combo_items(window) == ["Default"]
    assert window.template_combo.currentText() == "Default"


def test_package_mode_has_its_own_template_list(window, config) -> None:
    window.package_check.setChecked(True)

    assert combo_items(window) == ["Default"]
    assert window.template_hint.text() == "Package-mode layout"
    assert config.slot_template_mode() == "package"


# ----- create / switch -------------------------------------------------------


def test_creating_a_template_copies_the_current_layout(window, config) -> None:
    seed_model(config, {"9mm FC": 1})
    window._refresh_templates()

    dialog = new_dialog(window, config, "Range brass")
    dialog.create_template()
    window._after_template_change("created")

    assert combo_items(window) == ["Default", "Range brass"]
    assert window.template_combo.currentText() == "Range brass"
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"


def test_switching_templates_restores_the_other_layout(window, config) -> None:
    seed_model(config, {"9mm FC": 1})
    window._refresh_templates()
    new_dialog(window, config, "Empty bench", copy_current=False).create_template()
    window._after_template_change("created")
    # The blank template cleared the slot it doesn't mention.
    assert window.slot_grid.cards[1].names_label.text() == EMPTY_HINT

    select_template(window, "Default")

    assert window.template_combo.currentText() == "Default"
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"
    assert config.slot_for_headstamp("9mm FC") == 1

    select_template(window, "Empty bench")

    assert window.slot_grid.cards[1].names_label.text() == EMPTY_HINT
    assert config.slot_for_headstamp("9mm FC") == 0


def test_switching_a_template_zeroes_the_counters(window, config) -> None:
    new_dialog(window, config, "Second").create_template()
    window._after_template_change("created")
    window.bus.post("run/result", {"ok": True, "slot": 2})
    window.bus.drain()

    select_template(window, "Default")

    assert window.slot_grid.cards[2].count_label.text() == "0"
    assert window.master_count_label.text() == "0"


def test_a_duplicate_name_is_reported_not_raised(window, config) -> None:
    dialog = new_dialog(window, config, "Default")
    warnings = []
    dialog.notify = lambda title, text: warnings.append(title)

    dialog.create_template()

    assert warnings == ["Can't create template"]
    assert dialog.created is None


def test_templates_cannot_be_switched_during_a_run(window, config, monkeypatch) -> None:
    new_dialog(window, config, "Second").create_template()
    window._after_template_change("created")
    notices = []
    monkeypatch.setattr(window, "notify", lambda title, text: notices.append(title))
    window.bus.post("run/started", None)
    window.bus.drain()

    select_template(window, "Default")

    assert notices == ["Run in progress"]
    # The combo snapped back to whatever is actually loaded.
    assert window.template_combo.currentText() == "Second"


# ----- rename / delete -------------------------------------------------------


def test_renaming_the_active_template(window, config) -> None:
    dialog = edit_dialog(window, config, can_delete=False)
    dialog.name_edit.setText("Match prep")

    dialog.rename_template()
    window._after_template_change("renamed")

    assert combo_items(window) == ["Match prep"]


def test_deleting_a_template_loads_the_one_that_remains(window, config) -> None:
    seed_model(config, {"9mm FC": 1})
    window._refresh_templates()
    new_dialog(window, config, "Empty bench", copy_current=False).create_template()
    window._after_template_change("created")

    dialog = edit_dialog(window, config)
    dialog.delete_template()
    window._after_template_change("deleted")

    assert combo_items(window) == ["Default"]
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"


def test_the_last_template_offers_no_delete_button(window, config) -> None:
    assert edit_dialog(window, config, can_delete=False).delete_button is None


def test_deleting_the_last_template_is_refused(window, config) -> None:
    dialog = edit_dialog(window, config)
    warnings = []
    dialog.notify = lambda title, text: warnings.append(title)

    dialog.delete_template()

    assert warnings == ["Can't delete template"]
    assert combo_items(window) == ["Default"]
