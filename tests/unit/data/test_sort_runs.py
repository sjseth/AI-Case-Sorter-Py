"""sort_runs / sort_run_counts: the migration and SortRunRepo."""

from __future__ import annotations

from pathlib import Path

import pytest

from sorter.data.db import Database
from sorter.data.models import Model
from sorter.data.repository import ModelRepo, SortRunRepo

from ._legacy_db import write_db_at_version


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    return db


def test_migration_from_a_pre_sort_run_database_creates_both_tables(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    write_db_at_version(path, 4)
    db = Database(path)
    db.ensure_initialized()
    names = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master")}
    assert "sort_runs" in names
    assert "sort_run_counts" in names
    assert "idx_sort_runs_model_started" in names
    # Re-opening must not duplicate the step or the tables.
    db.ensure_initialized()
    assert db.conn.execute("SELECT COUNT(*) FROM sort_runs").fetchone()[0] == 0


def test_record_upserts_and_keeps_separate_rows_per_slot_and_reason(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = SortRunRepo(db)
    model = ModelRepo(db).list()[0]
    run_id = repo.begin(
        model_id=model.id,
        model_name=model.name,
        mode="standard",
        template_name="Range brass",
        confidence_floor=30,
        slot_quantity=8,
    )
    repo.record(run_id, label="BPS", parent=None, slot=0, reason="unassigned")
    repo.record(run_id, label="BPS", parent="ignored", slot=0, reason="unassigned")
    repo.record(run_id, label="BPS", parent=None, slot=0, reason="below_floor")
    repo.record(run_id, label="WIN", parent="Brass", slot=3, reason="routed")

    rows = {(row["label"], row["slot"], row["reason"]): row for row in repo.counts(run_id)}
    assert rows[("BPS", 0, "unassigned")]["count"] == 2
    # Parent is not part of the key; the first write wins.
    assert rows[("BPS", 0, "unassigned")]["parent"] is None
    assert rows[("BPS", 0, "below_floor")]["count"] == 1
    assert rows[("WIN", 3, "routed")]["parent"] == "Brass"
    assert rows[("WIN", 3, "routed")]["count"] == 1

    stored = db.conn.execute("SELECT * FROM sort_runs WHERE id = ?", (run_id,)).fetchone()
    assert stored["model_id"] == model.id
    assert stored["model_name"] == model.name
    assert stored["mode"] == "standard"
    assert stored["template_name"] == "Range brass"
    assert stored["confidence_floor"] == 30
    assert stored["slot_quantity"] == 8
    assert stored["started_at"]
    assert stored["ended_at"] is None


def test_end_stamps_once(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = SortRunRepo(db)
    run_id = repo.begin(
        model_id=None,
        model_name=None,
        mode="package",
        template_name=None,
        confidence_floor=0,
        slot_quantity=8,
    )
    repo.end(run_id)
    db.conn.execute("UPDATE sort_runs SET ended_at = ? WHERE id = ?", ("2000-01-01 00:00:00", run_id))
    repo.end(run_id)
    ended = db.conn.execute("SELECT ended_at FROM sort_runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert ended == "2000-01-01 00:00:00"


def test_recent_is_newest_first_and_filters_by_model(tmp_path: Path) -> None:
    db = _db(tmp_path)
    models = ModelRepo(db)
    first = models.list()[0]
    second = models.create(Model(name="Other", cartridge_id=first.cartridge_id))
    repo = SortRunRepo(db)
    older = repo.begin(
        model_id=first.id,
        model_name=first.name,
        mode="standard",
        template_name=None,
        confidence_floor=30,
        slot_quantity=8,
    )
    newer = repo.begin(
        model_id=second.id,
        model_name=second.name,
        mode="standard",
        template_name=None,
        confidence_floor=30,
        slot_quantity=8,
    )
    assert [row["id"] for row in repo.recent()] == [newer, older]
    assert [row["id"] for row in repo.recent(model_id=first.id)] == [older]
    assert [row["id"] for row in repo.recent(limit=1)] == [newer]
    # None is every run, not the AI Config rows.
    assert repo.recent(model_id=None)[0]["id"] == newer


def test_begin_rejects_an_unknown_mode(tmp_path: Path) -> None:
    repo = SortRunRepo(_db(tmp_path))
    with pytest.raises(ValueError, match="openai"):
        repo.begin(
            model_id=None,
            model_name=None,
            mode="openai",
            template_name=None,
            confidence_floor=30,
            slot_quantity=8,
        )


def test_deleting_a_model_keeps_the_run_and_deleting_a_run_drops_its_counts(tmp_path: Path) -> None:
    db = _db(tmp_path)
    models = ModelRepo(db)
    first = models.list()[0]
    second = models.create(Model(name="Keeper", cartridge_id=first.cartridge_id))
    repo = SortRunRepo(db)
    run_id = repo.begin(
        model_id=first.id,
        model_name=first.name,
        mode="standard",
        template_name=None,
        confidence_floor=30,
        slot_quantity=8,
    )
    repo.record(run_id, label="BPS", parent=None, slot=0, reason="unassigned")
    models.delete(first.id, replacement_active_id=second.id)
    kept = db.conn.execute("SELECT model_id, model_name FROM sort_runs WHERE id = ?", (run_id,)).fetchone()
    assert kept["model_id"] is None
    assert kept["model_name"] == first.name
    assert len(repo.counts(run_id)) == 1

    db.conn.execute("DELETE FROM sort_runs WHERE id = ?", (run_id,))
    assert repo.counts(run_id) == []
