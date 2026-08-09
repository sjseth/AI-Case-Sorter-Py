"""Ownership: which models this user may train.

A community download is the publisher's model — the local checkpoint is a
copy. Training it here forks it from the version they keep updating, and the
archive usually ships without the images it was built from. So downloads are
marked `CommunityManaged` at import and the Train tab hides for them.

The subtle part is what ownership is *not* keyed on: `community_model_uid`.
Sharing your own model stamps a UID onto your local copy, so a UID means
"this model exists in the community", not "this model isn't yours".
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from sorter.db import Database
from sorter.model_io import import_model
from sorter.models import Model, is_foreign_model, is_trainable
from sorter.repository import ModelRepo


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "own.db")
    db.ensure_initialized()
    return db


def _archive(
    tmp_path: Path,
    name: str = "Shared9mm",
    uid: str = "uid-1",
    model_type: str = "Standard",
    filename: str = "pub.zip",
) -> Path:
    """A share archive as a publisher would produce it — ModelType Standard,
    because that's what the model is on *their* machine."""
    zip_path = tmp_path / filename
    manifest = {
        "ModelName": name,
        "CartridgeName": "9mm",
        "Headstamps": ["FC", "WIN"],
        "ExportMode": 1,
        "ModelInfo": {
            "Name": name,
            "ModelMode": "convnext_tiny",
            "ModelType": model_type,
            "CommunityModelUID": uid,
            "ModelVersion": 2,
        },
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("model/pub.pth", b"CHECKPOINT")
    return zip_path


# ----- the predicates -------------------------------------------------------


def test_a_locally_authored_model_is_trainable() -> None:
    assert is_trainable(Model(name="Mine"))
    assert not is_foreign_model(Model(name="Mine"))


def test_a_model_you_shared_stays_trainable() -> None:
    """Sharing stamps a UID on your own copy — it must not cost you training."""
    mine = Model(name="Mine", community_model_uid="uid-abc", model_type="Standard")
    assert is_trainable(mine)
    assert not is_foreign_model(mine)


def test_a_community_download_is_not_trainable() -> None:
    assert not is_trainable(Model(name="Theirs", model_type="CommunityManaged"))
    assert is_foreign_model(Model(name="Theirs", model_type="CommunityManaged"))


def test_legacy_readonly_models_are_not_trainable() -> None:
    assert not is_trainable(Model(name="Old", model_type="ReadOnly"))


def test_ai_config_mode_is_not_trainable() -> None:
    """No active model at all — the Train tab hides for this too."""
    assert not is_trainable(None)


# ----- import stamping ------------------------------------------------------


def test_community_download_is_marked_not_owned(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    _cart, model_id = import_model(
        _archive(tmp_path),
        db=db,
        community_download=True,
    )
    saved = ModelRepo(db).get(model_id)
    assert saved is not None
    # The manifest said Standard; how it arrived is what counts.
    assert saved.model_type == "CommunityManaged"
    assert not is_trainable(saved)


def test_plain_zip_import_stays_owned(tmp_path: Path) -> None:
    """Importing a ZIP is just as likely to be restoring your own model."""
    db = _seed_db(tmp_path)
    _cart, model_id = import_model(_archive(tmp_path), db=db)
    saved = ModelRepo(db).get(model_id)
    assert saved is not None
    assert saved.model_type == "Standard"
    assert is_trainable(saved)


def test_updating_a_download_keeps_it_not_owned(tmp_path: Path) -> None:
    """A newer version of an installed community model stays read-only."""
    db = _seed_db(tmp_path)
    _cart, model_id = import_model(
        _archive(tmp_path),
        db=db,
        community_download=True,
    )
    _cart2, updated_id = import_model(
        _archive(tmp_path, filename="v2.zip"),
        db=db,
        community_download=True,
    )
    assert updated_id == model_id  # updated in place, not duplicated
    assert not is_trainable(ModelRepo(db).get(model_id))


def test_a_plain_zip_cannot_launder_a_download_into_an_owned_model(
    tmp_path: Path,
) -> None:
    """The loophole: re-import the same archive from disk, without the
    community flag, and the manifest's `Standard` would overwrite the
    installed `CommunityManaged`."""
    db = _seed_db(tmp_path)
    _cart, model_id = import_model(
        _archive(tmp_path),
        db=db,
        community_download=True,
    )
    import_model(_archive(tmp_path, filename="again.zip"), db=db)
    assert not is_trainable(ModelRepo(db).get(model_id))
