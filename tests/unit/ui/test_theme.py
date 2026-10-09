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


def _hsv(value: str) -> tuple[int, float, float]:
    """Chroma, hue in degrees, saturation. Independent of theme.py's copy."""
    from PySide6.QtGui import QColor

    color = QColor(value)
    red, green, blue = color.red(), color.green(), color.blue()
    peak, floor = max(red, green, blue), min(red, green, blue)
    chroma = peak - floor
    if peak == 0 or chroma == 0:
        return chroma, 0.0, 0.0
    if peak == red:
        sector = ((green - blue) / chroma) % 6
    elif peak == green:
        sector = (blue - red) / chroma + 2
    else:
        sector = (red - green) / chroma + 4
    return chroma, (sector * 60) % 360, chroma / peak


def _red_hued(value: str) -> bool:
    """Stop/danger red: enough chroma to carry a hue, and that hue is red."""
    chroma, hue, _saturation = _hsv(value)
    return chroma >= 40 and (hue <= 15 or hue >= 345)


def _hexes(text: str) -> list[str]:
    found = []
    start = 0
    while True:
        mark = text.find("#", start)
        if mark < 0 or mark + 7 > len(text):
            break
        token = text[mark : mark + 7]
        if all(char in "0123456789abcdefABCDEF" for char in token[1:]):
            found.append(token.lower())
        start = mark + 1
    return found


def _catch_all_rules(qss: str) -> str:
    return "\n".join(part for part in qss.split("}") if "catchAllMode" in part)


def _contrast(left: str, right: str) -> float:
    from PySide6.QtGui import QColor

    def luminance(value: str) -> float:
        color = QColor(value)

        def channel(component: int) -> float:
            scale = component / 255
            if scale <= 0.04045:
                return scale / 12.92
            return ((scale + 0.055) / 1.055) ** 2.4

        return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())

    hi, lo = sorted((luminance(left), luminance(right)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# fill, hover, press, outline, ink, unchecked-pressed. The first four themes
# keep the accent ramp; Gothic and Comic Book fall through to the text ramp
# because their accent (and Gothic's focus ring) is a stop red.
_CATCH_ALL_ROLES = {
    "Dark": ("accent", "accent_hover", "accent_press", "border_focus", "text_inverse", "bg_card_sel"),
    "Light": ("accent", "accent_hover", "accent_press", "border_focus", "text_inverse", "bg_card_sel"),
    "Sepia": ("accent", "accent_hover", "accent_press", "border_focus", "text_inverse", "bg_card_sel"),
    "Midnight Blue": ("accent", "accent_hover", "accent_press", "border_focus", "text_inverse", "bg_card_sel"),
    "Gothic": ("text", "text_highlight", "text_muted", "text_muted", "text_inverse", "bg_card"),
    "Comic Book": ("text", "text_highlight", "text_muted", "text_muted", "text_inverse", "bg_card_sel"),
}


@pytest.mark.parametrize("name", list(BUILTIN_THEMES))
def test_catch_all_mode_buttons_stay_off_stop_colours(qapp, name: str) -> None:
    """The view toggle's fill and border are never a red-hued palette value."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QPushButton

    palette = BUILTIN_THEMES[name]
    qss = build_stylesheet(palette)
    rules = _catch_all_rules(qss)
    used = _hexes(rules)
    reds = {value.lower() for value in palette.values() if isinstance(value, str) and _red_hued(value)}
    assert used, name
    assert reds.isdisjoint(used), (name, reds & set(used))
    assert all(not _red_hued(color) for color in used), name

    fill, hover, press, outline_key, ink, rest_press = _CATCH_ALL_ROLES[name]
    outline = f"border: 2px solid {palette[outline_key]}"
    resting = block(qss, "QPushButton#catchAllMode")
    checked = block(qss, "QPushButton#catchAllMode:checked")
    checked_hover = block(qss, "QPushButton#catchAllMode:checked:hover")
    checked_press = block(qss, "QPushButton#catchAllMode:checked:pressed")
    unchecked_press = block(qss, "QPushButton#catchAllMode:pressed")

    # No fill on the bare name: that would light both toggles at once.
    # accent_dim is the ordinary button rest, and it is neutral in every builtin.
    assert "background-color" not in resting
    assert outline in resting
    assert f"background-color: {palette[fill]};" in checked
    assert f"color: {palette[ink]};" in checked
    assert outline in checked
    assert palette["success"] not in checked
    assert palette["action"] not in checked
    assert palette["danger"] not in rules
    assert f"background-color: {palette[hover]};" in checked_hover
    assert f"background-color: {palette[press]};" in checked_press
    assert f"background-color: {palette[rest_press]};" in unchecked_press
    assert not _red_hued(palette[fill])
    assert not _red_hued(palette[outline_key])
    assert not _red_hued(palette["accent_dim"])
    # The outline reads against the panel; the lit fill reads against the rest.
    assert _contrast(palette[outline_key], palette["bg_surface"]) >= 3, name
    assert _contrast(palette[outline_key], palette["bg_window"]) >= 3, name
    assert _contrast(palette[fill], palette["accent_dim"]) >= 3, name
    assert _contrast(palette[ink], palette[fill]) >= 4.5, name

    off = QPushButton("Top 10")
    on = QPushButton("ALL")
    for button in (off, on):
        button.setObjectName("catchAllMode")
        button.setCheckable(True)
        button.setStyleSheet(qss)
        button.show()
    on.setChecked(True)
    qapp.processEvents()

    def _near(got: QColor, want: str) -> bool:
        target = QColor(want)
        return (
            max(abs(got.red() - target.red()), abs(got.green() - target.green()), abs(got.blue() - target.blue())) <= 8
        )

    for button, paint in ((off, palette["accent_dim"]), (on, palette[fill])):
        image = button.grab().toImage()
        assert image.width() > 16 and image.height() > 4
        assert _near(image.pixelColor(10, image.height() // 2), paint), name
        # The left edge is the outline, clear of the label and the rounded corner.
        assert _near(image.pixelColor(1, image.height() // 2), palette[outline_key]), name
    off.deleteLater()
    on.deleteLater()


def test_a_user_theme_with_stop_colours_keeps_the_toggle_neutral() -> None:
    """A hand-made palette whose accent is red resolves the toggle from the text ramp."""
    palette = dict(BUILTIN_THEMES["Dark"])
    palette.update(
        {
            "accent": "#d13c45",
            "accent_hover": "#e85860",
            "accent_press": "#a52a31",
            "border_focus": "#c0464e",
            "bg_card_sel": "#46181e",
            "accent_dim": "#cf1b1b",
        }
    )
    qss = build_stylesheet(palette)
    rules = _catch_all_rules(qss)
    used = set(_hexes(rules))
    reds = {value.lower() for value in palette.values() if isinstance(value, str) and _red_hued(value)}
    assert used.isdisjoint(reds)
    resting = block(qss, "QPushButton#catchAllMode")
    checked = block(qss, "QPushButton#catchAllMode:checked")
    assert "background-color: #272727;" in resting
    assert "border: 2px solid #9a9a9a" in resting
    assert "background-color: #d4d4d4;" in checked
    assert "color: #121212;" in checked
    assert "#d13c45" not in rules
    assert "#cf1b1b" not in rules


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
