"""User-made themes: the registry, and the editor dialog that writes them.

The registry half only needs tkinter to import; the dialog half needs a real
display (xvfb in practice), matching the other UI tests.
"""

from __future__ import annotations

import gc
import json
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

import tkinter as tk

from sorter.ui import theme
from sorter.ui.theme import (
    BUILTIN_THEMES,
    CUSTOM_THEME_VERSION,
    DEFAULT_THEME,
    HALFTONE_INK,
    INK_OUTLINE,
    MAX_THEME_NAME,
    THEMES,
    custom_theme_payload,
    custom_themes_payload,
    editable_roles,
    is_custom_theme,
    load_custom_themes,
    normalize_palette,
    register_custom_theme,
    rename_custom_theme,
    resolve_theme,
    set_palette,
    theme_names,
    unique_theme_name,
    unregister_custom_theme,
)


def _palette(**overrides: str) -> dict[str, str]:
    return normalize_palette(overrides, BUILTIN_THEMES["Dark"])


@pytest.fixture(autouse=True)
def _clean_registry():
    """Custom themes are module state — no test may leak one into the next."""
    original = custom_themes_payload()
    started_on = theme.current_theme()
    yield
    load_custom_themes(original)
    set_palette(started_on)


# ----- the registry -----------------------------------------------------------


def test_a_registered_theme_becomes_selectable() -> None:
    register_custom_theme("Mine", _palette(action="#123456"))

    assert is_custom_theme("Mine")
    assert "Mine" in theme_names()
    assert THEMES["Mine"]["action"] == "#123456"
    assert resolve_theme("mine") == "Mine"

    set_palette("Mine")
    assert theme.PALETTE["action"] == "#123456"


def test_registering_carries_the_halftone_and_outline_options() -> None:
    register_custom_theme(
        "Inky",
        _palette(),
        halftone="#101010",
        outline=2,
        base="Comic Book",
    )
    set_palette("Inky")

    assert HALFTONE_INK["Inky"] == "#101010"
    assert INK_OUTLINE["Inky"] == 2
    assert theme.halftone_ink() == "#101010"
    assert theme.ink_outline() == 2


def test_options_left_off_are_not_registered() -> None:
    register_custom_theme("Plain", _palette(), halftone=None, outline=0)
    set_palette("Plain")

    assert "Plain" not in HALFTONE_INK
    assert "Plain" not in INK_OUTLINE
    assert theme.halftone_ink() is None
    assert theme.ink_outline() == 0


def test_saving_over_a_theme_replaces_it() -> None:
    register_custom_theme("Mine", _palette(action="#111111"))
    register_custom_theme("Mine", _palette(action="#222222"), halftone="#333333")

    assert theme_names().count("Mine") == 1
    assert THEMES["Mine"]["action"] == "#222222"
    assert HALFTONE_INK["Mine"] == "#333333"


def test_built_in_names_are_protected() -> None:
    with pytest.raises(ValueError):
        register_custom_theme("Dark", _palette())
    with pytest.raises(ValueError):
        register_custom_theme("   ", _palette())

    assert THEMES["Dark"] == BUILTIN_THEMES["Dark"]


def test_unregistering_removes_every_trace() -> None:
    register_custom_theme("Gone", _palette(), halftone="#101010", outline=2)

    unregister_custom_theme("Gone")

    assert "Gone" not in theme_names()
    assert "Gone" not in HALFTONE_INK
    assert "Gone" not in INK_OUTLINE
    assert not is_custom_theme("Gone")
    # A built-in handed to the same call is left alone.
    unregister_custom_theme("Dark")
    assert "Dark" in theme_names()


def test_a_deleted_theme_falls_back_to_the_default_when_selected() -> None:
    register_custom_theme("Doomed", _palette())
    set_palette("Doomed")

    unregister_custom_theme("Doomed")
    set_palette("Doomed")

    assert theme.current_theme() == DEFAULT_THEME


def test_renaming_moves_a_theme_rather_than_copying_it() -> None:
    register_custom_theme(
        "Mine",
        _palette(action="#123456"),
        halftone="#101010",
        outline=2,
        base="Gothic",
    )

    rename_custom_theme("Mine", "Yours")

    assert "Mine" not in theme_names()
    assert THEMES["Yours"]["action"] == "#123456"
    assert HALFTONE_INK["Yours"] == "#101010"  # options came along
    assert "Mine" not in HALFTONE_INK
    assert custom_theme_payload("Yours")["based_on"] == "Gothic"


def test_a_rejected_rename_leaves_the_original_alone() -> None:
    register_custom_theme("Mine", _palette(action="#123456"))

    with pytest.raises(ValueError):
        rename_custom_theme("Mine", "Dark")  # a built-in
    with pytest.raises(ValueError):
        rename_custom_theme("Mine", "x" * (MAX_THEME_NAME + 1))
    with pytest.raises(ValueError):
        rename_custom_theme("Sepia", "Mine")  # not renameable

    assert THEMES["Mine"]["action"] == "#123456"
    assert THEMES["Dark"] == BUILTIN_THEMES["Dark"]


def test_names_are_capped_to_fit_the_picker() -> None:
    long_name = "A very long theme name"

    assert len(unique_theme_name(long_name)) <= MAX_THEME_NAME
    with pytest.raises(ValueError):
        register_custom_theme(long_name, _palette())

    register_custom_theme(unique_theme_name(long_name), _palette())
    # …and a collision-avoiding suffix still fits.
    assert len(unique_theme_name(long_name)) <= MAX_THEME_NAME


def test_unique_theme_name_avoids_collisions() -> None:
    assert unique_theme_name("Dark") != "Dark"
    register_custom_theme("Mine", _palette())
    assert unique_theme_name("Mine") == "Mine (2)"
    assert unique_theme_name("") == "My theme"


# ----- normalizing what a user (or a file) supplies ---------------------------


def test_normalize_fills_gaps_and_drops_junk() -> None:
    palette = normalize_palette(
        {"action": "#ABCDEF", "nonsense": "#000000", "text": 42, "border": "red"},
        BUILTIN_THEMES["Light"],
    )

    assert set(palette) == set(BUILTIN_THEMES["Dark"])
    assert palette["action"] == "#abcdef"  # accepted, lowercased
    assert "nonsense" not in palette  # unknown role dropped
    assert palette["text"] == BUILTIN_THEMES["Light"]["text"]  # wrong type
    assert palette["border"] == BUILTIN_THEMES["Light"]["border"]  # not a hex


def test_normalize_keeps_the_derived_roles_in_step() -> None:
    palette = normalize_palette({"action": "#0f0f0f", "success": "#ff0000"})

    # `retheme_widgets` resolves a colour to one role, so these must not drift.
    assert palette["success"] == palette["action"] == "#0f0f0f"
    assert palette["error"] == palette["danger"]


def test_editable_roles_exclude_the_derived_ones() -> None:
    roles = editable_roles()

    assert "success" not in roles and "error" not in roles
    assert set(roles) | set(theme.DERIVED_ROLES) == set(BUILTIN_THEMES["Dark"])


# ----- persistence ------------------------------------------------------------


def test_themes_round_trip_through_the_settings_payload() -> None:
    register_custom_theme(
        "Mine",
        _palette(action="#123456"),
        halftone="#101010",
        outline=2,
        base="Gothic",
    )
    stored = custom_themes_payload()

    load_custom_themes(json.loads(json.dumps(stored)))  # survives a JSON trip

    assert custom_theme_payload("Mine") == stored["Mine"]
    assert THEMES["Mine"]["action"] == "#123456"
    assert HALFTONE_INK["Mine"] == "#101010"
    assert stored["Mine"]["based_on"] == "Gothic"


def test_loading_replaces_the_previous_set() -> None:
    register_custom_theme("Old", _palette())

    load_custom_themes({"New": {"name": "New", "palette": _palette()}})

    assert "Old" not in theme_names()
    assert "New" in theme_names()


def test_a_corrupt_settings_row_never_stops_startup() -> None:
    loaded = load_custom_themes(
        {
            "Fine": {"name": "Fine", "palette": _palette(action="#010203")},
            "NotADict": "nonsense",
            "NoPalette": {"name": "NoPalette"},
            "Builtin": {"name": "Dark", "palette": _palette()},
        }
    )

    assert loaded == ["Fine", "NoPalette"]  # the readable ones
    assert THEMES["Fine"]["action"] == "#010203"
    assert THEMES["NoPalette"] == BUILTIN_THEMES["Dark"]  # gaps filled in
    assert THEMES["Dark"] == BUILTIN_THEMES["Dark"]  # untouched

    assert load_custom_themes(None) == []
    assert load_custom_themes("junk") == []


# ----- the editor dialog ------------------------------------------------------


@pytest.fixture(scope="module")
def _tk_root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    gc.collect()
    r.destroy()


@pytest.fixture
def root(_tk_root):
    from sorter.ui.theme import apply_theme

    apply_theme(_tk_root, theme=DEFAULT_THEME)
    yield _tk_root
    for child in _tk_root.winfo_children():
        child.destroy()


class _StubApp:
    """The slice of MainWindow the editor talks to."""

    def __init__(self) -> None:
        self.theme_name = theme.current_theme()
        self.saved: dict | None = None
        self.refreshed = 0

    def set_theme(self, name: str) -> None:
        self.theme_name = theme.resolve_theme(name)
        set_palette(self.theme_name)

    def save_custom_themes(self) -> None:
        self.saved = custom_themes_payload()

    def refresh_theme_picker(self) -> None:
        self.refreshed += 1


def _editor(root, app):
    from sorter.ui.dialog_theme_editor import ThemeEditorDialog

    dlg = ThemeEditorDialog(root, app=app)
    root.update_idletasks()
    return dlg


def test_editor_starts_as_a_copy_of_the_active_theme(root) -> None:
    set_palette("Gothic")
    dlg = _editor(root, _StubApp())

    assert dlg.base == "Gothic"
    assert dlg.editing is False
    assert dlg.name_var.get() == "My theme"
    assert dlg.values == THEMES["Gothic"]


def test_saving_registers_applies_and_persists(root) -> None:
    set_palette("Dark")
    app = _StubApp()
    dlg = _editor(root, app)
    dlg.name_var.set("Mine")
    dlg._set_color("action", "#ABCDEF")

    dlg._save()

    assert THEMES["Mine"]["action"] == "#abcdef"
    assert app.theme_name == "Mine"  # applied
    assert "Mine" in (app.saved or {})  # persisted
    assert app.refreshed == 1  # picker re-read
    assert theme.PALETTE["action"] == "#abcdef"


def test_saving_without_a_name_is_refused(root, monkeypatch) -> None:
    from sorter.ui import dialog_theme_editor as mod

    warned: list[str] = []
    monkeypatch.setattr(
        mod.messagebox,
        "showwarning",
        lambda *a, **k: warned.append(a[0]),
    )
    app = _StubApp()
    dlg = _editor(root, app)
    dlg.name_var.set("   ")

    dlg._save()

    assert warned and app.saved is None
    assert dlg.winfo_exists()  # dialog stays open to be fixed


def test_a_built_in_name_is_refused(root, monkeypatch) -> None:
    from sorter.ui import dialog_theme_editor as mod

    warned: list[str] = []
    monkeypatch.setattr(
        mod.messagebox,
        "showwarning",
        lambda *a, **k: warned.append(a[0]),
    )
    dlg = _editor(root, _StubApp())
    dlg.name_var.set("Sepia")

    dlg._save()

    assert warned
    assert THEMES["Sepia"] == BUILTIN_THEMES["Sepia"]


def test_editing_a_custom_theme_offers_delete(root) -> None:
    register_custom_theme("Mine", _palette(), base="Sepia")
    set_palette("Mine")
    app = _StubApp()
    dlg = _editor(root, app)

    assert dlg.editing is True
    assert dlg.name_var.get() == "Mine"

    dlg._delete_confirmed()

    assert "Mine" not in theme_names()
    assert app.theme_name == "Sepia"  # falls back to what it was made from
    assert app.saved == {}


def test_renaming_in_the_editor_updates_the_theme_in_place(root) -> None:
    register_custom_theme("Mine", _palette(action="#123456"), base="Sepia")
    set_palette("Mine")
    app = _StubApp()
    dlg = _editor(root, app)

    dlg.name_var.set("Renamed")
    dlg._save()

    assert "Mine" not in theme_names()
    assert THEMES["Renamed"]["action"] == "#123456"
    assert app.theme_name == "Renamed"
    assert list(app.saved or {}) == ["Renamed"]


def test_create_new_keeps_the_theme_it_started_from(root, monkeypatch) -> None:
    register_custom_theme("Mine", _palette(action="#123456"), base="Sepia")
    set_palette("Mine")
    app = _StubApp()
    dlg = _editor(root, app)
    monkeypatch.setattr(type(dlg), "_prompt_name", lambda _self: "Second")

    dlg._set_color("action", "#abcdef")
    dlg._create_new()

    assert THEMES["Mine"]["action"] == "#123456"  # untouched
    assert THEMES["Second"]["action"] == "#abcdef"
    assert app.theme_name == "Second"


def test_create_new_backing_out_changes_nothing(root, monkeypatch) -> None:
    app = _StubApp()
    dlg = _editor(root, app)
    monkeypatch.setattr(type(dlg), "_prompt_name", lambda _self: None)

    dlg._create_new()

    assert app.saved is None
    assert custom_themes_payload() == {}


def test_editor_refuses_an_over_long_name(root, monkeypatch) -> None:
    from sorter.ui import dialog_theme_editor as mod

    warned: list[str] = []
    monkeypatch.setattr(
        mod.messagebox,
        "showwarning",
        lambda *a, **k: warned.append(a[0]),
    )
    app = _StubApp()
    dlg = _editor(root, app)
    # The entry blocks typing past the limit; the check is the backstop.
    assert mod._within_limit("x" * MAX_THEME_NAME) is True
    assert mod._within_limit("x" * (MAX_THEME_NAME + 1)) is False

    dlg.name_var.set("x" * (MAX_THEME_NAME + 1))
    dlg._save()

    assert warned and app.saved is None


def test_typed_hex_updates_the_working_palette(root) -> None:
    dlg = _editor(root, _StubApp())

    dlg._hex_vars["accent"].set("#ff8800")
    assert dlg.values["accent"] == "#ff8800"

    dlg._hex_vars["accent"].set("#nope!!")  # ignored, not crashed on
    assert dlg.values["accent"] == "#ff8800"

    dlg._hex_vars["accent"].set("#ff")  # incomplete: wait for the rest
    assert dlg.values["accent"] == "#ff8800"


def test_export_writes_a_file_import_reads_it_back(root, tmp_path: Path) -> None:
    set_palette("Comic Book")
    app = _StubApp()
    dlg = _editor(root, app)
    dlg.name_var.set("Shared")
    dlg._set_color("danger", "#001122")
    path = tmp_path / "shared.json"

    dlg._write_theme_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["name"] == "Shared"
    assert payload["based_on"] == "Comic Book"
    assert payload["palette"]["danger"] == "#001122"
    assert payload["halftone"] == HALFTONE_INK["Comic Book"]
    assert payload["outline"] == INK_OUTLINE["Comic Book"]

    dlg._read_theme_file(path)

    assert app.theme_name == "Shared"
    assert THEMES["Shared"]["danger"] == "#001122"
    # Importing it a second time keeps both rather than clobbering.
    dlg2 = _editor(root, _StubApp())
    dlg2._read_theme_file(path)
    assert "Shared (2)" in theme_names()


def test_import_rejects_files_that_are_not_themes(root, tmp_path: Path) -> None:
    from sorter.ui.dialog_theme_editor import _describe_bad_theme

    assert _describe_bad_theme([1, 2, 3])
    assert _describe_bad_theme({"name": "x"})  # no palette
    assert _describe_bad_theme({"palette": {}, "version": "abc"})  # unreadable
    assert _describe_bad_theme({"palette": {}, "version": CUSTOM_THEME_VERSION + 1})  # from the future
    assert _describe_bad_theme({"palette": {}}) is None
