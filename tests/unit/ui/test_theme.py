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


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_disabled_role_buttons_drop_their_hue(name: str) -> None:
    """A disabled Disconnect stayed fully red next to an enabled plain-gray
    button, so the row read backwards — red looked live, gray looked dead."""
    palette = BUILTIN_THEMES[name]
    qss = build_stylesheet(palette)
    rule_start = qss.index("QPushButton#action:disabled, QPushButton#update:disabled, QPushButton#danger:disabled")
    rule = qss[rule_start : qss.index("}", rule_start)]
    assert palette["text_muted"] in rule
    assert "background-color: transparent" in rule
    for hue in (palette["action"], palette["danger"], palette["update"]):
        assert hue not in rule


# ----- spinbox step arrows (#145) ---------------------------------------------


def _side_by_side_style():
    """Fusion with Windows 11's spinbox geometry: up and down side by side.

    qwindows11style.cpp lays the buttons out horizontally while the stylesheet
    style sizes the edit field for one stacked column, so the edit field grew
    over the up button. That style only exists on Windows; this reproduces its
    one relevant difference everywhere.
    """
    from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

    class SideBySide(QProxyStyle):
        def subControlRect(self, cc, opt, sc, widget):
            buttons = (QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown)
            if cc != QStyle.ComplexControl.CC_SpinBox or sc not in buttons:
                return super().subControlRect(cc, opt, sc, widget)
            side = opt.rect.height() - 2
            rect = opt.rect.adjusted(opt.rect.width() - 2 * side - 1, 1, -1, -1)
            rect.setWidth(side)
            if sc == QStyle.SubControl.SC_SpinBoxDown:
                rect.translate(side, 0)
            return rect

    return SideBySide(QStyleFactory.create("Fusion"))


def _spin_styles() -> list[str]:
    from PySide6.QtWidgets import QStyleFactory

    return [*QStyleFactory.keys(), "side-by-side"]


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_spinbox_arrows_step_the_value_under_every_style(qapp, name: str) -> None:
    """Clicking an arrow must reach the spinbox, not the edit field over it.

    The click goes to whichever widget is really under the arrow's centre, as
    a mouse would; a click sent straight to the spinbox would pass regardless.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QSpinBox, QStyle, QStyleFactory, QStyleOptionSpinBox

    qss = build_stylesheet(BUILTIN_THEMES[name])
    for style_name in _spin_styles():
        spin = QSpinBox()
        style = _side_by_side_style() if style_name == "side-by-side" else QStyleFactory.create(style_name)
        style.setParent(spin)
        spin.setStyle(style)
        spin.setStyleSheet(qss)
        spin.setRange(0, 99)
        spin.setValue(50)
        spin.resize(spin.sizeHint().width() + 60, spin.sizeHint().height())
        spin.show()

        for label, control, expected in (
            ("up", QStyle.SubControl.SC_SpinBoxUp, 51),
            ("down", QStyle.SubControl.SC_SpinBoxDown, 50),
        ):
            opt = QStyleOptionSpinBox()
            spin.initStyleOption(opt)
            centre = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, opt, control, spin).center()
            target = spin.childAt(centre) or spin
            QTest.mouseClick(
                target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target.mapFrom(spin, centre)
            )
            assert spin.value() == expected, f"{name} / {style_name}: {label} click hit {type(target).__name__}"

        spin.hide()
        spin.deleteLater()
