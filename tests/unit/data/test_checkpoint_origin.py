"""Checkpoint provenance: was this `.pth` trained here, or did it arrive as a file?

The torch security floor (CVE-2026-24747, `local_inference.MIN_TORCH_VERSION`)
exists for checkpoints this machine did not produce. It used to key off
`model_type`, which answers a different question — who may *train* the model —
and which, for a plain ZIP import, comes from the archive's own manifest. So a
third-party archive could say `Standard` and turn the floor into a declinable
offer. `checkpoint_origin` is the machine-local answer, set only by the code
that writes the checkpoint.

Existing users must see no change: every row present when migration 0008 runs
keeps the old behaviour (`"local"`).
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sorter.data.db import Database
from sorter.data.model_io import import_model, model_from_export_dict, model_to_export_dict
from sorter.data.models import (
    CHECKPOINT_ORIGIN_IMPORTED,
    CHECKPOINT_ORIGIN_LOCAL,
    Model,
    needs_torch_floor,
    normalize_checkpoint_origin,
)
from sorter.data.repository import CartridgeRepo, ModelRepo

from ._legacy_db import SCHEMA_SHAPES, write_db_at_version


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "origin.db")
    db.ensure_initialized()
    return db


def _archive(
    tmp_path: Path,
    *,
    uid: str | None = None,
    with_checkpoint: bool = True,
    model_type: str = "Standard",
    filename: str = "m.zip",
    extra_info: dict | None = None,
) -> Path:
    zip_path = tmp_path / filename
    info = {"Name": "Gift", "ModelMode": 8, "ModelType": model_type, **(extra_info or {})}
    if uid:
        info["CommunityModelUID"] = uid
    manifest = {"ModelName": "Gift", "CartridgeName": "9mm", "Headstamps": ["FC"], "ModelInfo": info}
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        if with_checkpoint:
            zf.writestr("model/x.pth", b"CHECKPOINT")
        zf.writestr("images/FC__1.jpg", b"\xff\xd8jpeg")
    return zip_path


def _get(db: Database, model_id: int) -> Model:
    model = ModelRepo(db).get(model_id)
    assert model is not None
    return model


# ----- the predicate -----------------------------------------------------------


def test_only_a_locally_trained_checkpoint_skips_the_floor() -> None:
    assert needs_torch_floor(Model(model_type="Standard", checkpoint_origin="local")) is False
    assert needs_torch_floor(Model(model_type="Standard", checkpoint_origin="imported")) is True
    assert needs_torch_floor(Model(model_type="CommunityManaged", checkpoint_origin="local")) is True
    assert needs_torch_floor(Model(model_type="ReadOnly")) is True
    # Unknown provenance is the cautious branch, as it always was in the gate.
    assert needs_torch_floor(None) is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "local"),
        ("", "local"),
        ("local", "local"),
        ("imported", "imported"),
        # Something newer or hand-edited reads as the cautious value.
        ("from-the-future", "imported"),
    ],
)
def test_stored_values_normalize(raw: object, expected: str) -> None:
    assert normalize_checkpoint_origin(raw) == expected


# ----- storage ----------------------------------------------------------------


def test_a_new_row_defaults_to_local_and_round_trips(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ModelRepo(db)
    cart = CartridgeRepo(db).get_or_create("9mm").id

    plain = repo.create(Model(name="Mine", cartridge_id=cart))
    assert _get(db, plain.id).checkpoint_origin == CHECKPOINT_ORIGIN_LOCAL

    plain.checkpoint_origin = CHECKPOINT_ORIGIN_IMPORTED
    repo.update(plain)
    assert _get(db, plain.id).checkpoint_origin == CHECKPOINT_ORIGIN_IMPORTED


@pytest.mark.parametrize("version", sorted(SCHEMA_SHAPES))
def test_existing_models_keep_todays_behaviour_after_the_migration(tmp_path: Path, version: int) -> None:
    """The backfill decision: a row that predates the column is `local`, so an
    existing install's own models are not suddenly hard-blocked on an old torch."""
    db_path = tmp_path / "legacy.db"
    write_db_at_version(db_path, version)

    db = Database(db_path)
    db.ensure_initialized()

    (model,) = ModelRepo(db).list()
    assert model.checkpoint_origin == CHECKPOINT_ORIGIN_LOCAL
    assert needs_torch_floor(model) is False
    with sqlite3.connect(db_path) as raw:
        (stored,) = raw.execute("SELECT checkpoint_origin FROM models").fetchone()
    assert stored == "local"


# ----- what sets it -------------------------------------------------------------


def test_a_plain_zip_import_is_owned_but_its_checkpoint_is_imported(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _, model_id = import_model(
        _archive(tmp_path), db=db, images_target_dir=tmp_path / "i", models_target_dir=tmp_path / "m"
    )

    model = _get(db, model_id)
    # Still trainable: this may be the user's own model on a new machine.
    assert model.model_type == "Standard"
    assert model.checkpoint_origin == CHECKPOINT_ORIGIN_IMPORTED
    assert needs_torch_floor(model) is True


def test_a_manifest_cannot_vouch_for_its_own_checkpoint(tmp_path: Path) -> None:
    db = _db(tmp_path)
    zip_path = _archive(tmp_path, extra_info={"checkpoint_origin": "local", "CheckpointOrigin": "local"})

    _, model_id = import_model(zip_path, db=db, images_target_dir=tmp_path / "i", models_target_dir=tmp_path / "m")

    assert _get(db, model_id).checkpoint_origin == CHECKPOINT_ORIGIN_IMPORTED


def test_the_origin_is_never_exported() -> None:
    exported = model_to_export_dict(Model(name="Mine", checkpoint_origin="local"))
    assert "checkpoint_origin" not in exported
    assert model_from_export_dict(exported).checkpoint_origin == CHECKPOINT_ORIGIN_LOCAL


def test_an_images_only_update_keeps_the_installed_checkpoints_origin(tmp_path: Path) -> None:
    """Only replacing the checkpoint changes where it came from."""
    db = _db(tmp_path)
    repo = ModelRepo(db)
    cart = CartridgeRepo(db).get_or_create("9mm").id
    trained = tmp_path / "trained.pth"
    trained.write_bytes(b"OWN")
    mine = repo.create(Model(name="Mine", cartridge_id=cart, community_model_uid="uid-1", model_path=str(trained)))

    import_model(
        _archive(tmp_path, uid="uid-1", with_checkpoint=False),
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )

    after = _get(db, mine.id)
    assert after.model_path == str(trained)
    assert after.checkpoint_origin == CHECKPOINT_ORIGIN_LOCAL


def test_an_update_that_ships_a_checkpoint_marks_it_imported(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ModelRepo(db)
    cart = CartridgeRepo(db).get_or_create("9mm").id
    mine = repo.create(Model(name="Mine", cartridge_id=cart, community_model_uid="uid-1"))

    import_model(
        _archive(tmp_path, uid="uid-1"),
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )

    assert _get(db, mine.id).checkpoint_origin == CHECKPOINT_ORIGIN_IMPORTED
