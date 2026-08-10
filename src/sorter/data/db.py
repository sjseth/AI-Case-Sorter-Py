"""SQLite database wrapper + one-shot migration from legacy config.json.

Exposes a single `Database` class that owns:
  - a `sqlite3.Connection` with row_factory = sqlite3.Row
  - schema DDL plus an ordered set of migration steps run through
    `sqlite_utils.Migrations`
  - `ensure_initialized()` that creates the DB if missing and runs the
    one-shot import from `data/config.json` when present.

Every open lays down `SCHEMA_DDL` (idempotent, IF NOT EXISTS) and then applies
`MIGRATIONS`: sqlite-utils records each applied step in its
`_sqlite_migrations` table and skips it on every later open, so ordering and
run-once bookkeeping live there. That tracking table is **authoritative**;
`PRAGMA user_version` is still stamped to `SCHEMA_VERSION` after a successful
run, but only as an informational marker (and it is never downgraded).

Databases in the wild carry no tracking table — earlier builds versioned the
schema by `user_version` alone, and the pre-ladder ones stamped it to current
without actually migrating anything (issue #44). On first open under this
code every registered step therefore looks un-applied and runs, whatever the
stamp claims — which is exactly what repairs those installs. The price is
that **every step must be presence-guarded and idempotent**: safe against any
historical shape, and against a fresh database where the DDL pass already
laid down the current schema.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import sqlite_utils
from sqlite_utils.migrations import Migrations

from . import paths

SCHEMA_VERSION = 5

# Split out of SCHEMA_DDL because the v3 -> v4 ladder step replays it verbatim;
# one copy means the step cannot drift from the schema.
SLOT_TEMPLATES_DDL = """
-- Named slot-assignment layouts for the Run tab. Scoped to a model
-- (model_id NULL = AI Config mode) and to a run mode ('standard'/'package'),
-- which keep separate lists because their assignment rules differ.
CREATE TABLE IF NOT EXISTS slot_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id INTEGER REFERENCES models(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK(mode IN ('standard','package')),
  name TEXT NOT NULL,
  assignments_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- IFNULL() because UNIQUE treats every NULL model_id (AI Config mode) as
-- distinct, which would let duplicate AI-mode template names through.
CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_templates_name
  ON slot_templates(IFNULL(model_id, -1), mode, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_slot_templates_scope ON slot_templates(model_id, mode);
"""

SCHEMA_DDL = (
    """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS cartridges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS models (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cartridge_id INTEGER NOT NULL REFERENCES cartridges(id) ON DELETE RESTRICT,
  model_mode TEXT NOT NULL
    CHECK(model_mode IN ('convnext_tiny','convnext_small','convnext_base','convnext_large')),
  model_type TEXT NOT NULL DEFAULT 'Standard'
    CHECK(model_type IN ('Standard','ReadOnly','CommunityManaged')),
  community_model_uid TEXT,
  model_version INTEGER NOT NULL DEFAULT 1,
  enable_image_processing INTEGER NOT NULL DEFAULT 1,
  image_processing_json TEXT,
  training_config_json TEXT,
  ai_model_config_json TEXT,
  use_primer_mask INTEGER NOT NULL DEFAULT 0,
  hide_primer INTEGER NOT NULL DEFAULT 1,
  primer_mask_size INTEGER NOT NULL DEFAULT 135,
  last_training_date TEXT,
  last_training_duration INTEGER NOT NULL DEFAULT 0,
  trained_image_count INTEGER NOT NULL DEFAULT 0,
  training_confusion_table TEXT,
  feedback_loop_enabled INTEGER NOT NULL DEFAULT 0,
  feedback_loop_confidence_floor INTEGER NOT NULL DEFAULT 95,
  feedback_loop_upload_mode TEXT NOT NULL DEFAULT 'Manual',
  model_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_models_cartridge ON models(cartridge_id);

-- Parent classifications: named groups that child headstamps roll up into.
-- Scoped per-model.
CREATE TABLE IF NOT EXISTS headstamp_parents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL DEFAULT 0,
  UNIQUE(model_id, name)
);
CREATE INDEX IF NOT EXISTS idx_headstamp_parents_model ON headstamp_parents(model_id);

CREATE TABLE IF NOT EXISTS headstamps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL DEFAULT 0,
  parent_id INTEGER REFERENCES headstamp_parents(id) ON DELETE SET NULL,
  UNIQUE(model_id, name)
);
CREATE INDEX IF NOT EXISTS idx_headstamps_model ON headstamps(model_id);
"""
    + SLOT_TEMPLATES_DDL
    + """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
)
# NOTE: community feedback-loop captures are intentionally NOT tracked in the
# DB. They live as JPEGs under data/models/<id>/feedback_images/ and that folder
# is the queue (see sorter/feedback.py). A legacy DB may still carry an unused
# feedback_queue table from an earlier schema; it is simply ignored.


DEFAULT_CARTRIDGE_NAME = "9mm"
DEFAULT_MODEL_NAME = "Default"
DEFAULT_MODEL_MODE = "convnext_tiny"


# ----- schema migrations ------------------------------------------------------


def _execute_script(conn: sqlite3.Connection, ddl: str) -> None:
    """Run a multi-statement DDL string one statement at a time.

    Not `executescript()`: that commits any open transaction before it runs,
    which would tear a migration step out of its enclosing transaction.
    """
    for stmt in ddl.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The column names of `table`, empty when the table does not exist.

    `PRAGMA` cannot take a bound parameter in sqlite3, hence the f-string; the
    table name is always a literal from this module.
    """
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# Every `models` column the current DDL declares beyond the four the table
# shipped with, in declaration order, as (name, ALTER definition).
_LATE_MODEL_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "model_type",
        "TEXT NOT NULL DEFAULT 'Standard' CHECK(model_type IN ('Standard','ReadOnly','CommunityManaged'))",
    ),
    ("community_model_uid", "TEXT"),
    ("model_version", "INTEGER NOT NULL DEFAULT 1"),
    ("enable_image_processing", "INTEGER NOT NULL DEFAULT 1"),
    ("image_processing_json", "TEXT"),
    ("training_config_json", "TEXT"),
    ("ai_model_config_json", "TEXT"),
    ("use_primer_mask", "INTEGER NOT NULL DEFAULT 0"),
    ("hide_primer", "INTEGER NOT NULL DEFAULT 1"),
    ("primer_mask_size", "INTEGER NOT NULL DEFAULT 135"),
    ("last_training_date", "TEXT"),
    ("last_training_duration", "INTEGER NOT NULL DEFAULT 0"),
    ("trained_image_count", "INTEGER NOT NULL DEFAULT 0"),
    ("training_confusion_table", "TEXT"),
    ("feedback_loop_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("feedback_loop_confidence_floor", "INTEGER NOT NULL DEFAULT 95"),
    ("feedback_loop_upload_mode", "TEXT NOT NULL DEFAULT 'Manual'"),
    ("model_path", "TEXT"),
    # SQLite rejects ADD COLUMN with a non-constant default on a populated
    # table, so the two timestamps cannot carry the DDL's
    # `NOT NULL DEFAULT (datetime('now'))`. They arrive nullable and are
    # backfilled instead.
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
)

_BACKFILLED_MODEL_COLUMNS = ("created_at", "updated_at")


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    """ALTER `table` to add `name`, unless it is already there.

    A table the DDL pass had to create arrives complete and is skipped
    wholesale, so a step only ever patches a table it found already present.
    """
    present = _columns(conn, table)
    if present and name not in present:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _add_missing_model_columns(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing `models` table up to the current column set."""
    if not _columns(conn, "models"):
        return
    for name, definition in _LATE_MODEL_COLUMNS:
        _add_column(conn, "models", name, definition)
    for name in _BACKFILLED_MODEL_COLUMNS:
        conn.execute(f"UPDATE models SET {name} = datetime('now') WHERE {name} IS NULL")


# The migration set. Steps run in registration order; sqlite-utils records
# each one in `_sqlite_migrations` keyed by (set, name) and never reruns a
# recorded step, so the explicit `name=` strings are load-bearing — renaming
# one would make every install run it again. Numbered after the historical
# `user_version` each change shipped at.
#
# Every step is presence-guarded (see the module docstring for why): it runs
# once on every pre-existing database regardless of shape, and once on a
# fresh one where the DDL pass has already done its work.
MIGRATIONS = Migrations("casesorter")


@MIGRATIONS(name="0002_headstamps_parent_id")
def _headstamps_parent_id(db: sqlite_utils.Database) -> None:
    """Parent classifications: the child link on `headstamps`.

    The `headstamp_parents` table itself is created by the DDL pass that runs
    before the migrations; only the column on an already-present `headstamps`
    table needs an ALTER.
    """
    # NULL default keeps this a legal ALTER even with a REFERENCES clause.
    _add_column(
        db.conn,
        "headstamps",
        "parent_id",
        "INTEGER REFERENCES headstamp_parents(id) ON DELETE SET NULL",
    )


@MIGRATIONS(name="0003_headstamp_parents_slot")
def _headstamp_parents_slot(db: sqlite_utils.Database) -> None:
    """Per-parent slot assignment."""
    _add_column(db.conn, "headstamp_parents", "slot", "INTEGER NOT NULL DEFAULT 0")


@MIGRATIONS(name="0004_slot_templates")
def _slot_templates(db: sqlite_utils.Database) -> None:
    """Named slot-assignment layouts for the Run tab.

    Replays the shared `SLOT_TEMPLATES_DDL` constant (all IF NOT EXISTS), so
    the step cannot drift from the schema.
    """
    _execute_script(db.conn, SLOT_TEMPLATES_DDL)


@MIGRATIONS(name="0005_models_columns")
def _models_columns(db: sqlite_utils.Database) -> None:
    """The `models` columns no earlier build ever migrated (issue #44).

    Presence-guarded per column, so this single step repairs every historical
    shape — including a database stamped `user_version = SCHEMA_VERSION` by a
    pre-ladder build while still carrying the original four-column table.
    """
    _add_missing_model_columns(db.conn)


class Database:
    """Owns a single sqlite3.Connection for the lifetime of the app."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else paths.db_path()
        self._conn: sqlite3.Connection | None = None
        # Worker threads (RunController, training manager, community
        # downloader) read/write the DB while the UI thread does the same.
        # sqlite3 raises "objects created in a thread can only be used in
        # that same thread" by default; check_same_thread=False allows the
        # cross-thread access and this RLock serialises multi-statement
        # transactions so two threads can't interleave a BEGIN/COMMIT pair.
        # Single-statement execute() calls are atomic at the SQLite layer
        # so they don't need the lock, but transaction() / SAVEPOINT blocks
        # do.
        self._lock = threading.RLock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized; call ensure_initialized() first")
        return self._conn

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path,
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run statements in a single transaction. Auto-commit/rollback.

        Reentrant: when called within an existing transaction, uses a
        SAVEPOINT so nested `with db.transaction()` blocks compose naturally.
        The RLock serialises across threads so concurrent transactions
        from worker threads (test_once, training subprocess wire-up,
        community downloads) and the UI thread don't interleave.
        """
        with self._lock:
            conn = self.conn
            if conn.in_transaction:
                sp_name = f"sp_{id(conn) & 0xFFFF}_{conn.total_changes & 0xFFFF}"
                conn.execute(f"SAVEPOINT {sp_name}")
                try:
                    yield conn
                    conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                except Exception:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                    raise
            else:
                conn.execute("BEGIN")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

    def ensure_initialized(self, legacy_config_json: Path | None = None) -> None:
        """Create the DB if missing, run DDL, and migrate from legacy JSON.

        If the DB is brand new and `legacy_config_json` is provided and exists,
        its contents are imported and the file is renamed to ``.bak``. If no
        legacy config exists, the DB is still seeded with a default cartridge
        and model so the app boots into a usable state.
        """
        was_fresh = not self.path.exists()
        conn = self.connect()
        # DDL is idempotent (IF NOT EXISTS) so re-running on an existing DB is safe.
        _execute_script(conn, SCHEMA_DDL)
        self._migrate_schema()

        if was_fresh:
            if legacy_config_json and Path(legacy_config_json).exists():
                self._migrate_from_json(Path(legacy_config_json))
            else:
                self._seed_defaults()

    # ----- migration ----------------------------------------------------------

    def _migrate_schema(self) -> None:
        """Apply every pending migration step, then stamp `user_version`.

        sqlite-utils' `_sqlite_migrations` table decides what is pending, so
        every step runs at most once per database — including on a fresh one,
        where each step is a presence-guarded no-op that just gets recorded.

        The whole run happens inside one `transaction()` (sqlite-utils'
        per-step transactions nest as SAVEPOINTs), so an interrupted upgrade
        rolls back whole: no step is left recorded as applied without its
        changes, and the stamp never advances early. `user_version` is written
        last, informationally, and never downgraded — a database stamped by a
        newer build keeps its stamp.
        """
        with self.transaction() as conn:
            # Wrapping our own connection: recursive_triggers/execute_plugins
            # off so construction leaves the connection exactly as it was.
            MIGRATIONS.apply(sqlite_utils.Database(conn, recursive_triggers=False, execute_plugins=False))
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_from_json(self, json_path: Path) -> None:
        """One-shot import. Reads `config.json`, writes rows, renames to `.bak`."""
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._seed_defaults()
            return

        api = raw.get("api") or {}
        with self.transaction() as conn:
            for section in ("api", "serial", "image_proc", "camera"):
                value = raw.get(section)
                if value is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                        (section, json.dumps(value)),
                    )

            cart_id = conn.execute(
                "INSERT INTO cartridges(name) VALUES (?)",
                (DEFAULT_CARTRIDGE_NAME,),
            ).lastrowid

            model_name = api.get("model") or DEFAULT_MODEL_NAME
            model_id = conn.execute(
                "INSERT INTO models(name, cartridge_id, model_mode) VALUES (?, ?, ?)",
                (model_name, cart_id, DEFAULT_MODEL_MODE),
            ).lastrowid

            for entry in raw.get("headstamps") or []:
                hs_name = entry.get("name")
                if not hs_name:
                    continue
                hs_slot = int(entry.get("slot", 0))
                conn.execute(
                    "INSERT OR IGNORE INTO headstamps(name, model_id, slot) VALUES (?, ?, ?)",
                    (hs_name, model_id, hs_slot),
                )

            # Note: we intentionally do NOT seed settings['default_model_id'].
            # An absent key signals "AI Config mode" — the seeded model is
            # available but not active until the user activates it from the
            # Models tab.

        backup = json_path.with_suffix(json_path.suffix + ".bak")
        with contextlib.suppress(OSError):
            shutil.move(str(json_path), backup)

    def _seed_defaults(self) -> None:
        """Insert a starter cartridge + model so a fresh install is usable.

        The seeded model is NOT auto-activated; default runtime is AI Config
        mode (no `default_model_id` setting). The Models tab's synthetic
        "Use AI Config" row is the active selection out of the box.
        """
        with self.transaction() as conn:
            cart_id = conn.execute(
                "INSERT INTO cartridges(name) VALUES (?)",
                (DEFAULT_CARTRIDGE_NAME,),
            ).lastrowid
            conn.execute(
                "INSERT INTO models(name, cartridge_id, model_mode) VALUES (?, ?, ?)",
                (DEFAULT_MODEL_NAME, cart_id, DEFAULT_MODEL_MODE),
            )

    # ----- raw dump helper (debug) -------------------------------------------

    def dump_table(self, table: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]
