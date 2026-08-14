"""Every palette has to survive the trip to QSS — no gaps, no format artifacts."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from sorter.ui.palettes import BUILTIN_THEMES
from sorter.ui.theme import GROUPBOX_TOP_MARGIN, INDICATOR_SIZE, build_stylesheet


def block(qss: str, selector: str) -> str:
    """The declarations of the rule opened by `selector`."""
    start = qss.index(selector + " {")
    return qss[start : qss.index("}", start)]


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_stylesheet_carries_the_palette(name: str) -> None:
    palette = BUILTIN_THEMES[name]
    qss = build_stylesheet(palette)

    assert isinstance(qss, str)
    for role in ("bg_window", "action", "danger"):
        assert palette[role] in qss, f"{name}: {role} missing from the stylesheet"


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_stylesheet_is_well_formed(name: str) -> None:
    qss = build_stylesheet(BUILTIN_THEMES[name])

    assert qss.count("{") == qss.count("}")
    assert "{bg_" not in qss and "{c[" not in qss, "unsubstituted format placeholder"
    assert "None" not in qss


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_sidebar_labels_use_the_two_roles_the_icons_are_inked_from(name: str) -> None:
    # app._paint_sidebar_icon reads these same two roles: a stylesheet cannot
    # reach a QIcon, so the pairing is only kept by hand.
    palette = BUILTIN_THEMES[name]
    qss = build_stylesheet(palette)

    assert f"color: {palette['text_muted']};" in qss
    assert f"color: {palette['text_highlight']};" in qss
    # The gear is a drawn icon now — no per-role color workaround left.
    assert "settingsButton" not in qss


# ----- toggles have to look like toggles -------------------------------------


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_check_indicators_are_sized_and_inked_from_the_palette(name: str) -> None:
    palette = BUILTIN_THEMES[name]
    qss = build_stylesheet(palette)

    base = block(qss, "QCheckBox::indicator, QRadioButton::indicator")
    assert f"width: {INDICATOR_SIZE}px" in base
    assert f"height: {INDICATOR_SIZE}px" in base
    assert f"border: 1px solid {palette['border_focus']};" in base
    assert f"background-color: {palette['bg_input']};" in base

    checked = block(qss, "QCheckBox::indicator:checked, QRadioButton::indicator:checked")
    assert f"background-color: {palette['action']};" in checked
    # A disabled toggle that is ON must still read as on, so :checked has to
    # come last of the two same-specificity rules.
    assert qss.index("QCheckBox::indicator:disabled") < qss.index("QCheckBox::indicator:checked")


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_a_checked_box_actually_paints_the_action_colour(qapp, name: str) -> None:
    """The QSS is only half the claim — this renders it and counts pixels."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QCheckBox

    palette = BUILTIN_THEMES[name]
    box = QCheckBox("Sort while training")
    box.setStyleSheet(build_stylesheet(palette))
    action = QColor(palette["action"]).rgb()

    def action_pixels() -> int:
        image = box.grab().toImage()
        return sum(image.pixel(x, y) == action for y in range(image.height()) for x in range(min(image.width(), 40)))

    assert action_pixels() == 0  # unchecked: nothing is filled
    box.setChecked(True)
    # Half the indicator's area is a floor no rounding or border can eat into.
    assert action_pixels() > INDICATOR_SIZE * INDICATOR_SIZE // 2
    box.deleteLater()


def test_the_group_splitter_handle_drops_to_the_group_frames() -> None:
    """The divider must not run above the borders it divides (JL, screenshot)."""
    qss = build_stylesheet(BUILTIN_THEMES["Dark"])

    assert f"margin-top: {GROUPBOX_TOP_MARGIN}px" in block(qss, "QGroupBox")
    assert f"margin-top: {GROUPBOX_TOP_MARGIN}px" in block(qss, "QSplitter#groupSplitter::handle:horizontal")
