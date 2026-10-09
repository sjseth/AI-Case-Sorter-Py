"""Cross-thread use of the one shared connection.

``sqlite3.Row`` keeps the cursor's column description. A second statement on
the same connection — the run thread listing headstamps while the UI thread
reads them, or ``SortRunRepo.record`` writing a count — can rebind that
description before ``row["id"]`` indexes the values. That is
``IndexError: tuple index out of range`` (or ``InterfaceError``). ``Database.execute``
holds the connection lock across the fetch and copies the row out.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.repository import ModelRepo, SettingsRepo, SortRunRepo


def test_execute_rows_survive_after_the_cursor_moves_on(tmp_path: Path) -> None:
    db = Database(tmp_path / "rows.db")
    db.ensure_initialized()
    row = db.execute("SELECT id, name FROM cartridges").fetchone()
    assert row is not None
    assert row["name"] == "9mm"
    assert row[0] == row["id"]
    assert "name" in row.keys()
    assert dict(row)["name"] == "9mm"
    # A later statement must not change a row already handed back.
    db.execute("SELECT 1")
    assert row["name"] == "9mm"
    with pytest.raises(IndexError):
        row["no_such_column"]


def test_concurrent_headstamp_reads_and_sort_run_writes(tmp_path: Path) -> None:
    """The shape that killed a live run: readers in config.headstamps, a writer in record."""
    db = Database(tmp_path / "race.db")
    db.ensure_initialized()
    config = Config(db).load()
    model = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(model.id)
    for index in range(20):
        config.add_headstamp(f"H{index:02d}", slot=index % 8)
    run_id = SortRunRepo(db).begin(
        model_id=model.id,
        model_name=model.name,
        mode="standard",
        template_name=None,
        confidence_floor=30,
        slot_quantity=8,
    )

    errors: list[BaseException] = []
    loops = 80
    start = threading.Barrier(4)

    def read() -> None:
        start.wait()
        for _ in range(loops):
            try:
                rows = config.headstamps
                assert len(rows) == 20
                for entry in rows:
                    assert isinstance(entry["name"], str)
                assert config.slot_for_headstamp("H00") == 0
            except Exception as exc:
                errors.append(exc)
                return

    def write() -> None:
        start.wait()
        repo = SortRunRepo(db)
        for _ in range(loops):
            try:
                repo.record(run_id, label="H00", parent=None, slot=0, reason="unassigned")
                rows = config.headstamps
                assert rows[0]["name"]
            except Exception as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=read) for _ in range(3)]
    threads.append(threading.Thread(target=write))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    counts = SortRunRepo(db).counts(run_id)
    assert len(counts) == 1
    assert counts[0]["count"] == loops
    assert counts[0]["label"] == "H00"
