"""The Themes dock: pick a theme off a list and watch the app repaint.

Seth via JL — trying themes is its own activity, and a combo buried in
Settings is the wrong shape for it. What is pinned here is that the panel and
Settings → Theme are two faces of one registry and one ``set_theme``: neither
can drift from the other, and applying through either must not double-apply.

Custom themes are module state in ``sorter.ui.palettes``; ``_clean_registry``
restores whatever was registered before each test.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from sorter.ui.app import DOCK_HOMES
from sorter.ui.palettes import (
    BUILTIN_THEMES,
    custom_themes_payload,
    load_custom_themes,
    normalize_palette,
    register_custom_theme,
    rename_custom_theme,
    theme_names,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    original = custom_themes_payload()
    yield
    load_custom_themes(original)


def listed(window) -> list[str]:
    return [window.theme_list.item(i).text() for i in range(window.theme_list.count())]


def combo_items(window) -> list[str]:
    return [window.theme_combo.itemText(i) for i in range(window.theme_combo.count())]


def pick(window, name: str) -> None:
    """What a click on a row does — the selection change is the signal."""
    window.theme_list.setCurrentRow(listed(window).index(name))


# ----- the panel --------------------------------------------------------------


def test_the_panel_lists_every_theme_with_the_live_one_selected(window) -> None:
    assert listed(window) == theme_names()
    assert window.theme_list.currentItem().text() == window.theme_name


def test_the_panel_is_a_closed_right_hand_dock_with_a_home(window) -> None:
    assert window.themes_dock.windowTitle() == "Themes"
    assert window.themes_dock.isClosed()  # supplementary, like History and the guide
    assert "Re-dock panels" in window.themes_dock.tabWidget().toolTip()
    assert "themes_dock" in dict(DOCK_HOMES)

    registered = window.dock_manager.dockWidgetsMap()
    assert registered[window.themes_dock.objectName()] is window.themes_dock


def test_redock_brings_the_themes_panel_home(window) -> None:
    window.themes_dock.toggleView(True)
    window.themes_dock.setFloating()
    assert window.themes_dock.isFloating()

    window._redock_panels()

    assert not window.themes_dock.isFloating()
    assert window.themes_dock.dockContainer() is window.dock_manager


# ----- one theme, two pickers -------------------------------------------------


def test_picking_a_row_applies_the_theme_and_syncs_the_settings_combo(window) -> None:
    window.set_theme("Dark")
    dark = window.styleSheet()

    pick(window, "Light")

    assert window.theme_name == "Light"
    assert window.styleSheet() != dark
    assert window.theme_combo.currentText() == "Light"


def test_the_settings_combo_syncs_the_panel_back(window) -> None:
    window.theme_combo.setCurrentText("Sepia")

    assert window.theme_name == "Sepia"
    assert window.theme_list.currentItem().text() == "Sepia"


def test_syncing_the_pickers_never_reapplies_the_theme(window) -> None:
    """The two follow each other, so a naive sync would apply twice — once per
    picker, each firing the other's handler."""
    applied: list[str] = []
    original = window._apply_theme
    window._apply_theme = lambda name: (applied.append(name), original(name))[1]

    pick(window, "Gothic")

    assert applied == ["Gothic"]


def test_the_panel_offers_the_same_editor_as_settings(window, monkeypatch) -> None:
    import sorter.ui.dialog_theme_editor as dialog_theme_editor

    opened = []

    class FakeDialog:
        def __init__(self, parent, win) -> None:
            opened.append(win)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(dialog_theme_editor, "ThemeEditorDialog", FakeDialog)

    window.theme_dock_edit_button.click()

    assert opened == [window]


# ----- custom themes ----------------------------------------------------------


def test_a_saved_theme_reaches_both_pickers(window) -> None:
    """``refresh_theme_picker`` is the hook the theme editor already calls
    (``_refresh_picker``) — the panel rides it rather than growing its own."""
    register_custom_theme("Mine", normalize_palette({"action": "#123456"}, BUILTIN_THEMES["Dark"]), base="Dark")

    window.refresh_theme_picker()

    assert "Mine" in listed(window)
    assert "Mine" in combo_items(window)
    assert listed(window) == combo_items(window) == theme_names()


def test_a_rename_keeps_the_list_in_step(window) -> None:
    register_custom_theme("Mine", normalize_palette({"action": "#123456"}, BUILTIN_THEMES["Dark"]), base="Dark")
    window.set_theme("Mine")
    window.refresh_theme_picker()

    rename_custom_theme("Mine", "Renamed")
    window.set_theme("Renamed")
    window.refresh_theme_picker()

    assert "Mine" not in listed(window)
    assert window.theme_list.currentItem().text() == "Renamed"
    assert window.theme_combo.currentText() == "Renamed"


def test_opening_the_panel_rereads_the_registry(window) -> None:
    """A theme saved before the panel was ever opened (or from another
    surface) still has to be in the list the moment it is shown."""
    register_custom_theme("Late", normalize_palette({"action": "#654321"}, BUILTIN_THEMES["Dark"]), base="Dark")
    assert "Late" not in listed(window)

    window.themes_dock.toggleView(True)

    assert "Late" in listed(window)
