"""Repository facade — every SQL statement that mutates the DB lives here.

Repositories receive a `Database` instance and expose dataclass-shaped CRUD.
UI code never touches `sqlite3` directly.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from typing import Any

from .db import Database
from .models import (
    MODEL_MODES,
    SLOT_TEMPLATE_MODES,
    Cartridge,
    Headstamp,
    HeadstampParent,
    Model,
    SlotTemplate,
)


def _require_rowid(cursor: sqlite3.Cursor) -> int:
    """`Cursor.lastrowid` is typed `int | None` in the stdlib stubs because it's
    `None` before any statement runs, or after one that isn't an INSERT — but a
    successful `INSERT` against one of this module's rowid tables always sets
    it. `None` here would mean the statement above didn't actually insert a
    row, which is a programming error in this module, not a condition callers
    should have to handle.
    """
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT did not return a row id")
    return cursor.lastrowid


class CartridgeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # Return-annotated `builtins.list[...]` rather than bare `list[...]`: a
    # method literally named `list` shadows the builtin within this class body
    # for every annotation written after it, including its own.
    def list(self) -> builtins.list[Cartridge]:
        rows = self.db.conn.execute("SELECT id, name FROM cartridges ORDER BY name COLLATE NOCASE").fetchall()
        return [Cartridge.from_row(r) for r in rows]

    def get(self, cartridge_id: int) -> Cartridge | None:
        row = self.db.conn.execute("SELECT id, name FROM cartridges WHERE id = ?", (cartridge_id,)).fetchone()
        return Cartridge.from_row(row) if row else None

    def find_by_name(self, name: str) -> Cartridge | None:
        row = self.db.conn.execute("SELECT id, name FROM cartridges WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return Cartridge.from_row(row) if row else None

    def create(self, name: str) -> Cartridge:
        cur = self.db.conn.execute("INSERT INTO cartridges(name) VALUES (?)", (name,))
        return Cartridge(id=_require_rowid(cur), name=name)

    def get_or_create(self, name: str) -> Cartridge:
        existing = self.find_by_name(name)
        if existing is not None:
            return existing
        return self.create(name)

    def rename(self, cartridge_id: int, new_name: str) -> None:
        self.db.conn.execute("UPDATE cartridges SET name = ? WHERE id = ?", (new_name, cartridge_id))

    def delete(self, cartridge_id: int) -> None:
        # Will raise IntegrityError if any models still reference this cartridge.
        self.db.conn.execute("DELETE FROM cartridges WHERE id = ?", (cartridge_id,))


class ModelRepo:
    """CRUD for models, plus active-model selection.

    Active-model invariants enforced here:
      - delete refuses to remove the last model of a cartridge
      - delete refuses to remove the active model unless a replacement is given
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- read ----------------------------------------------------------------

    # Return-annotated `builtins.list[...]` rather than bare `list[...]`: a
    # method literally named `list` shadows the builtin within this class body
    # for every annotation written after it, including its own.
    def list(self) -> builtins.list[Model]:
        rows = self.db.conn.execute("SELECT * FROM models ORDER BY name COLLATE NOCASE").fetchall()
        return [Model.from_row(r) for r in rows]

    def list_by_cartridge(self, cartridge_id: int) -> builtins.list[Model]:
        rows = self.db.conn.execute(
            "SELECT * FROM models WHERE cartridge_id = ? ORDER BY name COLLATE NOCASE",
            (cartridge_id,),
        ).fetchall()
        return [Model.from_row(r) for r in rows]

    def get(self, model_id: int) -> Model | None:
        row = self.db.conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        return Model.from_row(row) if row else None

    def find_by_community_uid(self, uid: str) -> Model | None:
        """The installed copy of a community model.

        A library can hold more than one row for a UID — earlier versions of
        the app imported every community update as a new model instead of
        updating the installed one. Resolve that deterministically: the
        active model wins (it's the one being sorted with), otherwise the
        oldest, so an update and the Community tab's installed/update badge
        always agree on which row they mean.
        """
        row = self.db.conn.execute(
            """
            SELECT m.* FROM models m
            LEFT JOIN settings s ON s.key = 'default_model_id'
            WHERE m.community_model_uid = ?
            ORDER BY (CAST(s.value AS INTEGER) = m.id) DESC, m.id ASC
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
        return Model.from_row(row) if row else None

    def count_in_cartridge(self, cartridge_id: int) -> int:
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM models WHERE cartridge_id = ?",
            (cartridge_id,),
        ).fetchone()[0]

    # ---- write ---------------------------------------------------------------

    def create(self, model: Model) -> Model:
        if model.model_mode not in MODEL_MODES:
            raise ValueError(f"Unsupported model_mode: {model.model_mode!r}")
        cur = self.db.conn.execute(
            """
            INSERT INTO models(
                name, cartridge_id, model_mode, model_type, community_model_uid,
                model_version, enable_image_processing, image_processing_json,
                training_config_json, ai_model_config_json,
                use_primer_mask, hide_primer, primer_mask_size,
                last_training_date, last_training_duration, trained_image_count,
                training_confusion_table, feedback_loop_enabled,
                feedback_loop_confidence_floor, feedback_loop_upload_mode, model_path,
                checkpoint_env_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                model.name,
                model.cartridge_id,
                model.model_mode,
                model.model_type,
                model.community_model_uid,
                model.model_version,
                int(model.enable_image_processing),
                json.dumps(model.image_processing.to_dict()),
                json.dumps(model.training_config.to_dict()),
                json.dumps(model.ai_model_config.to_dict()),
                int(model.use_primer_mask),
                int(model.hide_primer),
                model.primer_mask_size,
                model.last_training_date,
                model.last_training_duration,
                model.trained_image_count,
                model.training_confusion_table,
                int(model.feedback_loop_enabled),
                model.feedback_loop_confidence_floor,
                model.feedback_loop_upload_mode,
                model.model_path,
                json.dumps(model.checkpoint_env.to_dict()),
            ),
        )
        model.id = _require_rowid(cur)
        return model

    def update(self, model: Model) -> None:
        if not model.id:
            raise ValueError("Cannot update a model with no id")
        if model.model_mode not in MODEL_MODES:
            raise ValueError(f"Unsupported model_mode: {model.model_mode!r}")
        self.db.conn.execute(
            """
            UPDATE models SET
                name = ?, cartridge_id = ?, model_mode = ?, model_type = ?,
                community_model_uid = ?, model_version = ?,
                enable_image_processing = ?, image_processing_json = ?,
                training_config_json = ?, ai_model_config_json = ?,
                use_primer_mask = ?, hide_primer = ?, primer_mask_size = ?,
                last_training_date = ?, last_training_duration = ?,
                trained_image_count = ?, training_confusion_table = ?,
                feedback_loop_enabled = ?, feedback_loop_confidence_floor = ?,
                feedback_loop_upload_mode = ?, model_path = ?,
                checkpoint_env_json = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                model.name,
                model.cartridge_id,
                model.model_mode,
                model.model_type,
                model.community_model_uid,
                model.model_version,
                int(model.enable_image_processing),
                json.dumps(model.image_processing.to_dict()),
                json.dumps(model.training_config.to_dict()),
                json.dumps(model.ai_model_config.to_dict()),
                int(model.use_primer_mask),
                int(model.hide_primer),
                model.primer_mask_size,
                model.last_training_date,
                model.last_training_duration,
                model.trained_image_count,
                model.training_confusion_table,
                int(model.feedback_loop_enabled),
                model.feedback_loop_confidence_floor,
                model.feedback_loop_upload_mode,
                model.model_path,
                json.dumps(model.checkpoint_env.to_dict()),
                model.id,
            ),
        )

    def delete(self, model_id: int, *, replacement_active_id: int | None = None) -> None:
        """Delete a model. Refuses when:

        - the model is the last one in its cartridge
        - the model is currently active and no `replacement_active_id` is given
        """
        existing = self.get(model_id)
        if existing is None:
            return
        siblings = self.count_in_cartridge(existing.cartridge_id)
        if siblings <= 1:
            raise ValueError("Cannot delete the last model in a cartridge. Add another model first.")
        settings_repo = SettingsRepo(self.db)
        if settings_repo.get_active_model_id() == model_id:
            if replacement_active_id is None:
                raise ValueError("Cannot delete the active model. Activate another model first.")
            replacement = self.get(replacement_active_id)
            if replacement is None or replacement.id == model_id:
                raise ValueError("Replacement model not found.")
            settings_repo.set_active_model_id(replacement.id)
        self.db.conn.execute("DELETE FROM models WHERE id = ?", (model_id,))


class HeadstampRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_for_model(self, model_id: int) -> list[Headstamp]:
        rows = self.db.conn.execute(
            "SELECT id, name, model_id, slot, parent_id FROM headstamps "
            "WHERE model_id = ? ORDER BY name COLLATE NOCASE",
            (model_id,),
        ).fetchall()
        return [Headstamp.from_row(r) for r in rows]

    def add(self, model_id: int, name: str, slot: int = 0) -> Headstamp:
        cur = self.db.conn.execute(
            "INSERT INTO headstamps(name, model_id, slot) VALUES (?, ?, ?)",
            (name, model_id, slot),
        )
        return Headstamp(id=_require_rowid(cur), name=name, model_id=model_id, slot=slot)

    def update_slot(self, headstamp_id: int, slot: int) -> None:
        self.db.conn.execute("UPDATE headstamps SET slot = ? WHERE id = ?", (slot, headstamp_id))

    def set_parent(self, headstamp_id: int, parent_id: int | None) -> None:
        """Assign (or clear, with ``None``) a headstamp's parent classification."""
        self.db.conn.execute(
            "UPDATE headstamps SET parent_id = ? WHERE id = ?",
            (parent_id, headstamp_id),
        )

    def rename(self, headstamp_id: int, new_name: str) -> None:
        self.db.conn.execute("UPDATE headstamps SET name = ? WHERE id = ?", (new_name, headstamp_id))

    def delete(self, headstamp_id: int) -> None:
        self.db.conn.execute("DELETE FROM headstamps WHERE id = ?", (headstamp_id,))

    def clear_for_model(self, model_id: int) -> None:
        self.db.conn.execute("DELETE FROM headstamps WHERE model_id = ?", (model_id,))

    def replace_for_model(self, model_id: int, entries: list[dict[str, Any]]) -> None:
        """Atomically replace the headstamp set for a model."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM headstamps WHERE model_id = ?", (model_id,))
            for entry in entries:
                name = entry.get("name")
                if not name:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO headstamps(name, model_id, slot) VALUES (?, ?, ?)",
                    (name, model_id, int(entry.get("slot", 0))),
                )


class HeadstampParentRepo:
    """CRUD for parent classifications (the groups child headstamps roll into).

    Parents are scoped per-model. Deleting a parent relies on the
    ``ON DELETE SET NULL`` foreign key to unlink its children rather than
    deleting them.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def list_for_model(self, model_id: int) -> list[HeadstampParent]:
        rows = self.db.conn.execute(
            "SELECT id, name, model_id, slot FROM headstamp_parents WHERE model_id = ? ORDER BY name COLLATE NOCASE",
            (model_id,),
        ).fetchall()
        return [HeadstampParent.from_row(r) for r in rows]

    def get(self, parent_id: int) -> HeadstampParent | None:
        row = self.db.conn.execute(
            "SELECT id, name, model_id, slot FROM headstamp_parents WHERE id = ?",
            (parent_id,),
        ).fetchone()
        return HeadstampParent.from_row(row) if row else None

    def find_by_name(self, model_id: int, name: str) -> HeadstampParent | None:
        row = self.db.conn.execute(
            "SELECT id, name, model_id, slot FROM headstamp_parents WHERE model_id = ? AND name = ? COLLATE NOCASE",
            (model_id, name),
        ).fetchone()
        return HeadstampParent.from_row(row) if row else None

    def add(self, model_id: int, name: str) -> HeadstampParent:
        cur = self.db.conn.execute(
            "INSERT INTO headstamp_parents(name, model_id) VALUES (?, ?)",
            (name, model_id),
        )
        return HeadstampParent(id=_require_rowid(cur), name=name, model_id=model_id)

    def rename(self, parent_id: int, new_name: str) -> None:
        self.db.conn.execute(
            "UPDATE headstamp_parents SET name = ? WHERE id = ?",
            (new_name, parent_id),
        )

    def update_slot(self, parent_id: int, slot: int) -> None:
        """Set the physical bin a parent routes to in parent-classification mode."""
        self.db.conn.execute("UPDATE headstamp_parents SET slot = ? WHERE id = ?", (slot, parent_id))

    def delete(self, parent_id: int) -> None:
        # Children are unlinked via ON DELETE SET NULL; also clear explicitly so
        # the result is correct even if foreign keys are disabled on this
        # connection.
        self.db.conn.execute(
            "UPDATE headstamps SET parent_id = NULL WHERE parent_id = ?",
            (parent_id,),
        )
        self.db.conn.execute("DELETE FROM headstamp_parents WHERE id = ?", (parent_id,))


class SlotTemplateRepo:
    """CRUD for named slot-assignment layouts (see ``models.SlotTemplate``).

    Every method is scoped by ``(model_id, mode)`` — ``model_id=None`` is AI
    Config mode, and 'standard'/'package' never share a list.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def list_for_scope(self, model_id: int | None, mode: str) -> list[SlotTemplate]:
        rows = self.db.conn.execute(
            "SELECT * FROM slot_templates WHERE model_id IS ? AND mode = ? ORDER BY name COLLATE NOCASE",
            (model_id, mode),
        ).fetchall()
        return [SlotTemplate.from_row(r) for r in rows]

    def get(self, template_id: int) -> SlotTemplate | None:
        row = self.db.conn.execute("SELECT * FROM slot_templates WHERE id = ?", (template_id,)).fetchone()
        return SlotTemplate.from_row(row) if row else None

    def find_by_name(self, model_id: int | None, mode: str, name: str) -> SlotTemplate | None:
        row = self.db.conn.execute(
            "SELECT * FROM slot_templates WHERE model_id IS ? AND mode = ? AND name = ? COLLATE NOCASE",
            (model_id, mode, name),
        ).fetchone()
        return SlotTemplate.from_row(row) if row else None

    def count_for_scope(self, model_id: int | None, mode: str) -> int:
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM slot_templates WHERE model_id IS ? AND mode = ?",
            (model_id, mode),
        ).fetchone()[0]

    def create(
        self,
        model_id: int | None,
        mode: str,
        name: str,
        assignments: dict[str, Any] | None = None,
    ) -> SlotTemplate:
        if mode not in SLOT_TEMPLATE_MODES:
            raise ValueError(f"Unsupported slot-template mode: {mode!r}")
        payload = assignments or {}
        cur = self.db.conn.execute(
            "INSERT INTO slot_templates(model_id, mode, name, assignments_json) VALUES (?, ?, ?, ?)",
            (model_id, mode, name, json.dumps(payload)),
        )
        return SlotTemplate(
            id=_require_rowid(cur),
            model_id=model_id,
            mode=mode,
            name=name,
            assignments=payload,
        )

    def rename(self, template_id: int, new_name: str) -> None:
        self.db.conn.execute(
            "UPDATE slot_templates SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (new_name, template_id),
        )

    def update_assignments(self, template_id: int, assignments: dict[str, Any]) -> None:
        self.db.conn.execute(
            "UPDATE slot_templates SET assignments_json = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(assignments or {}), template_id),
        )

    def delete(self, template_id: int) -> None:
        self.db.conn.execute("DELETE FROM slot_templates WHERE id = ?", (template_id,))


class SettingsRepo:
    """JSON-encoded key/value store for app-level settings."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        self.db.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ---- active model helpers ------------------------------------------------

    def get_active_model_id(self) -> int | None:
        """Returns None when running in AI Config mode (no active local model)."""
        v = self.get("default_model_id")
        return int(v) if v is not None else None

    def set_active_model_id(self, model_id: int) -> None:
        self.set("default_model_id", int(model_id))

    def clear_active_model(self) -> None:
        """Switch to AI Config mode by removing the active-model setting."""
        self.delete("default_model_id")
