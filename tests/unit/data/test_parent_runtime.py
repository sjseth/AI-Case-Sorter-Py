"""Parent-classification runtime config: visibility, persistence, routing."""

from __future__ import annotations

from pathlib import Path

from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.models import Model
from sorter.data.repository import (
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)

from ._legacy_db import columns, write_legacy_db


def _new_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    return db


def _activate_seeded_model(db: Database) -> int:
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    return seed.id


def _seed_parents(db: Database, model_id: int) -> dict[str, int]:
    """WIN/FC parents with BLZR,PMC->WIN-ish; leaves WIN,XTRM as orphans."""
    hs = HeadstampRepo(db)
    pr = HeadstampParentRepo(db)
    brass = pr.add(model_id, "Brass")
    nickle = pr.add(model_id, "Nickle")
    for name, parent in [("BLZR", brass), ("CBC", brass), ("RP", nickle)]:
        h = hs.add(model_id, name)
        hs.set_parent(h.id, parent.id)
    hs.add(model_id, "WIN")  # orphan
    hs.add(model_id, "XTRM")  # orphan
    return {"Brass": brass.id, "Nickle": nickle.id}


def test_model_has_parents(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    mid = _activate_seeded_model(db)
    cfg = Config(db).load()
    assert cfg.model_has_parents() is False
    _seed_parents(db, mid)
    assert cfg.model_has_parents() is True

    # AI Config mode never has parents.
    SettingsRepo(db).clear_active_model()
    assert Config(db).load().model_has_parents() is False


def test_use_parent_flag_is_per_model(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    mid = _activate_seeded_model(db)
    cfg = Config(db).load()
    assert cfg.use_parent_classifications is False
    assert cfg.set_use_parent_classifications(True)
    assert Config(db).load().use_parent_classifications is True

    # A different active model has its own (default-off) flag.
    seeded = ModelRepo(db).get(mid)
    assert seeded is not None
    other = ModelRepo(db).create(
        Model(
            name="other",
            cartridge_id=seeded.cartridge_id,
            model_mode="convnext_tiny",
        )
    )
    SettingsRepo(db).set_active_model_id(other.id)
    assert Config(db).load().use_parent_classifications is False

    # AI Config mode: setting is a no-op, reads False.
    SettingsRepo(db).clear_active_model()
    cfg = Config(db).load()
    assert cfg.set_use_parent_classifications(True) is False
    assert cfg.use_parent_classifications is False


def test_parent_slot_assignment_and_listing(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    mid = _activate_seeded_model(db)
    ids = _seed_parents(db, mid)
    cfg = Config(db).load()

    assert cfg.set_parent_slot(ids["Brass"], 1)
    assert cfg.set_parent_slot(ids["Nickle"], 2)
    by_name = {p["name"]: p for p in Config(db).load().parents_with_slots()}
    assert by_name["Brass"]["slot"] == 1
    assert by_name["Nickle"]["slot"] == 2

    rows = {h["name"]: h for h in cfg.headstamps_with_parents()}
    assert rows["BLZR"]["parent_id"] == ids["Brass"]
    assert rows["WIN"]["parent_id"] is None


def test_routing_uses_parent_slot_when_enabled(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    mid = _activate_seeded_model(db)
    ids = _seed_parents(db, mid)
    cfg = Config(db).load()
    cfg.set_parent_slot(ids["Brass"], 1)
    cfg.set_parent_slot(ids["Nickle"], 2)
    cfg.set_headstamp_slot("WIN", 3)  # orphan keeps its own slot
    cfg.set_use_parent_classifications(True)

    routed = Config(db).load()
    # Children route to their parent's slot...
    assert routed.slot_for_headstamp("BLZR") == 1
    assert routed.slot_for_headstamp("CBC") == 1
    assert routed.slot_for_headstamp("RP") == 2
    # ...orphans route to their own slot...
    assert routed.slot_for_headstamp("WIN") == 3
    # ...a parent-name prediction routes to the parent slot...
    assert routed.slot_for_headstamp("Brass") == 1
    # ...unknown labels fall through to catch-all.
    assert routed.slot_for_headstamp("ZZZ") is None


def test_parent_for_headstamp(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    mid = _activate_seeded_model(db)
    _seed_parents(db, mid)
    cfg = Config(db).load()
    assert cfg.parent_for_headstamp("BLZR") == "Brass"
    assert cfg.parent_for_headstamp("RP") == "Nickle"
    assert cfg.parent_for_headstamp("WIN") is None  # orphan
    assert cfg.parent_for_headstamp("ZZZ") is None  # unknown

    SettingsRepo(db).clear_active_model()
    assert Config(db).load().parent_for_headstamp("BLZR") is None  # AI Config mode


def test_routing_ignores_parents_when_disabled(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    mid = _activate_seeded_model(db)
    ids = _seed_parents(db, mid)
    cfg = Config(db).load()
    cfg.set_parent_slot(ids["Brass"], 1)
    # Child has its own (child-mode) slot independent of the parent's slot.
    cfg.set_headstamp_slot("BLZR", 5)
    # Flag off (default): standard per-headstamp routing.
    routed = Config(db).load()
    assert routed.slot_for_headstamp("BLZR") == 5


def test_parent_slot_column_migrates_on_v2_db(tmp_path: Path) -> None:
    """A v2 headstamp_parents table (no slot column) gains it on open.

    The schema-level assertions (column present, existing rows backfilled to
    0) live in `test_db.py`; what this one adds is that the slot is writable
    through the repo afterwards.
    """
    db_path = tmp_path / "v2.db"
    write_legacy_db(db_path, user_version=2, with_parents_table=True, with_parent_id=True)

    db = Database(db_path)
    db.ensure_initialized()
    assert "slot" in columns(db.conn, "headstamp_parents")
    HeadstampParentRepo(db).update_slot(1, 4)
    parent = HeadstampParentRepo(db).get(1)
    assert parent is not None
    assert parent.slot == 4
