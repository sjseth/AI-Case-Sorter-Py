"""Theme palettes, the live PALETTE switch, and the title-bar picker.

The palette invariants run anywhere tkinter imports; the widget tests need a
real display (xvfb in practice), matching the other UI tests.
"""

from __future__ import annotations

import gc
import re
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

import tkinter as tk
from tkinter import ttk

from sorter.ui import theme
from sorter.ui.theme import (
    DEFAULT_THEME,
    HALFTONE_INK,
    INK_OUTLINE,
    PALETTE,
    SETTING_THEME,
    THEMES,
    apply_theme,
    current_theme,
    halftone_ink,
    ink_outline,
    paint_halftone,
    resolve_theme,
    retheme_widgets,
    row_style,
    set_palette,
    theme_names,
)

_HEX = re.compile(r"^#[0-9a-f]{6}$")

# Roles that intentionally share a color inside a theme: the status text is
# the button color rendered as text. `retheme_widgets` resolves a color to
# the first matching role, so any *other* duplicate would make an existing
# widget's re-colour ambiguous.
_ALIASES = (("success", "action"), ("error", "danger"))


@pytest.fixture(autouse=True)
def _restore_default_theme():
    """Keep a test's theme switch from leaking into the next test."""
    original = current_theme()
    yield
    set_palette(original)


@pytest.fixture(scope="module")
def _tk_root():
    # One root for the module: churning dozens of independent Tk roots through
    # a single process is fragile (see the note in tests/test_monitor.py).
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    # Let the widgets die while the interpreter is still up (see the
    # collection fixture in conftest.py).
    gc.collect()
    r.destroy()


@pytest.fixture
def root(_tk_root):
    yield _tk_root
    for child in _tk_root.winfo_children():
        child.destroy()


# ----- palette invariants -----------------------------------------------------


def test_default_theme_is_offered() -> None:
    assert DEFAULT_THEME in THEMES
    assert theme_names()[0] == DEFAULT_THEME
    assert len(THEMES) >= 4


@pytest.mark.parametrize("name", list(THEMES))
def test_every_theme_defines_the_same_roles(name: str) -> None:
    assert set(THEMES[name]) == set(THEMES[DEFAULT_THEME]), (
        f"{name} does not define the same palette keys as {DEFAULT_THEME}"
    )


@pytest.mark.parametrize("name", list(THEMES))
def test_colors_are_six_digit_hex(name: str) -> None:
    bad = {k: v for k, v in THEMES[name].items() if not _HEX.match(v.lower())}
    assert not bad, f"{name} has non-hex colors: {bad}"


@pytest.mark.parametrize("name", list(THEMES))
def test_status_colors_track_their_buttons(name: str) -> None:
    palette = THEMES[name]
    for status, button in _ALIASES:
        assert palette[status] == palette[button], (
            f"{name}: {status} must equal {button} so a re-themed widget can't pick the wrong role"
        )


@pytest.mark.parametrize("name", list(THEMES))
def test_roles_are_otherwise_uniquely_coloured(name: str) -> None:
    aliased = {status for status, _ in _ALIASES}
    seen: dict[str, str] = {}
    for key, color in THEMES[name].items():
        if key in aliased:
            continue
        assert color not in seen, (
            f"{name}: {key} and {seen[color]} share {color}; give them "
            "distinct values so re-theming resolves the role unambiguously"
        )
        seen[color] = key


# ----- selecting a palette ----------------------------------------------------


def test_set_palette_mutates_the_shared_dict_in_place() -> None:
    # Modules do `from .theme import PALETTE` — rebinding would strand them.
    before = theme.PALETTE
    set_palette("Light")

    assert theme.PALETTE is before is PALETTE
    assert PALETTE["bg_window"] == THEMES["Light"]["bg_window"]
    assert current_theme() == "Light"


def test_set_palette_returns_the_outgoing_palette() -> None:
    set_palette("Dark")
    previous = set_palette("Gothic")

    assert previous == THEMES["Dark"]
    assert PALETTE == THEMES["Gothic"]


def test_unknown_theme_falls_back_to_the_default() -> None:
    set_palette("Chartreuse")

    assert current_theme() == DEFAULT_THEME
    assert PALETTE == THEMES[DEFAULT_THEME]


def test_resolve_theme_is_case_insensitive_and_null_safe() -> None:
    assert resolve_theme("midnight blue") == "Midnight Blue"
    assert resolve_theme(None) == DEFAULT_THEME
    assert resolve_theme("") == DEFAULT_THEME


def test_apply_theme_selects_the_palette(root) -> None:
    apply_theme(root, theme="Sepia")

    assert current_theme() == "Sepia"
    assert PALETTE["bg_card"] == THEMES["Sepia"]["bg_card"]
    assert ttk.Style(root).lookup("TFrame", "background") == THEMES["Sepia"]["bg_surface"]


# ----- re-colouring live widgets ----------------------------------------------


def test_retheme_walks_the_tree_and_translates_roles(root) -> None:
    set_palette("Dark")
    frame = tk.Frame(root, bg=PALETTE["bg_surface"])
    label = tk.Label(
        frame,
        text="hi",
        bg=PALETTE["bg_card"],
        fg=PALETTE["text_muted"],
    )
    label.pack()

    previous = set_palette("Light")
    retheme_widgets(root, previous)

    assert frame.cget("bg") == THEMES["Light"]["bg_surface"]
    assert label.cget("bg") == THEMES["Light"]["bg_card"]
    assert label.cget("fg") == THEMES["Light"]["text_muted"]


def test_retheme_leaves_non_palette_colors_alone(root) -> None:
    set_palette("Dark")
    # The monitor's "snake" borders are its own colors, not palette roles.
    label = tk.Label(root, bg="#ff00ff", fg=PALETTE["text"])

    previous = set_palette("Gothic")
    retheme_widgets(root, previous)

    assert label.cget("bg") == "#ff00ff"
    assert label.cget("fg") == THEMES["Gothic"]["text"]


def test_retheme_skips_ttk_widgets_without_error(root) -> None:
    set_palette("Dark")
    button = ttk.Button(root, text="ok")
    entry = ttk.Entry(root)

    previous = set_palette("Light")
    retheme_widgets(root, previous)

    # No exception, and the style (not an instance override) drives the color.
    assert button.cget("style") == ""
    assert entry.winfo_exists()


# ----- the halftone field -----------------------------------------------------


@pytest.fixture
def painted_canvas(root):
    """A canvas with a real, mapped size.

    The paint helpers bail out on a widget Tk hasn't laid out yet (it reports
    a width of 1), which is why the window has to come up for these.
    """
    root.deiconify()
    canvas = tk.Canvas(root, width=400, height=36, highlightthickness=0)
    canvas.pack()
    root.update()
    yield canvas
    root.withdraw()


def test_halftone_is_opt_in_per_theme() -> None:
    assert set(HALFTONE_INK) <= set(THEMES)

    set_palette("Comic Book")
    assert halftone_ink() == HALFTONE_INK["Comic Book"]

    set_palette("Dark")
    assert halftone_ink() is None


def test_painting_dots_replaces_the_previous_field(painted_canvas) -> None:
    canvas = painted_canvas

    paint_halftone(canvas, color="#123a86")
    first = canvas.find_withtag("halftone")
    assert len(first) > 20

    paint_halftone(canvas, color="#123a86")
    assert len(canvas.find_withtag("halftone")) == len(first)


def test_dots_shrink_toward_the_fade_and_stop(painted_canvas) -> None:
    canvas = painted_canvas

    paint_halftone(canvas, color="#123a86", fade_span=200)
    widths = {}
    for item in canvas.find_withtag("halftone"):
        x1, _y1, x2, _y2 = canvas.coords(item)
        widths[round(x1)] = x2 - x1

    assert max(widths) < 200  # nothing past the fade
    left, right = min(widths), max(widths)
    assert widths[left] > widths[right]  # the screen thins out rightwards


def test_no_ink_means_no_dots(painted_canvas) -> None:
    canvas = painted_canvas
    paint_halftone(canvas, color="#123a86")

    paint_halftone(canvas, color=None)

    assert canvas.find_withtag("halftone") == ()


# ----- ink outlines -----------------------------------------------------------


def test_ink_outline_is_opt_in_per_theme() -> None:
    assert set(INK_OUTLINE) <= set(THEMES)

    set_palette("Comic Book")
    assert ink_outline() == INK_OUTLINE["Comic Book"] > 0

    set_palette("Dark")
    assert ink_outline() == 0


def test_outlined_theme_draws_cards_in_ink(root) -> None:
    apply_theme(root, theme="Comic Book")
    style = ttk.Style(root)

    assert int(style.lookup("Card.TFrame", "borderwidth")) == INK_OUTLINE["Comic Book"]
    assert style.lookup("Card.TFrame", "bordercolor") == THEMES["Comic Book"]["border"]


def test_flat_theme_keeps_its_borderless_cards(root) -> None:
    apply_theme(root, theme="Dark")
    style = ttk.Style(root)

    assert int(style.lookup("Card.TFrame", "borderwidth") or 0) == 0


@pytest.mark.parametrize("card", ["Card.TFrame", "CardHover.TFrame", "CardSel.TFrame"])
def test_card_rows_share_the_fill_but_never_the_outline(root, card: str) -> None:
    # Layout rows inside a card are restyled to the row variant, so only the
    # card itself draws a box.
    apply_theme(root, theme="Comic Book")
    style = ttk.Style(root)
    row = row_style(card)

    assert row != card
    assert style.lookup(row, "background") == style.lookup(card, "background")
    assert int(style.lookup(row, "borderwidth") or 0) == 0


# ----- the title-bar picker ---------------------------------------------------


@pytest.fixture(autouse=True)
def _data_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))


def _stub_window(root, db=None):
    """MainWindow's theme methods over stub state.

    Constructing a real MainWindow opens the camera and probes serial ports,
    which no test should do; borrowing the methods (as test_updater_ui does)
    exercises the shipped implementations rather than a copy of them.
    """
    pytest.importorskip("cv2")
    from sorter.ui.app import HEADER_HEIGHT, MainWindow

    class _StubWindow:
        _layout_page = MainWindow._layout_page
        _load_setting = MainWindow._load_setting
        _save_setting = MainWindow._save_setting
        _on_theme_selected = MainWindow._on_theme_selected
        _place_theme_picker = MainWindow._place_theme_picker
        _refresh_status_indicators = MainWindow._refresh_status_indicators
        _repaint_header = MainWindow._repaint_header
        set_theme = MainWindow.set_theme

        def __init__(self) -> None:
            self.root = root
            self.db = db
            self.status = ""
            self.fonts = apply_theme(root, theme=self._load_setting(SETTING_THEME))
            self.theme_name = current_theme()
            self.theme_var = tk.StringVar(value=self.theme_name)
            self.header_canvas = tk.Canvas(root, height=HEADER_HEIGHT)
            self.header_canvas.pack(fill=tk.X)
            self.page = tk.Canvas(root, height=120)
            self.page.pack(fill=tk.BOTH, expand=True)
            self._notebook_window = self.page.create_window(
                0,
                0,
                window=ttk.Frame(self.page),
                anchor=tk.NW,
            )
            self._page_size = (0, 0)
            self.theme_combo = ttk.Combobox(
                self.header_canvas,
                textvariable=self.theme_var,
                values=theme_names(),
                state="readonly",
                style="Header.TCombobox",
            )
            self._theme_window = self.header_canvas.create_window(
                0,
                HEADER_HEIGHT // 2,
                anchor=tk.E,
                window=self.theme_combo,
            )
            self.theme_new_button = ttk.Button(self.header_canvas, text="+", width=2)
            self._theme_new_window = self.header_canvas.create_window(
                0,
                HEADER_HEIGHT // 2,
                anchor=tk.E,
                window=self.theme_new_button,
            )
            self._camera_connected = False
            self._serial_connected = True
            # set_theme repaints an open serial monitor's Text tags.
            self._serial_monitor = None
            self.camera_dot = tk.Label(
                root,
                text="●",
                bg=PALETTE["bg_window"],
                fg=PALETTE["error"],
            )
            self.serial_dot = tk.Label(
                root,
                text="●",
                bg=PALETTE["bg_window"],
                fg=PALETTE["success"],
            )

        def set_status(self, message: str) -> None:
            self.status = message

    return _StubWindow()


def test_picker_offers_every_theme(root) -> None:
    win = _stub_window(root)

    assert list(win.theme_combo.cget("values")) == theme_names()
    assert win.theme_var.get() == DEFAULT_THEME


def test_selecting_a_theme_applies_it(root) -> None:
    win = _stub_window(root)
    panel = tk.Frame(root, bg=PALETTE["bg_card"])

    win.theme_var.set("Midnight Blue")
    win._on_theme_selected()

    assert current_theme() == "Midnight Blue"
    assert win.theme_name == "Midnight Blue"
    assert panel.cget("bg") == THEMES["Midnight Blue"]["bg_card"]
    assert ttk.Style(root).lookup("TFrame", "background") == (THEMES["Midnight Blue"]["bg_surface"])


def test_status_dots_keep_their_meaning_across_a_switch(root) -> None:
    win = _stub_window(root)

    win.set_theme("Light")

    assert win.camera_dot.cget("fg") == THEMES["Light"]["error"]  # disconnected
    assert win.serial_dot.cget("fg") == THEMES["Light"]["success"]  # connected
    assert win.camera_dot.cget("bg") == THEMES["Light"]["bg_window"]


def test_theme_choice_round_trips_through_settings(root) -> None:
    pytest.importorskip("cv2")
    from sorter.data.db import Database
    from sorter.data.repository import SettingsRepo

    db = Database()
    db.ensure_initialized()
    try:
        win = _stub_window(root, db)
        win.set_theme("Gothic")
        assert SettingsRepo(db).get(SETTING_THEME) == "Gothic"

        # A later launch starts on the stored theme.
        set_palette(DEFAULT_THEME)
        restored = _stub_window(root, db)
        assert restored.theme_name == "Gothic"
        assert current_theme() == "Gothic"
    finally:
        db.close()


def test_theme_applies_even_without_a_database(root) -> None:
    win = _stub_window(root, db=None)

    win.set_theme("Sepia")

    assert current_theme() == "Sepia"
