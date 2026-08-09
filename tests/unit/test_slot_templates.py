"""Sorting templates: named slot layouts per model + run mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from sorter.config import DEFAULT_SLOT_TEMPLATE_NAME, Config
from sorter.db import Database
from sorter.models import Model
from sorter.repository import HeadstampParentRepo, ModelRepo, SettingsRepo, SlotTemplateRepo


def _new_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "templates.db")
    db.ensure_initialized()
    return db


def _activate_seeded_model(db: Database) -> int:
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    return seed.id


def _cfg(tmp_path: Path) -> Config:
    db = _new_db(tmp_path)
    _activate_seeded_model(db)
    cfg = Config(db).load()
    cfg.add_headstamp("WIN")
    cfg.add_headstamp("FC")
    cfg.add_headstamp("CBC")
    return cfg


def _slots(cfg: Config) -> dict[str, int]:
    return {h["name"]: int(h["slot"]) for h in cfg.headstamps}


# ----- seeding ---------------------------------------------------------------


def test_default_template_seeded_from_existing_assignments(tmp_path: Path) -> None:
    """Upgrading users keep their layout: it becomes the "Default" template."""
    cfg = _cfg(tmp_path)
    cfg.set_headstamp_slot("WIN", 3)

    templates = cfg.list_slot_templates()
    assert [t.name for t in templates] == [DEFAULT_SLOT_TEMPLATE_NAME]
    assert cfg.active_slot_template().assignments["headstamps"] == {"WIN": 3}


def test_assignment_changes_sync_into_the_active_template(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    active = cfg.active_slot_template()
    cfg.set_headstamp_slot("FC", 2)

    stored = SlotTemplateRepo(cfg.db).get(active.id)
    assert stored is not None
    assert stored.assignments["headstamps"] == {"FC": 2}


# ----- create / switch -------------------------------------------------------


def test_create_copy_keeps_layout_and_switching_restores_each(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.set_headstamp_slot("WIN", 3)
    default = cfg.active_slot_template()

    match = cfg.create_slot_template("Match prep", copy_current=True)
    assert _slots(cfg)["WIN"] == 3  # copied layout stays live
    cfg.set_headstamp_slot("WIN", 5)  # diverge from Default

    cfg.activate_slot_template(default.id)
    assert _slots(cfg)["WIN"] == 3

    cfg.activate_slot_template(match.id)
    assert _slots(cfg)["WIN"] == 5


def test_create_fresh_clears_the_live_layout(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.set_headstamp_slot("WIN", 3)
    cfg.set_headstamp_slot("FC", 4)
    default = cfg.active_slot_template()

    cfg.create_slot_template("Blank", copy_current=False)
    assert _slots(cfg) == {"WIN": 0, "FC": 0, "CBC": 0}

    # ...and the template we came from kept its own assignments.
    cfg.activate_slot_template(default.id)
    assert _slots(cfg) == {"WIN": 3, "FC": 4, "CBC": 0}


def test_applying_a_template_clears_slots_it_does_not_mention(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    default = cfg.active_slot_template()
    other = cfg.create_slot_template("Other", copy_current=False)
    cfg.set_headstamp_slot("CBC", 6)

    cfg.activate_slot_template(default.id)
    assert _slots(cfg)["CBC"] == 0
    cfg.activate_slot_template(other.id)
    assert _slots(cfg)["CBC"] == 6


def test_parent_slots_travel_with_the_template(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    mid = cfg.settings.get_active_model_id()
    assert mid is not None
    parent = HeadstampParentRepo(cfg.db).add(mid, "Federal")
    cfg.set_parent_slot(parent.id, 4)
    default = cfg.active_slot_template()

    cfg.create_slot_template("No parents", copy_current=False)
    assert cfg.parents_with_slots()[0]["slot"] == 0

    cfg.activate_slot_template(default.id)
    assert cfg.parents_with_slots()[0]["slot"] == 4


def test_duplicate_names_are_rejected_case_insensitively(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.create_slot_template("Range brass")
    with pytest.raises(ValueError):
        cfg.create_slot_template("range BRASS")
    with pytest.raises(ValueError):
        cfg.create_slot_template("   ")


# ----- rename / delete -------------------------------------------------------


def test_rename_and_duplicate_rename(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    default = cfg.active_slot_template()
    other = cfg.create_slot_template("Other")

    cfg.rename_slot_template(other.id, "Renamed")
    assert cfg.active_slot_template().name == "Renamed"
    with pytest.raises(ValueError):
        cfg.rename_slot_template(other.id, default.name)


def test_delete_active_template_loads_the_next_one(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.set_headstamp_slot("WIN", 3)
    default = cfg.active_slot_template()
    fresh = cfg.create_slot_template("Fresh", copy_current=False)

    now_active = cfg.delete_slot_template(fresh.id)
    assert now_active is not None
    assert now_active.id == default.id
    assert _slots(cfg)["WIN"] == 3  # Default's layout came back


def test_delete_inactive_template_leaves_the_layout_alone(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    default = cfg.active_slot_template()
    cfg.create_slot_template("Scratch", copy_current=False)
    cfg.set_headstamp_slot("FC", 7)

    cfg.delete_slot_template(default.id)
    assert _slots(cfg)["FC"] == 7


def test_last_template_cannot_be_deleted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    only = cfg.active_slot_template()
    with pytest.raises(ValueError):
        cfg.delete_slot_template(only.id)


# ----- scoping ---------------------------------------------------------------


def test_package_mode_has_its_own_template_list(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.create_slot_template("Standard only")
    assert cfg.slot_template_mode() == "standard"

    cfg.set_run_package_mode(True)
    assert cfg.slot_template_mode() == "package"
    assert [t.name for t in cfg.list_slot_templates()] == [DEFAULT_SLOT_TEMPLATE_NAME]

    # Package assignments are many-to-many and swap as a unit.
    cfg.set_package_slot_headstamp(1, "WIN", True)
    cfg.set_package_slot_headstamp(2, "WIN", True)
    packed = cfg.active_slot_template()
    cfg.create_slot_template("Empty batch", copy_current=False)
    assert cfg.slots_for_headstamp_package("WIN") == []

    cfg.activate_slot_template(packed.id)
    assert cfg.slots_for_headstamp_package("WIN") == [1, 2]

    # Standard-mode templates are untouched by any of that.
    cfg.set_run_package_mode(False)
    assert sorted(t.name for t in cfg.list_slot_templates()) == [
        DEFAULT_SLOT_TEMPLATE_NAME,
        "Standard only",
    ]


def test_templates_are_scoped_per_model(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    first = _activate_seeded_model(db)
    cfg = Config(db).load()
    cfg.add_headstamp("WIN", slot=2)
    cfg.create_slot_template("Model one layout")

    models = ModelRepo(db)
    first_model = models.get(first)
    assert first_model is not None
    second = models.create(
        Model(
            name="Second",
            cartridge_id=first_model.cartridge_id,
            model_mode="convnext_tiny",
        )
    )
    SettingsRepo(db).set_active_model_id(second.id)

    assert [t.name for t in cfg.list_slot_templates()] == [DEFAULT_SLOT_TEMPLATE_NAME]
    SettingsRepo(db).set_active_model_id(first)
    assert sorted(t.name for t in cfg.list_slot_templates()) == [
        DEFAULT_SLOT_TEMPLATE_NAME,
        "Model one layout",
    ]


def test_ai_config_mode_has_its_own_templates(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    cfg = Config(db).load()  # no active model -> AI Config mode
    cfg.add_headstamp("WIN")
    cfg.set_headstamp_slot("WIN", 4)

    template = cfg.active_slot_template()
    assert template.model_id is None
    assert template.assignments["headstamps"] == {"WIN": 4}

    cfg.create_slot_template("AI blank", copy_current=False)
    assert _slots(cfg) == {"WIN": 0}
    cfg.activate_slot_template(template.id)
    assert _slots(cfg) == {"WIN": 4}


def test_templates_are_dropped_with_their_model(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    first = _activate_seeded_model(db)
    cfg = Config(db).load()
    cfg.create_slot_template("Doomed")

    models = ModelRepo(db)
    first_model = models.get(first)
    assert first_model is not None
    keeper = models.create(
        Model(
            name="Keeper",
            cartridge_id=first_model.cartridge_id,
            model_mode="convnext_tiny",
        )
    )
    models.delete(first, replacement_active_id=keeper.id)

    assert SlotTemplateRepo(db).list_for_scope(first, "standard") == []
