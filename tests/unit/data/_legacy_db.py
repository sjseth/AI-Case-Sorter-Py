"""Hand-written pre-SCHEMA_VERSION databases, shared by the migration tests.

Three modules used to carry their own copy of a "legacy" schema
(`test_db.py`, `test_headstamp_parents.py`, `test_parent_runtime.py`), which
meant three things to keep in step and three chances to drift.

The tables are deliberately kept to the **minimum the inserts need**, with the
shape of each table the migration steps touch driven by `SCHEMA_SHAPES` — one
entry per historical `user_version` a database in the wild can carry, so a
fixture and the step that consumes it cannot drift apart.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# The columns `models` carried before any of the later additions. A real
# legacy database still has exactly these, because no old build ever migrated
# this table (issue #44).
LEGACY_MODEL_COLUMNS = {"id", "name", "cartridge_id", "model_mode"}

# What the database looked like on disk at each historical `user_version`.
# `models` stays legacy throughout: no old build ever migrated it, so a v4
# database in the wild carries the v1 `models` table.
SCHEMA_SHAPES: dict[int, dict[str, bool]] = {
    1: {},
    2: {"with_parents_table": True, "with_parent_id": True},
    3: {"with_parents_table": True, "with_parent_id": True, "with_parent_slot": True},
    4: {
        "with_parents_table": True,
        "with_parent_id": True,
        "with_parent_slot": True,
        "with_slot_templates": True,
    },
}


def write_legacy_db(
    path: Path,
    *,
    user_version: int,
    with_parents_table: bool = False,
    with_parent_id: bool = False,
    with_parent_slot: bool = False,
    with_slot_templates: bool = False,
) -> None:
    """Lay down a pre-SCHEMA_VERSION database by hand.

    Only the tables the migration steps touch are given their *old* shape:
    `headstamps` without `parent_id` (unless `with_parent_id`), optionally a
    `headstamp_parents` that carries no `slot` of its own (unless
    `with_parent_slot`), and optionally `slot_templates`. Everything else is
    the minimum the seed rows below need.

    `user_version` no longer decides what runs (sqlite-utils' tracking table
    does, and a legacy database has none — every presence-guarded step runs on
    first open), but a real database of this shape carried this stamp, so the
    fixture writes it too — see `write_db_at_version`, which pairs the two.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE cartridges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE models (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          cartridge_id INTEGER NOT NULL,
          model_mode TEXT NOT NULL DEFAULT 'convnext_tiny'
        );
        CREATE TABLE settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE TABLE headstamps ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL,"
        "  model_id INTEGER NOT NULL,"
        "  slot INTEGER NOT NULL DEFAULT 0,"
        + ("  parent_id INTEGER," if with_parent_id else "")
        + "  UNIQUE(model_id, name))"
    )
    if with_parents_table:
        conn.execute(
            "CREATE TABLE headstamp_parents ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  name TEXT NOT NULL,"
            "  model_id INTEGER NOT NULL,"
            + ("  slot INTEGER NOT NULL DEFAULT 0," if with_parent_slot else "")
            + "  UNIQUE(model_id, name))"
        )
    if with_slot_templates:
        conn.execute(
            "CREATE TABLE slot_templates ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  model_id INTEGER,"
            "  mode TEXT NOT NULL,"
            "  name TEXT NOT NULL,"
            "  assignments_json TEXT NOT NULL DEFAULT '{}',"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )

    conn.execute("INSERT INTO cartridges(id, name) VALUES (1, '9mm')")
    conn.execute("INSERT INTO models(id, name, cartridge_id, model_mode) VALUES (1, 'Legacy', 1, 'convnext_small')")
    conn.execute("INSERT INTO headstamps(name, model_id, slot) VALUES ('WIN', 1, 3)")
    conn.execute("INSERT INTO headstamps(name, model_id, slot) VALUES ('FC', 1, 5)")
    if with_parents_table:
        conn.execute("INSERT INTO headstamp_parents(id, name, model_id) VALUES (1, 'Winchester', 1)")
    conn.execute("INSERT INTO settings(key, value) VALUES ('api', ?)", (json.dumps({"model": "legacy"}),))
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.close()


def write_db_at_version(path: Path, version: int, *, stamped_as: int | None = None) -> None:
    """Write the on-disk shape of `version`, stamped `stamped_as` (default `version`).

    `stamped_as` exists for the historically common mismatch: a database
    already stamped at the current SCHEMA_VERSION while carrying an older
    shape, which is what every pre-ladder build produced (issue #44).
    """
    write_legacy_db(
        path,
        user_version=version if stamped_as is None else stamped_as,
        **SCHEMA_SHAPES[version],
    )


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The column names of `table`.

    `PRAGMA` cannot take a bound parameter in sqlite3, hence the f-string; the
    table name is always a test literal.
    """
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
