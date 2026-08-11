"""Tests for the SQLite Database wrapper + JSON migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sorter.data import db as db_module
from sorter.data.db import DEFAULT_CARTRIDGE_NAME, DEFAULT_MODEL_MODE, SCHEMA_VERSION, Database
from sorter.data.repository import (
    CartridgeRepo,
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
    SlotTemplateRepo,
)

from ._legacy_db import LEGACY_MODEL_COLUMNS, SCHEMA_SHAPES, columns, write_db_at_version

# The registered migration steps, in application order. Spelled out rather
# than read from sorter.data.db because the names are load-bearing: sqlite-utils
# keys its `_sqlite_migrations` bookkeeping on them, so renaming one makes
# every install in the wild run it again — this list failing is the alarm.
MIGRATION_NAMES = [
    "0002_headstamps_parent_id",
    "0003_headstamp_parents_slot",
    "0004_slot_templates",
    "0005_models_columns",
]
MIGRATION_SET = "casesorter"


def test_fresh_db_seeds_default_cartridge_and_model(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()

    cartridges = db.dump_table("cartridges")
    models = db.dump_table("models")
    settings = {r["key"]: json.loads(r["value"]) for r in db.dump_table("settings")}

    assert len(cartridges) == 1
    assert cartridges[0]["name"] == DEFAULT_CARTRIDGE_NAME
    assert len(models) == 1
    assert models[0]["model_mode"] == DEFAULT_MODEL_MODE
    # The seeded model is NOT auto-activated — fresh install defaults to AI Config mode.
    assert "default_model_id" not in settings


def test_ensure_initialized_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    first_models = db.dump_table("models")
    db.ensure_initialized()  # second call must not duplicate
    second_models = db.dump_table("models")
    assert first_models == second_models


def test_migration_from_legacy_json(tmp_path: Path) -> None:
    legacy = tmp_path / "config.json"
    legacy.write_text(
        json.dumps(
            {
                "api": {"endpoint_url": "http://example.com", "model": "9mm-comp"},
                "serial": {"port": "COM3", "baud": 115200},
                "image_proc": {"strategy": "hough"},
                "camera": {"device_index": 1},
                "headstamps": [
                    {"name": "WIN", "slot": 3},
                    {"name": "FC", "slot": 5},
                ],
            }
        )
    )

    db = Database(tmp_path / "test.db")
    db.ensure_initialized(legacy_config_json=legacy)

    cartridges = db.dump_table("cartridges")
    models = db.dump_table("models")
    headstamps = db.dump_table("headstamps")
    settings = {r["key"]: json.loads(r["value"]) for r in db.dump_table("settings")}

    assert len(cartridges) == 1
    assert len(models) == 1
    assert models[0]["name"] == "9mm-comp"
    assert {h["name"] for h in headstamps} == {"WIN", "FC"}
    assert {h["slot"] for h in headstamps} == {3, 5}
    assert settings["api"]["endpoint_url"] == "http://example.com"
    assert settings["serial"]["port"] == "COM3"
    assert settings["camera"]["device_index"] == 1
    # Migration does not set an active model — AI Config mode is the default.
    assert "default_model_id" not in settings

    assert not legacy.exists(), "legacy JSON should be renamed to .bak"
    assert legacy.with_suffix(".json.bak").exists()


def test_migration_corrupt_json_falls_back_to_seed(tmp_path: Path) -> None:
    legacy = tmp_path / "config.json"
    legacy.write_text("{not json")

    db = Database(tmp_path / "test.db")
    db.ensure_initialized(legacy_config_json=legacy)

    cartridges = db.dump_table("cartridges")
    assert len(cartridges) == 1
    assert cartridges[0]["name"] == DEFAULT_CARTRIDGE_NAME


def test_foreign_key_constraint_enforced(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()

    with pytest.raises(Exception):  # IntegrityError
        db.conn.execute("INSERT INTO models(name, cartridge_id, model_mode) VALUES ('bad', 9999, 'convnext_tiny')")


def _user_version(db: Database) -> int:
    return db.conn.execute("PRAGMA user_version").fetchone()[0]


def _fresh_columns(tmp_path: Path, table: str) -> set[str]:
    """The columns `table` has on a brand-new database."""
    db = Database(tmp_path / "reference.db")
    db.ensure_initialized()
    try:
        return columns(db.conn, table)
    finally:
        db.close()


def test_migration_from_v1_adds_columns_and_keeps_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, 1)

    db = Database(db_path)
    db.ensure_initialized()

    assert _user_version(db) == SCHEMA_VERSION

    # The migration added parent_id to the existing headstamps table.
    assert "parent_id" in columns(db.conn, "headstamps")
    # headstamp_parents and slot_templates did not exist at all, so these come
    # from the idempotent CREATE TABLE IF NOT EXISTS pass rather than from an
    # ALTER — a migration step only has to patch a table it finds already there.
    assert {"slot", "model_id", "name"} <= columns(db.conn, "headstamp_parents")
    assert db.conn.execute("SELECT COUNT(*) FROM slot_templates").fetchone()[0] == 0

    # Pre-existing data survives untouched, with a sane default for the new column.
    headstamps = {h["name"]: h for h in db.dump_table("headstamps")}
    assert set(headstamps) == {"WIN", "FC"}
    assert headstamps["WIN"]["slot"] == 3
    assert headstamps["FC"]["slot"] == 5
    assert headstamps["WIN"]["parent_id"] is None

    models = db.dump_table("models")
    assert len(models) == 1, "an existing DB must not be re-seeded"
    assert models[0]["name"] == "Legacy"
    assert models[0]["model_mode"] == "convnext_small"

    settings = {r["key"]: json.loads(r["value"]) for r in db.dump_table("settings")}
    assert settings["api"]["model"] == "legacy"


@pytest.mark.parametrize("version", sorted(SCHEMA_SHAPES))
def test_an_existing_models_table_is_migrated_to_the_current_shape(tmp_path: Path, version: int) -> None:
    """`models` is brought up to the current column set from every old version.

    This is the fix for issue #44: `models` used to be left exactly as found,
    because the DDL is CREATE TABLE IF NOT EXISTS and no column migration ever
    touched it, so every repository read of a later column raised
    OperationalError on such a database.
    """
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, version)
    with sqlite3.connect(db_path) as fixture:
        assert columns(fixture, "models") == LEGACY_MODEL_COLUMNS, "the fixture must start out legacy"

    db = Database(db_path)
    db.ensure_initialized()

    assert columns(db.conn, "models") == _fresh_columns(tmp_path, "models")

    row = db.conn.execute("SELECT * FROM models WHERE id = 1").fetchone()
    assert row["name"] == "Legacy", "the pre-existing row survives the migration"
    assert row["model_type"] == "Standard"
    assert row["model_version"] == 1
    assert row["feedback_loop_enabled"] == 0
    assert row["feedback_loop_upload_mode"] == "Manual"
    assert row["community_model_uid"] is None
    # ADD COLUMN cannot carry the DDL's datetime('now') default on a populated
    # table, so these are backfilled instead of arriving NULL.
    assert row["created_at"] is not None
    assert row["updated_at"] is not None


@pytest.mark.parametrize("version", sorted(SCHEMA_SHAPES))
def test_every_repository_read_works_after_migrating_from_any_version(tmp_path: Path, version: int) -> None:
    """Acceptance for issue #44: a legacy DB opens and reads cleanly."""
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, version)

    db = Database(db_path)
    db.ensure_initialized()

    models = ModelRepo(db).list()
    assert [m.name for m in models] == ["Legacy"]
    assert models[0].model_type == "Standard"
    assert ModelRepo(db).get(1) is not None
    assert ModelRepo(db).find_by_community_uid("nope") is None
    assert [c.name for c in CartridgeRepo(db).list()] == ["9mm"]
    assert {h.name for h in HeadstampRepo(db).list_for_model(1)} == {"WIN", "FC"}
    assert [p.name for p in HeadstampParentRepo(db).list_for_model(1)] in ([], ["Winchester"])
    assert SlotTemplateRepo(db).list_for_scope(None, "standard") == []
    assert SettingsRepo(db).get("api")["model"] == "legacy"


def test_migration_adds_slot_to_an_existing_headstamp_parents_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, 2)

    db = Database(db_path)
    db.ensure_initialized()

    assert _user_version(db) == SCHEMA_VERSION
    assert "slot" in columns(db.conn, "headstamp_parents")

    parents = db.dump_table("headstamp_parents")
    assert len(parents) == 1
    assert parents[0]["name"] == "Winchester"
    assert parents[0]["slot"] == 0, "new NOT NULL column backfills to its default"


def test_migration_is_idempotent_on_an_already_migrated_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, 2)

    db = Database(db_path)
    db.ensure_initialized()
    first = {
        table: db.dump_table(table) for table in ("cartridges", "models", "headstamps", "headstamp_parents", "settings")
    }
    cols = {table: columns(db.conn, table) for table in first}

    # A second *process* is the realistic repeat, not a second call on the same
    # object: a fresh Database re-runs connect()'s WAL setup against a file that
    # is already in WAL mode, which the same-object path never exercises.
    reopened = Database(db_path)
    reopened.ensure_initialized()

    assert _user_version(reopened) == SCHEMA_VERSION
    assert {table: reopened.dump_table(table) for table in first} == first
    assert {table: columns(reopened.conn, table) for table in first} == cols


def _applied_migrations(db: Database) -> list[tuple[str, str]]:
    """The (name, applied_at) rows sqlite-utils recorded, in application order."""
    rows = db.conn.execute(
        "SELECT name, applied_at FROM _sqlite_migrations WHERE migration_set = ? ORDER BY rowid",
        (MIGRATION_SET,),
    ).fetchall()
    return [(r["name"], r["applied_at"]) for r in rows]


def test_migration_steps_are_recorded_in_order_and_only_once(tmp_path: Path) -> None:
    """sqlite-utils' tracking table decides what runs, so reopening replays nothing."""
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, 1)

    db = Database(db_path)
    db.ensure_initialized()
    first = _applied_migrations(db)
    assert [name for name, _ in first] == MIGRATION_NAMES
    db.close()

    reopened = Database(db_path)
    reopened.ensure_initialized()
    # Identical applied_at timestamps prove the steps were skipped, not rerun.
    assert _applied_migrations(reopened) == first, "a second open must not rerun a landed step"


def test_the_tracking_table_not_the_stamp_decides_what_runs(tmp_path: Path) -> None:
    """A step recorded in `_sqlite_migrations` is skipped whatever `user_version` says.

    The stamp is informational only: this database claims v1 — older than
    every step — yet the pre-recorded `models` step must not run again.
    """
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, 1)
    fixture = sqlite3.connect(db_path)
    with fixture:
        fixture.execute(
            "CREATE TABLE _sqlite_migrations (id INTEGER PRIMARY KEY, migration_set TEXT, name TEXT, applied_at TEXT)"
        )
        fixture.execute("CREATE UNIQUE INDEX idx_sqlite_migrations ON _sqlite_migrations(migration_set, name)")
        fixture.execute(
            "INSERT INTO _sqlite_migrations (migration_set, name, applied_at) VALUES (?, ?, 'earlier')",
            (MIGRATION_SET, MIGRATION_NAMES[-1]),
        )
    fixture.close()

    db = Database(db_path)
    db.ensure_initialized()

    # The recorded models step was skipped, so the table kept its legacy shape...
    assert columns(db.conn, "models") == LEGACY_MODEL_COLUMNS
    # ...while every unrecorded step still ran (the pre-recorded row keeps its
    # earlier rowid, so it lists first).
    assert "parent_id" in columns(db.conn, "headstamps")
    assert [name for name, _ in _applied_migrations(db)] == [MIGRATION_NAMES[-1], *MIGRATION_NAMES[:-1]]


def test_a_fresh_db_records_every_step_and_stamps_current(tmp_path: Path) -> None:
    """On a fresh DB the DDL lays down the current shape, so every step is a
    presence-guarded no-op — but each is still recorded, so only steps added
    later will ever run against this database."""
    db = Database(tmp_path / "fresh.db")
    db.ensure_initialized()

    assert [name for name, _ in _applied_migrations(db)] == MIGRATION_NAMES
    assert _user_version(db) == SCHEMA_VERSION


def test_a_db_stamped_current_but_incomplete_is_repaired(tmp_path: Path) -> None:
    """Every pre-ladder build stamped SCHEMA_VERSION without migrating `models`.

    Under sqlite-utils the stamp is irrelevant: such a database has no
    tracking table, so every presence-guarded step runs on first open and the
    `models` step repairs it by inspecting the actual columns.
    """
    db_path = tmp_path / "stamped.db"
    write_db_at_version(db_path, 4, stamped_as=SCHEMA_VERSION)

    db = Database(db_path)
    db.ensure_initialized()

    assert _user_version(db) == SCHEMA_VERSION
    assert columns(db.conn, "models") == _fresh_columns(tmp_path, "models")
    assert ModelRepo(db).list()[0].model_type == "Standard"


# How many of `_LATE_MODEL_COLUMNS` a partially-migrated `models` table already
# carries. Not every install migrated *nothing*: a user whose database was
# created fresh partway through the project's history got whatever columns the
# DDL declared on that day, so `models` in the wild can hold any prefix of the
# list. 18 stops just short of the two backfilled timestamps; 20 is a table
# that is already complete on an otherwise-old database.
@pytest.mark.parametrize("already_present", [1, 5, 10, 18, 20])
def test_a_partially_migrated_models_table_is_completed(tmp_path: Path, already_present: int) -> None:
    """The `models` step is guarded per column, not per table.

    It has to be: a half-migrated table would otherwise either raise "duplicate
    column name" on the first ALTER it repeats, or be skipped wholesale and
    left missing the rest.
    """
    db_path = tmp_path / "partial.db"
    write_db_at_version(db_path, 4)
    carried = db_module._LATE_MODEL_COLUMNS[:already_present]
    fixture = sqlite3.connect(db_path)
    with fixture:
        for name, definition in carried:
            fixture.execute(f"ALTER TABLE models ADD COLUMN {name} {definition}")
    fixture.close()

    db = Database(db_path)
    db.ensure_initialized()

    assert columns(db.conn, "models") == _fresh_columns(tmp_path, "models")
    row = db.conn.execute("SELECT * FROM models WHERE id = 1").fetchone()
    assert row["name"] == "Legacy", "the pre-existing row survives"
    assert row["model_type"] == "Standard"
    assert row["feedback_loop_upload_mode"] == "Manual"
    # Backfilled whether they arrived with this migration or an earlier one:
    # a column added by ADD COLUMN cannot carry the DDL's datetime('now').
    assert row["created_at"] is not None
    assert row["updated_at"] is not None


def test_a_failing_step_rolls_back_the_whole_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupted upgrade must leave nothing behind — least of all a
    tracking row claiming a step landed when its changes were rolled back.

    That is the point of running the steps inside `transaction()`: sqlite-utils
    commits each step as it goes, so without the enclosing transaction a later
    failure would strand the earlier steps' bookkeeping.
    """

    def boom(conn: sqlite3.Connection) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr(db_module, "_add_missing_model_columns", boom)

    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, 1)

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="interrupted"):
        db.ensure_initialized()
    db.close()

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    try:
        recorded = check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = '_sqlite_migrations'"
        ).fetchone()[0]
        assert recorded == 0, "no step may be recorded when the run rolled back"
        # The steps that ran before the failure are undone too — SQLite rolls
        # DDL back with everything else.
        assert "parent_id" not in columns(check, "headstamps")
        assert columns(check, "models") == LEGACY_MODEL_COLUMNS
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1, "the stamp never advanced"
    finally:
        check.close()

    # With the failure gone, the next launch migrates cleanly from the same
    # untouched database — a rolled-back attempt is not a poisoned one.
    monkeypatch.undo()
    retried = Database(db_path)
    retried.ensure_initialized()
    assert [name for name, _ in _applied_migrations(retried)] == MIGRATION_NAMES
    assert columns(retried.conn, "models") == _fresh_columns(tmp_path, "models")


def test_ensure_initialized_does_not_downgrade_user_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    db.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 10}")
    db.ensure_initialized()
    # The stamp is informational and only ever advanced (`current <
    # SCHEMA_VERSION` guard), so a DB written by a newer build keeps its stamp.
    assert _user_version(db) == SCHEMA_VERSION + 10


def test_model_mode_check_constraint(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    cart_id = db.conn.execute("SELECT id FROM cartridges LIMIT 1").fetchone()["id"]
    with pytest.raises(Exception):
        db.conn.execute(
            "INSERT INTO models(name, cartridge_id, model_mode) VALUES (?, ?, ?)",
            ("bad-mode", cart_id, "resnet50"),
        )
