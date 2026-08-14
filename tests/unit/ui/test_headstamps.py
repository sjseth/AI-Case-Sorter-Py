"""The per-model headstamp manager, offscreen.

Everything runs against the real SQLite-backed ``Config`` from the shared UI
conftest with ``CASESORTER_DATA_DIR`` pointed at ``tmp_path``: renaming a
headstamp renames real ``{label}__{ticks}.jpg`` files, so an unset data root
would have these tests renaming the developer's own training images.

The bottom section is the end-to-end pass: the dialog on the real window's
event bus, mutating a real model, with the Sort grid re-reading its
assignments off ``run/assignment_changed``.

``notify`` / ``confirm`` / ``ask_text`` are replaced everywhere; nothing modal
opens, and nothing sleeps on a fixed delay — the image-rename thread is waited
on with a bounded poll.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from sorter import paths
from sorter.data import image_store
from sorter.data.config import Config
from sorter.data.repository import HeadstampParentRepo, HeadstampRepo, SettingsRepo
from sorter.ui.dialog_headstamps import (
    NONE_LABEL,
    SUGGESTED_MARK,
    HeadstampManagerDialog,
    clean_name,
    prefix_groups,
)

from .conftest import drain_until, seed_model


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Keep every path this dialog touches inside the test's tmp dir.

    Autouse so it lands before the ``config``/``window`` fixtures build
    anything; the assertion is the proof that it did — this dialog renames
    files under the model's images directory.
    """
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


class _Recorder:
    """Stand-in for ``notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]

    @property
    def texts(self) -> list[str]:
        return [text for _title, text in self.calls]


class _Asker:
    """Stand-in for ``confirm`` — records what it was asked, answers a constant."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.titles: list[str] = []

    def __call__(self, title: str, _text: str) -> bool:
        self.titles.append(title)
        return self.answer


def _fail_text(title: str, label: str, initial: str) -> str | None:
    pytest.fail(f"unexpected text prompt: {title} — {label}")


def _make_dialog(config: Any, model_id: int, *, bus: Any = None) -> Any:
    """A dialog with every modal replaced. Returns ``Any``: the tests read the
    recorders back off ``notify``/``confirm``, which are plain attributes."""
    dialog = HeadstampManagerDialog(None, config, model_id, bus=bus)
    dialog.notify = _Recorder()
    dialog.confirm = _Asker(True)
    dialog.ask_text = _fail_text
    return dialog


def _write_image(path: Path, color: int = 30, size: int = 8) -> None:
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((size, size, 3), color, dtype=np.uint8))


def _seed_images(images_dir: Path, counts: dict[str, int], start_ticks: int = 1) -> None:
    """Write synthetic ``{label}__{ticks}.jpg`` files, image_store's convention."""
    ticks = start_ticks
    for label, n in counts.items():
        for _ in range(n):
            _write_image(images_dir / f"{label}__{ticks}.jpg")
            ticks += 1


def _wait_for_rename(qapp, dialog: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if not dialog.renaming:
            break
        time.sleep(0.005)
    for _ in range(5):  # flush the queued `done` signal the worker just emitted
        qapp.processEvents()


def _rows(dialog: Any) -> list[tuple[str, str, str]]:
    tree = dialog.tree
    items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    return [(item.text(0), item.text(1), item.text(2)) for item in items]


def _names(dialog: Any) -> list[str]:
    return [name for name, _slot, _parent in _rows(dialog)]


def _select(dialog: Any, name: str) -> None:
    for index in range(dialog.tree.topLevelItemCount()):
        item = dialog.tree.topLevelItem(index)
        if item.text(0) == name:
            dialog.tree.setCurrentItem(item)
            return
    pytest.fail(f"no row for {name!r} in {_names(dialog)}")


def _select_parent(dialog: Any, name: str) -> None:
    for index in range(dialog.parent_list.count()):
        if dialog.parent_list.item(index).text() == name:
            dialog.parent_list.setCurrentRow(index)
            return
    pytest.fail(f"no parent row for {name!r}")


def _db_headstamps(config: Config, model_id: int) -> dict[str, tuple[int, int | None]]:
    """``{name: (slot, parent_id)}`` read fresh from the DB."""
    return {h.name: (int(h.slot), h.parent_id) for h in HeadstampRepo(config.db).list_for_model(model_id)}


def _db_parents(config: Config, model_id: int) -> dict[str, int]:
    return {p.name: p.id for p in HeadstampParentRepo(config.db).list_for_model(model_id)}


def _fresh(config: Config) -> Config:
    """A second Config on the same DB — proves a mutation actually persisted."""
    return Config(config.db).load()


# ----- the name rules ---------------------------------------------------------


def test_clean_name_matches_the_tk_rule() -> None:
    assert clean_name(' WIN"9mm/?* ') == "WIN-9mm---"
    assert clean_name("FED__223") == "FED_223"  # `__` is the filename separator
    assert clean_name("   ") == ""


def test_prefix_groups_is_the_leading_alpha_run() -> None:
    groups = prefix_groups(sorted(["FC 223", "fc 9mm", "RP 45", "WIN", "9mm only"]))

    # Upper-cased prefix, groups of two or more only, non-alpha starts skipped.
    assert groups == {"FC": ["FC 223", "fc 9mm"]}


# ----- listing ---------------------------------------------------------------


def test_lists_every_headstamp_with_its_slot_and_parent(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1, "FED 45": 0})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    headstamps = {h.name: h for h in HeadstampRepo(config.db).list_for_model(model_id)}
    HeadstampRepo(config.db).set_parent(headstamps["WIN 9mm"].id, parent_id)

    dialog = _make_dialog(config, model_id)

    assert _rows(dialog) == [("FED 45", "—", NONE_LABEL), ("WIN 9mm", "1", "WIN")]
    assert [dialog.parent_list.item(i).text() for i in range(dialog.parent_list.count())] == ["WIN"]


def test_lists_only_this_models_headstamps(qapp, config) -> None:
    mine = seed_model(config, {"WIN 9mm": 1}, name="Mine")
    other = seed_model(config, {"Someone else's": 2}, name="Other")

    assert _names(_make_dialog(config, mine)) == ["WIN 9mm"]
    assert _names(_make_dialog(config, other)) == ["Someone else's"]


# ----- add / delete ----------------------------------------------------------


def test_add_round_trips_through_a_fresh_config(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)

    dialog.name_edit.setText("  FED 45  ")
    dialog.add_button.click()

    assert sorted(_db_headstamps(config, model_id)) == ["FED 45", "WIN 9mm"]
    assert _names(dialog) == ["FED 45", "WIN 9mm"]
    assert dialog.name_edit.text() == ""
    assert dialog.selected_headstamp().name == "FED 45"  # the new row is selected


def test_add_cleans_the_name_after_confirming(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)

    dialog.name_edit.setText("FED/45")
    dialog.add_button.click()

    assert dialog.confirm.titles == ["Clean name"]
    assert "FED-45" in _db_headstamps(config, model_id)


def test_add_is_abandoned_when_the_cleanup_is_declined(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)
    dialog.confirm = _Asker(False)

    dialog.name_edit.setText("FED/45")
    dialog.add_button.click()

    assert list(_db_headstamps(config, model_id)) == ["WIN 9mm"]


def test_duplicate_add_is_refused_case_insensitively(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)

    dialog.name_edit.setText("win 9MM")
    dialog.add_button.click()

    assert list(_db_headstamps(config, model_id)) == ["WIN 9mm"]
    assert dialog.notify.titles == ["Duplicate"]


def test_delete_removes_the_row_after_confirming(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1, "FED 45": 2})
    dialog = _make_dialog(config, model_id)
    _select(dialog, "FED 45")

    dialog.delete_button.click()

    assert list(_db_headstamps(config, model_id)) == ["WIN 9mm"]
    assert _names(dialog) == ["WIN 9mm"]


def test_delete_is_abandoned_when_declined(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)
    dialog.confirm = _Asker(False)
    _select(dialog, "WIN 9mm")

    dialog.delete_button.click()

    assert list(_db_headstamps(config, model_id)) == ["WIN 9mm"]


# ----- rename (DB + the training images on disk) ------------------------------


def test_rename_updates_the_db_and_renames_the_training_images(qapp, config, tmp_path) -> None:
    model_id = seed_model(config, {"WIN 9mm": 3, "FED 45": 0})
    images_dir = paths.model_images_dir(model_id)
    _seed_images(images_dir, {"WIN 9mm": 3, "FED 45": 2})
    dialog = _make_dialog(config, model_id)
    dialog.ask_text = lambda _title, _label, _initial: "WIN 9x19"
    _select(dialog, "WIN 9mm")

    dialog.rename_button.click()
    _wait_for_rename(qapp, dialog)

    # The DB row keeps its id and its slot; only the name changed.
    assert _db_headstamps(config, model_id)["WIN 9x19"] == (3, None)
    assert "WIN 9mm" not in _db_headstamps(config, model_id)
    assert _fresh(config).slot_for_headstamp("WIN 9x19") == 3
    # Every image carries the new label; the other class is untouched.
    on_disk = sorted(image_store.parse_headstamp(p) for p in image_store.list_images(images_dir))
    assert on_disk == ["FED 45", "FED 45", "WIN 9x19", "WIN 9x19", "WIN 9x19"]
    assert "3 training images renamed" in dialog.status_label.text()


def test_rename_leaves_an_image_whose_target_name_is_taken(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    images_dir = paths.model_images_dir(model_id)
    _seed_images(images_dir, {"WIN 9mm": 1}, start_ticks=7)
    _write_image(images_dir / "WIN 9x19__7.jpg")  # same ticks, already there
    dialog = _make_dialog(config, model_id)
    dialog.ask_text = lambda _title, _label, _initial: "WIN 9x19"
    _select(dialog, "WIN 9mm")

    dialog.rename_button.click()
    _wait_for_rename(qapp, dialog)

    # image_store.reclassify never overwrites, so the odd one out stays put.
    assert {p.name for p in image_store.list_images(images_dir)} == {"WIN 9mm__7.jpg", "WIN 9x19__7.jpg"}
    assert "0 training images renamed" in dialog.status_label.text()


def test_rename_onto_an_existing_headstamp_is_refused(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1, "FED 45": 2})
    images_dir = paths.model_images_dir(model_id)
    _seed_images(images_dir, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)
    dialog.ask_text = lambda _title, _label, _initial: "fed 45"  # case-insensitive clash
    _select(dialog, "WIN 9mm")

    dialog.rename_button.click()
    _wait_for_rename(qapp, dialog)

    assert sorted(_db_headstamps(config, model_id)) == ["FED 45", "WIN 9mm"]
    assert dialog.notify.titles == ["Duplicate"]
    assert [p.name for p in image_store.list_images(images_dir)] == ["WIN 9mm__1.jpg"]


def test_rename_resyncs_the_active_sorting_template(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 3})
    dialog = _make_dialog(config, model_id)
    dialog.ask_text = lambda _title, _label, _initial: "WIN 9x19"
    _select(dialog, "WIN 9mm")

    dialog.rename_button.click()
    _wait_for_rename(qapp, dialog)

    # Templates are name-keyed: the live layout and the active template must
    # not disagree about what the slot-3 headstamp is called.
    template = _fresh(config).active_slot_template("standard")
    assert template.assignments["headstamps"] == {"WIN 9x19": 3}


# ----- slots -----------------------------------------------------------------


def test_slot_edit_persists_and_shows_in_the_row(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 0})
    dialog = _make_dialog(config, model_id)
    _select(dialog, "WIN 9mm")

    dialog.slot_spin.setValue(4)

    assert _fresh(config).slot_for_headstamp("WIN 9mm") == 4
    assert _rows(dialog) == [("WIN 9mm", "4", NONE_LABEL)]
    assert _fresh(config).active_slot_template("standard").assignments["headstamps"] == {"WIN 9mm": 4}


def test_slot_range_follows_the_configured_slot_quantity(qapp, config) -> None:
    config.serial["slot_quantity"] = 12
    model_id = seed_model(config, {"WIN 9mm": 0})

    dialog = _make_dialog(config, model_id)

    assert dialog.slot_spin.maximum() == 12


def test_slot_edit_of_a_non_active_model_leaves_the_active_template_alone(qapp, config) -> None:
    active = seed_model(config, {"Active hs": 5}, name="Active")
    other = seed_model(config, {"Other hs": 0}, name="Other")
    # seed_model activates whatever it created last; put the first one back.
    SettingsRepo(config.db).set_active_model_id(active)
    before = _fresh(config).active_slot_template("standard").assignments
    dialog = _make_dialog(config, other)
    _select(dialog, "Other hs")

    dialog.slot_spin.setValue(2)

    assert _db_headstamps(config, other)["Other hs"] == (2, None)
    # The template API is scoped to the *active* model, so editing another
    # model's slots must not rewrite it.
    assert _fresh(config).active_slot_template("standard").assignments == before


# ----- parents ---------------------------------------------------------------


def test_parent_add_and_duplicate_refusal(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    dialog = _make_dialog(config, model_id)

    dialog.parent_edit.setText("WIN")
    dialog.parent_add_button.click()
    dialog.parent_edit.setText("win")
    dialog.parent_add_button.click()

    assert list(_db_parents(config, model_id)) == ["WIN"]
    assert dialog.notify.titles == ["Duplicate"]
    assert dialog.parent_edit.text() == "win"  # a refused add keeps what was typed


def test_parent_assignment_stages_until_save(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    dialog = _make_dialog(config, model_id)
    _select(dialog, "WIN 9mm")

    dialog.parent_combo.setCurrentIndex(dialog.parent_combo.findData(parent_id))

    # Staged only: the row shows it, the DB does not.
    assert _rows(dialog) == [("WIN 9mm", "1", "WIN")]
    assert _db_headstamps(config, model_id)["WIN 9mm"][1] is None

    dialog.save_button.click()

    assert _db_headstamps(config, model_id)["WIN 9mm"][1] == parent_id
    assert _fresh(config).parent_for_headstamp("WIN 9mm") == "WIN"
    assert dialog.status_label.text() == "Saved."


def test_parent_rename_shows_through_to_the_rows(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    HeadstampRepo(config.db).set_parent(
        next(h.id for h in HeadstampRepo(config.db).list_for_model(model_id)), parent_id
    )
    dialog = _make_dialog(config, model_id)
    dialog.ask_text = lambda _title, _label, _initial: "Winchester"
    _select_parent(dialog, "WIN")

    dialog.parent_rename_button.click()

    assert list(_db_parents(config, model_id)) == ["Winchester"]
    assert _rows(dialog) == [("WIN 9mm", "1", "Winchester")]


def test_parent_delete_unlinks_children_and_drops_staged_assignments(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1, "WIN 45": 2})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    repo = HeadstampRepo(config.db)
    for headstamp in repo.list_for_model(model_id):
        repo.set_parent(headstamp.id, parent_id)
    dialog = _make_dialog(config, model_id)
    _select_parent(dialog, "WIN")

    dialog.parent_delete_button.click()

    assert _db_parents(config, model_id) == {}
    assert all(parent is None for _slot, parent in _db_headstamps(config, model_id).values())
    assert {row[2] for row in _rows(dialog)} == {NONE_LABEL}

    dialog.save_button.click()  # the staged copies went too, so Save can't resurrect it

    assert all(parent is None for _slot, parent in _db_headstamps(config, model_id).values())


def test_save_clears_an_assignment_whose_parent_vanished(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    dialog = _make_dialog(config, model_id)
    _select(dialog, "WIN 9mm")
    dialog.parent_combo.setCurrentIndex(dialog.parent_combo.findData(parent_id))
    # Deleted behind the dialog's back (another window, an import).
    HeadstampParentRepo(config.db).delete(parent_id)
    dialog.refresh()

    dialog.save_button.click()

    assert _db_headstamps(config, model_id)["WIN 9mm"][1] is None


# ----- auto-suggest ----------------------------------------------------------


def test_auto_suggest_groups_by_prefix_and_stages_the_result(qapp, config) -> None:
    model_id = seed_model(config, {"FC 223": 1, "FC 9mm": 2, "RP 45": 3, "9mm mixed": 0})
    dialog = _make_dialog(config, model_id)

    dialog.suggest_button.click()

    # A parent per qualifying prefix, created immediately; assignments staged.
    assert list(_db_parents(config, model_id)) == ["FC"]
    assert all(parent is None for _slot, parent in _db_headstamps(config, model_id).values())
    suggested = {name: parent for name, _slot, parent in _rows(dialog)}
    assert suggested["FC 223"] == "FC" + SUGGESTED_MARK
    assert suggested["FC 9mm"] == "FC" + SUGGESTED_MARK
    assert suggested["RP 45"] == NONE_LABEL  # only one member: no group
    assert suggested["9mm mixed"] == NONE_LABEL  # no leading alpha run
    assert "2 suggestions applied" in dialog.status_label.text()

    dialog.save_button.click()

    parent_id = _db_parents(config, model_id)["FC"]
    written = _db_headstamps(config, model_id)
    assert written["FC 223"][1] == parent_id and written["FC 9mm"][1] == parent_id
    assert "suggestion" not in dialog.status_label.text()


def test_auto_suggest_never_overrides_an_existing_assignment(qapp, config) -> None:
    model_id = seed_model(config, {"FC 223": 1, "FC 9mm": 2})
    mine = HeadstampParentRepo(config.db).add(model_id, "Mine").id
    repo = HeadstampRepo(config.db)
    kept = next(h for h in repo.list_for_model(model_id) if h.name == "FC 223")
    repo.set_parent(kept.id, mine)
    dialog = _make_dialog(config, model_id)

    dialog.suggest_button.click()

    rows = {name: parent for name, _slot, parent in _rows(dialog)}
    assert rows["FC 223"] == "Mine"
    assert rows["FC 9mm"] == "FC" + SUGGESTED_MARK
    assert "1 suggestion applied" in dialog.status_label.text()


def test_auto_suggest_reports_when_it_has_nothing_to_do(qapp, config) -> None:
    single = seed_model(config, {"WIN 9mm": 1}, name="Single")
    dialog = _make_dialog(config, single)

    dialog.suggest_button.click()

    assert dialog.notify.texts == ["Not enough headstamps to suggest parents."]

    numeric = seed_model(config, {"9mm": 1, "45 ACP": 2}, name="Numeric")
    other = _make_dialog(config, numeric)

    other.suggest_button.click()

    assert other.notify.texts == ["No common prefixes found to suggest parent groups."]


def test_auto_suggest_says_so_when_everything_is_already_assigned(qapp, config) -> None:
    model_id = seed_model(config, {"FC 223": 1, "FC 9mm": 2})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "Mine").id
    repo = HeadstampRepo(config.db)
    for headstamp in repo.list_for_model(model_id):
        repo.set_parent(headstamp.id, parent_id)
    dialog = _make_dialog(config, model_id)

    dialog.suggest_button.click()

    assert dialog.notify.texts == ["All headstamps already have parents assigned."]
    assert dialog.status_label.text() == ""


# ----- the unsaved-changes guard ---------------------------------------------


def test_close_with_staged_assignments_asks_first(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    dialog = _make_dialog(config, model_id)
    dialog.show()
    _select(dialog, "WIN 9mm")
    dialog.parent_combo.setCurrentIndex(dialog.parent_combo.findData(parent_id))

    dialog.confirm = _Asker(False)
    dialog.close()

    assert dialog.confirm.titles == ["Unsaved changes"]
    assert dialog.isVisible()  # refused: the dialog stays open

    dialog.confirm = _Asker(True)
    dialog.close()

    assert not dialog.isVisible()
    assert _db_headstamps(config, model_id)["WIN 9mm"][1] is None  # discarded, not written


def test_close_after_saving_does_not_ask(qapp, config) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    parent_id = HeadstampParentRepo(config.db).add(model_id, "WIN").id
    dialog = _make_dialog(config, model_id)
    dialog.show()
    _select(dialog, "WIN 9mm")
    dialog.parent_combo.setCurrentIndex(dialog.parent_combo.findData(parent_id))
    dialog.save_button.click()

    dialog.confirm = lambda title, text: pytest.fail(f"unexpected prompt: {title}")
    dialog.close()

    assert not dialog.isVisible()


def test_slot_edits_alone_never_trigger_the_guard(qapp, config) -> None:
    """Slots write through immediately, so there is nothing unsaved to warn about."""
    model_id = seed_model(config, {"WIN 9mm": 0})
    dialog = _make_dialog(config, model_id)
    dialog.show()
    _select(dialog, "WIN 9mm")
    dialog.slot_spin.setValue(2)

    dialog.confirm = lambda title, text: pytest.fail(f"unexpected prompt: {title}")
    dialog.close()

    assert not dialog.isVisible()


# ----- end to end: the real bus and the real Sort grid -----------------------


def test_mutations_reach_the_sort_grid_through_the_bus(qapp, config, window) -> None:
    model_id = seed_model(config, {"WIN 9mm": 0})
    dialog = _make_dialog(config, model_id, bus=window.bus)
    _select(dialog, "WIN 9mm")

    dialog.slot_spin.setValue(2)

    # No shortcut: the window's own `run/assignment_changed` subscription is
    # what repaints the grid.
    assert drain_until(window, lambda: window.slot_grid.cards[2].names_label.text() == "WIN 9mm")

    dialog.name_edit.setText("FED 45")
    dialog.add_button.click()
    _select(dialog, "FED 45")
    dialog.slot_spin.setValue(2)

    assert drain_until(window, lambda: window.slot_grid.cards[2].names_label.text() == "FED 45, WIN 9mm")


def test_rename_repaints_the_grid_and_moves_the_images(qapp, config, window) -> None:
    model_id = seed_model(config, {"WIN 9mm": 2})
    images_dir = paths.model_images_dir(model_id)
    _seed_images(images_dir, {"WIN 9mm": 2})
    dialog = _make_dialog(config, model_id, bus=window.bus)
    dialog.ask_text = lambda _title, _label, _initial: "WIN 9x19"
    _select(dialog, "WIN 9mm")

    dialog.rename_button.click()
    _wait_for_rename(qapp, dialog)

    assert drain_until(window, lambda: window.slot_grid.cards[2].names_label.text() == "WIN 9x19")
    assert {p.name for p in image_store.list_images(images_dir)} == {"WIN 9x19__1.jpg", "WIN 9x19__2.jpg"}


def test_closing_after_a_write_tells_the_app_the_headstamp_set_changed(qapp, config, window) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    posted: list[Any] = []
    window.bus.subscribe("mode/changed", posted.append)
    dialog = _make_dialog(config, model_id, bus=window.bus)
    dialog.show()
    dialog.name_edit.setText("FED 45")
    dialog.add_button.click()

    dialog.close()

    assert drain_until(window, lambda: posted == [{"reason": "headstamps", "model_id": model_id}])


def test_closing_without_a_write_stays_quiet(qapp, config, window) -> None:
    model_id = seed_model(config, {"WIN 9mm": 1})
    posted: list[Any] = []
    window.bus.subscribe("mode/changed", posted.append)
    dialog = _make_dialog(config, model_id, bus=window.bus)
    dialog.show()

    dialog.close()

    window.bus.drain()
    assert posted == []
