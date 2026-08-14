"""The Qt theme editor: roles, preview, the save/rename/import rules.

Two levels, both here:

* **Unit** — the dialog's own behaviour (role list, preview restyle, derived
  roles, name rules, JSON files), offscreen, every modal hook replaced.
* **Integration** — the end-to-end persistence flow through the real layers: a
  theme saved in the dialog lands in the ``ui.custom_themes`` settings row, and
  a *second* ``QtMainWindow`` built on the same database re-registers it through
  ``load_custom_themes`` at construction — which is what "it's still there next
  launch" actually means.

Custom themes are module state in ``sorter.ui.palettes``; ``_clean_registry``
restores whatever was registered before each test so none leaks into the next.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from sorter import paths
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.repository import SettingsRepo
from sorter.ui.dialog_theme_editor import (
    ThemeEditorDialog,
    describe_bad_theme,
    grouped_roles,
    preview_stylesheet,
)
from sorter.ui.palettes import (
    BUILTIN_THEMES,
    CUSTOM_THEME_VERSION,
    DERIVED_ROLES,
    HALFTONE_INK,
    INK_OUTLINE,
    MAX_THEME_NAME,
    SETTING_CUSTOM_THEMES,
    THEMES,
    custom_theme_payload,
    custom_themes_payload,
    editable_roles,
    load_custom_themes,
    normalize_palette,
    register_custom_theme,
    theme_names,
)


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Keep every path this suite touches inside the test's tmp dir."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Custom themes are module state — no test may leak one into the next."""
    original = custom_themes_payload()
    yield
    load_custom_themes(original)


class _Recorder:
    """Stand-in for ``notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]


def _notified(dialog: ThemeEditorDialog) -> list[str]:
    """The titles the dialog warned with — its ``notify`` hook is a _Recorder."""
    recorder = dialog.notify
    assert isinstance(recorder, _Recorder)
    return recorder.titles


def _editor(window: Any, *, confirm: bool = True) -> ThemeEditorDialog:
    """The dialog with every native dialog replaced by a hook."""
    dialog = ThemeEditorDialog(window, window)
    dialog.notify = _Recorder()
    dialog.confirm = lambda _title, _text: confirm
    dialog.pick_color = lambda _title, _initial: None
    dialog.ask_name = lambda _initial: None
    dialog.ask_open_path = lambda: None
    dialog.ask_save_path = lambda _default: None
    return dialog


def _palette(**overrides: str) -> dict[str, str]:
    return normalize_palette(overrides, BUILTIN_THEMES["Dark"])


# ----- roles ------------------------------------------------------------------


def test_the_role_rows_are_exactly_the_editable_palette_keys(window) -> None:
    dialog = _editor(window)

    listed = {role for _group, rows in grouped_roles() for role, _label in rows}

    assert listed == set(editable_roles())
    assert set(dialog._swatches) == listed
    # Nothing in the palette is unreachable: what isn't editable is derived.
    assert listed | set(DERIVED_ROLES) == set(BUILTIN_THEMES["Dark"])
    assert "success" not in listed and "error" not in listed


def test_the_editor_starts_from_the_theme_the_window_shows(window) -> None:
    window.set_theme("Gothic")

    dialog = _editor(window)

    assert dialog.base == "Gothic"
    assert dialog.editing is False  # a built-in is never written back to
    assert dialog.values == THEMES["Gothic"]
    assert dialog.name_edit.text() == "My theme"


# ----- preview ----------------------------------------------------------------


def test_every_role_edit_reaches_the_preview_stylesheet(window) -> None:
    dialog = _editor(window)

    for index, role in enumerate(editable_roles()):
        color = f"#{index:02x}00ff"
        dialog.set_color(role, color)
        assert color in dialog.preview.styleSheet(), f"{role} is not visible in the preview"


def test_the_preview_uses_the_apps_own_stylesheet(window) -> None:
    dialog = _editor(window)
    dialog.set_color("bg_window", "#123456")

    qss = dialog.preview.styleSheet()

    assert qss.startswith(preview_stylesheet(dialog.values)[:40])
    assert qss.count("{") == qss.count("}")
    assert "{c[" not in qss and "None" not in qss


# ----- the derived roles ------------------------------------------------------


def test_success_and_error_stay_locked_to_action_and_danger(window) -> None:
    dialog = _editor(window)

    dialog.set_color("action", "#0f0f0f")
    dialog.set_color("danger", "#010203")

    assert dialog.values["success"] == "#0f0f0f"
    assert dialog.values["error"] == "#010203"
    assert dialog.payload("Mine")["palette"]["success"] == "#0f0f0f"

    dialog.name_edit.setText("Mine")
    dialog.save()

    assert THEMES["Mine"]["success"] == THEMES["Mine"]["action"] == "#0f0f0f"
    assert THEMES["Mine"]["error"] == THEMES["Mine"]["danger"] == "#010203"


def test_save_and_apply_keeps_the_editor_open_and_switches_to_editing(window) -> None:
    """Seth: saving is the iteration step, not the exit — the dialog stays up,
    and a first save of a built-in-based theme becomes an edit session on the
    new theme."""
    dialog = _editor(window)
    dialog.name_edit.setText("Mine")

    dialog.save()

    assert dialog.result() == 0  # neither accepted nor rejected — still open
    assert dialog.editing is True
    assert dialog.base == "Mine"
    assert "Mine" in dialog.mode_hint.text()

    # A second save writes back to the same theme without a replace-confirm.
    dialog.set_color("action", "#123456")
    dialog.save()
    assert THEMES["Mine"]["action"] == "#123456"


def test_a_typed_hex_updates_the_working_palette(window) -> None:
    dialog = _editor(window)

    dialog._hex_edits["accent"].setText("#ff8800")
    assert dialog.values["accent"] == "#ff8800"

    dialog._hex_edits["accent"].setText("#nope!!")  # ignored, not crashed on
    assert dialog.values["accent"] == "#ff8800"

    dialog._hex_edits["accent"].setText("#ff")  # incomplete: wait for the rest
    assert dialog.values["accent"] == "#ff8800"


# ----- saving -----------------------------------------------------------------


def test_saving_registers_applies_and_persists(window, config) -> None:
    dialog = _editor(window)
    dialog.name_edit.setText("Mine")
    dialog.set_color("action", "#abcdef")

    dialog.save()

    assert THEMES["Mine"]["action"] == "#abcdef"
    assert window.theme_name == "Mine"  # applied
    assert "#abcdef" in window.styleSheet()
    assert window.theme_combo.currentText() == "Mine"  # picker re-read
    stored = SettingsRepo(config.db).get(SETTING_CUSTOM_THEMES)
    assert stored["Mine"]["palette"]["action"] == "#abcdef"  # persisted


def test_a_saved_theme_is_back_on_the_next_launch(window, config, window_factory, tmp_path: Path) -> None:
    """End to end: dialog -> settings row -> a fresh window's load_custom_themes."""
    dialog = _editor(window)
    dialog.name_edit.setText("Mine")
    dialog.set_color("action", "#abcdef")
    dialog.save()

    # A new process starts with nothing registered; the second window is the
    # one that has to find the theme in the database.
    load_custom_themes(None)
    assert "Mine" not in THEMES

    db = Database(tmp_path / "casesorter.db")
    db.ensure_initialized()
    again = Config(db).load()
    second = window_factory(again)

    assert THEMES["Mine"]["action"] == "#abcdef"
    assert "Mine" in [second.theme_combo.itemText(i) for i in range(second.theme_combo.count())]


def test_editing_a_built_in_is_refused_and_forces_a_new_theme(window) -> None:
    dialog = _editor(window)
    dialog.name_edit.setText("Dark")

    dialog.save()

    assert _notified(dialog) == ["Built-in theme"]
    assert THEMES["Dark"] == BUILTIN_THEMES["Dark"]
    assert custom_themes_payload() == {}

    dialog.name_edit.setText("Mine")
    dialog.set_color("bg_window", "#010101")
    dialog.save()

    assert THEMES["Mine"]["bg_window"] == "#010101"
    assert THEMES["Dark"] == BUILTIN_THEMES["Dark"]  # still untouched


def test_renaming_keeps_the_theme_its_options_and_its_place(window, config) -> None:
    register_custom_theme("Mine", _palette(action="#123456"), halftone="#101010", outline=2, base="Sepia")
    window.set_theme("Mine")
    slot = theme_names().index("Mine")
    dialog = _editor(window)

    assert dialog.editing is True
    assert dialog.halftone_check.isChecked() and dialog.outline_check.isChecked()

    dialog.name_edit.setText("Renamed")
    dialog.save()

    assert "Mine" not in theme_names()
    assert THEMES["Renamed"]["action"] == "#123456"
    # A rename moves the theme: the options and the origin come along, and it
    # keeps the slot it had in the picker.
    assert HALFTONE_INK["Renamed"] == "#101010"
    assert INK_OUTLINE["Renamed"] == 2
    assert custom_theme_payload("Renamed")["based_on"] == "Sepia"
    assert theme_names().index("Renamed") == slot
    assert window.theme_name == "Renamed"
    assert list(SettingsRepo(config.db).get(SETTING_CUSTOM_THEMES)) == ["Renamed"]


def test_create_new_always_leaves_the_theme_it_started_from(window) -> None:
    register_custom_theme("Mine", _palette(action="#123456"), base="Sepia")
    window.set_theme("Mine")
    dialog = _editor(window)
    dialog.ask_name = lambda _initial: "Second"

    dialog.set_color("action", "#abcdef")
    dialog.create_new()

    assert THEMES["Mine"]["action"] == "#123456"  # untouched
    assert THEMES["Second"]["action"] == "#abcdef"
    assert window.theme_name == "Second"


def test_backing_out_of_create_new_changes_nothing(window) -> None:
    dialog = _editor(window)

    dialog.create_new()  # the hook returns None

    assert custom_themes_payload() == {}


def test_deleting_falls_back_to_the_theme_it_was_made_from(window, config) -> None:
    register_custom_theme("Mine", _palette(), base="Sepia")
    window.set_theme("Mine")
    dialog = _editor(window)

    dialog.delete_confirmed()

    assert "Mine" not in theme_names()
    assert window.theme_name == "Sepia"
    assert SettingsRepo(config.db).get(SETTING_CUSTOM_THEMES) == {}


# ----- names ------------------------------------------------------------------


def test_theme_names_are_capped_to_fit_the_picker(window) -> None:
    dialog = _editor(window)

    assert dialog.name_edit.maxLength() == MAX_THEME_NAME
    # The field blocks typing past the limit; a name from the Create-new prompt
    # is the path that needs the backstop.
    dialog.ask_name = lambda _initial: "x" * (MAX_THEME_NAME + 1)
    dialog.create_new()

    assert _notified(dialog) == ["Name too long"]
    assert custom_themes_payload() == {}


def test_a_theme_without_a_name_is_refused(window) -> None:
    dialog = _editor(window)
    dialog.name_edit.setText("   ")

    dialog.save()

    assert _notified(dialog) == ["Name needed"]
    assert custom_themes_payload() == {}


# ----- files ------------------------------------------------------------------


def test_export_then_import_round_trips_the_options(window, tmp_path: Path) -> None:
    window.set_theme("Comic Book")  # the built-in that carries both options
    dialog = _editor(window)
    dialog.name_edit.setText("Shared")
    dialog.set_color("danger", "#001122")
    path = tmp_path / "shared.json"

    payload = json.loads(json.dumps(dialog.write_theme_file(path)))

    assert payload == json.loads(path.read_text(encoding="utf-8"))
    assert payload["name"] == "Shared"
    assert payload["based_on"] == "Comic Book"
    assert payload["palette"]["danger"] == "#001122"
    # Flat under Qt, but the options travel so the classic UI still draws them.
    assert payload["halftone"] == HALFTONE_INK["Comic Book"]
    assert payload["outline"] == INK_OUTLINE["Comic Book"]

    dialog.read_theme_file(path)

    assert THEMES["Shared"]["danger"] == "#001122"
    assert HALFTONE_INK["Shared"] == HALFTONE_INK["Comic Book"]
    assert INK_OUTLINE["Shared"] == payload["outline"]
    assert window.theme_name == "Shared"

    # Importing the same file again keeps both rather than clobbering.
    _editor(window).read_theme_file(path)
    assert "Shared (2)" in theme_names()


def test_import_rejects_a_file_that_is_not_a_theme(window, tmp_path: Path) -> None:
    dialog = _editor(window)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "Bad"}), encoding="utf-8")

    with pytest.raises(ValueError):
        dialog.read_theme_file(path)

    assert "Bad" not in theme_names()
    assert describe_bad_theme([1, 2, 3])
    assert describe_bad_theme({"name": "x"})  # no palette
    assert describe_bad_theme({"palette": {}, "version": "abc"})  # unreadable
    assert describe_bad_theme({"palette": {}, "version": CUSTOM_THEME_VERSION + 1})  # from the future
    assert describe_bad_theme({"palette": {}}) is None


def test_import_normalizes_a_partial_or_wrong_palette(window, tmp_path: Path) -> None:
    dialog = _editor(window)
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps(
            {
                "name": "Partial",
                "palette": {"action": "#ABCDEF", "bg_window": "notacolour", "nonsense": "#000000"},
                "halftone": "not a colour either",
                "outline": 99,
            }
        ),
        encoding="utf-8",
    )

    dialog.read_theme_file(path)

    registered = THEMES["Partial"]
    assert set(registered) == set(BUILTIN_THEMES["Dark"])  # gaps filled in
    assert registered["action"] == "#abcdef"
    assert registered["bg_window"] == BUILTIN_THEMES["Dark"]["bg_window"]  # junk ignored
    assert "nonsense" not in registered
    assert "Partial" not in HALFTONE_INK  # not a colour, so no dots
    assert INK_OUTLINE["Partial"] == 4  # clamped, not trusted


# ----- the halftone option ----------------------------------------------------


def test_picking_a_halftone_ink_turns_the_option_on(window) -> None:
    dialog = _editor(window)
    assert dialog.halftone_check.isChecked() is False
    dialog.pick_color = lambda _title, _initial: "#101010"

    dialog.pick_halftone()

    assert dialog.halftone_check.isChecked() is True
    assert dialog.payload("Mine")["halftone"] == "#101010"

    dialog.outline_check.setChecked(True)
    dialog.name_edit.setText("Inky")
    dialog.save()

    assert HALFTONE_INK["Inky"] == "#101010"
    assert INK_OUTLINE["Inky"] == 2
