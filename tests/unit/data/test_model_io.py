"""Tests for ZIP model import/export and backslash normalization."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from sorter.data.db import Database
from sorter.data.model_io import (
    ExportMode,
    export_model,
    find_update_target,
    import_model,
    model_from_export_dict,
    read_manifest,
)
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, HeadstampRepo, ModelRepo, SettingsRepo


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "x.db")
    db.ensure_initialized()
    return db


def _get_model(db: Database, model_id: int) -> Model:
    """`ModelRepo.get` is `Model | None`; every call site here follows an
    import/create that is known to have produced the row."""
    m = ModelRepo(db).get(model_id)
    assert m is not None
    return m


def test_export_and_import_round_trip(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    cart = CartridgeRepo(db).create("45ACP")
    model = Model(
        name="My45",
        cartridge_id=cart.id,
        model_mode="convnext_small",
    )
    model.training_config.epochs = 33
    saved = ModelRepo(db).create(model)
    HeadstampRepo(db).add(saved.id, "FC")
    HeadstampRepo(db).add(saved.id, "WIN")

    images_dir = tmp_path / "src_images"
    images_dir.mkdir()
    (images_dir / "FC__123.jpg").write_bytes(b"fakejpg1")
    (images_dir / "WIN__456.jpg").write_bytes(b"fakejpg2")

    model_file = tmp_path / "model_src.pth"
    model_file.write_bytes(b"PYTORCHPTH")

    zip_path = tmp_path / "export.zip"
    export_model(
        zip_path,
        saved,
        cartridge_name="45ACP",
        headstamps=["FC", "WIN"],
        mode=ExportMode.MODEL_AND_IMAGES,
        model_file=model_file,
        images_dir=images_dir,
    )

    manifest = read_manifest(zip_path)
    assert manifest["ModelName"] == "My45"
    assert manifest["CartridgeName"] == "45ACP"
    assert set(manifest["Headstamps"]) == {"FC", "WIN"}

    # Import into a different DB.
    db2 = _seed_db(tmp_path / "second")
    images_target = tmp_path / "img_target"
    models_target = tmp_path / "mod_target"
    cart_id, model_id = import_model(
        zip_path,
        db=db2,
        images_target_dir=images_target,
        models_target_dir=models_target,
    )

    imported_model = ModelRepo(db2).get(model_id)
    assert imported_model is not None
    assert imported_model.model_mode == "convnext_small"
    assert imported_model.training_config.epochs == 33
    assert imported_model.model_path is not None
    assert Path(imported_model.model_path).exists()
    assert sorted(p.name for p in images_target.iterdir()) == [
        "FC__123.jpg",
        "WIN__456.jpg",
    ]
    hs_names = sorted(h.name for h in HeadstampRepo(db2).list_for_model(model_id))
    assert hs_names == ["FC", "WIN"]


def test_import_normalizes_backslash_entries(tmp_path: Path) -> None:
    """Simulate a Windows-produced ZIP with backslash separators."""
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "win.zip"

    manifest = {
        "ModelName": "WinExported",
        "CartridgeName": "9mm",
        "Headstamps": ["FOO"],
        "ExportMode": "ModelAndImages",
        "ModelInfo": {"name": "WinExported", "model_mode": "convnext_tiny"},
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        # NOTE: backslashes — what Windows-side legacy exports actually look like.
        zf.writestr("images\\FOO__100.jpg", b"jpg1")
        zf.writestr("model\\winexported.pth", b"PTH")

    img_target = tmp_path / "imgs"
    mod_target = tmp_path / "mods"
    cart_id, model_id = import_model(
        zip_path,
        db=db,
        images_target_dir=img_target,
        models_target_dir=mod_target,
    )

    assert (img_target / "FOO__100.jpg").exists()
    # Note: file is renamed to <model_id>.pth at import time.
    assert any(p.name == f"{model_id}.pth" for p in mod_target.iterdir())


def test_import_rejects_path_traversal(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "evil.zip"
    manifest = {
        "ModelName": "Evil",
        "CartridgeName": "9mm",
        "Headstamps": [],
        "ExportMode": "ModelAndImages",
        "ModelInfo": {"name": "Evil", "model_mode": "convnext_tiny"},
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("../../etc/passwd", b"oops")

    with pytest.raises(ValueError):
        import_model(zip_path, db=db, images_target_dir=tmp_path / "i", models_target_dir=tmp_path / "m")


def _import_manifest() -> dict:
    return {
        "ModelName": "M",
        "CartridgeName": "9mm",
        "Headstamps": [],
        "ExportMode": "ModelAndImages",
        "ModelInfo": {"name": "M", "model_mode": "convnext_tiny"},
    }


def test_import_rejects_decompression_bomb(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "bomb.zip"
    # ~5 MB of zeros compresses to a few KB — a ratio far above the guard.
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(_import_manifest()))
        zf.writestr("images/BOOM__1.jpg", b"\x00" * (5 * 1024 * 1024))

    with pytest.raises(ValueError, match="compression ratio"):
        import_model(zip_path, db=db, images_target_dir=tmp_path / "i", models_target_dir=tmp_path / "m")


def test_import_rejects_unexpected_entry_extension(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "weird.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_import_manifest()))
        zf.writestr("images/notanimage.exe", b"MZ")

    with pytest.raises(ValueError, match="unexpected image entry"):
        import_model(zip_path, db=db, images_target_dir=tmp_path / "i", models_target_dir=tmp_path / "m")


def test_export_atomic_write_no_partial_file(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    cart = CartridgeRepo(db).create("test")
    model = ModelRepo(db).create(Model(name="x", cartridge_id=cart.id, model_mode="convnext_tiny"))

    out = tmp_path / "export.zip"
    export_model(
        out,
        model,
        cartridge_name="test",
        headstamps=[],
        mode=ExportMode.MODEL_ONLY,
    )
    assert out.exists()
    # No leftover .tmp from atomic-write
    assert not out.with_suffix(out.suffix + ".tmp").exists()


def test_unknown_model_mode_in_manifest_falls_back_to_tiny(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "future.zip"
    manifest = {
        "ModelName": "Future",
        "CartridgeName": "9mm",
        "Headstamps": [],
        "ExportMode": "ModelOnly",
        "ModelInfo": {"name": "Future", "model_mode": "future_arch"},
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))

    _, mid = import_model(
        zip_path,
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )
    assert _get_model(db, mid).model_mode == "convnext_tiny"


def test_winforms_pascal_manifest_picks_up_training_config(tmp_path: Path) -> None:
    """A legacy export uses PascalCase keys and ints for the ModelMode enum.
    This app must pull the training image size, model architecture, primer
    settings, and AI endpoint out of that manifest — otherwise an imported
    community model classifies at the wrong resolution and predictions look
    random."""
    # Synthesize a legacy-shaped manifest dict (no real zip needed).
    payload = {
        "Name": "Community9mm",
        "ModelMode": 7,  # ConvNeXtSmall
        "ModelType": 0,  # Standard
        "ModelVersion": 3,
        "CommunityModelUID": "abc-123",
        "EnableImageProcessing": True,
        "UsePrimerMask": True,
        "HidePrimer": False,
        "PrimerMaskSize": 142,
        "FeedbackLoopEnabled": True,
        "FeedbackLoopConfidenceFloor": 88,
        "FeedbackLoopUploadMode": "OnRunComplete",
        "PythonTrainingConfig": {
            "ModelName": "convnext_small",
            "Epochs": 12,
            "LearningRate": 0.0002,
            "BatchSize": 8,
            "ValSplit": 0.2,
            "WeightDecay": 0.0001,
            "MaxWorkers": 0,
            "ImageSize": 480,
            "TrainAll": False,
            "DropoutRate": 0.0,
            "UseSWA": True,
            "SWAStart": 0.8,
            "SWAMode": "adaptive",
        },
        "AIModelConfig": {
            "OpenAI_EndpointUrl": "https://example.test",
            "OpenAI_Model": "gpt-something",
            "OpenAI_SystemPrompt": "classify pls",
            "ImageQuality": 75,
            "ImageScale": 90,
        },
    }
    m = model_from_export_dict(payload)
    # Architecture detected from ModelMode int
    assert m.model_mode == "convnext_small"
    assert m.model_type == "Standard"
    assert m.model_version == 3
    assert m.community_model_uid == "abc-123"
    # Per-model primer settings flow through.
    assert m.use_primer_mask is True
    assert m.hide_primer is False
    assert m.primer_mask_size == 142
    # The critical one: trained image size makes it into training_config so
    # inference can resize to match what the model expects.
    assert m.training_config.image_size == 480
    assert m.training_config.epochs == 12
    assert m.training_config.model_name == "convnext_small"
    assert m.training_config.use_swa is True
    assert m.training_config.swa_start == pytest.approx(0.8)
    # AI endpoint settings copied across.
    assert m.ai_model_config.endpoint_url == "https://example.test"
    assert m.ai_model_config.model == "gpt-something"
    assert m.ai_model_config.prompt == "classify pls"
    assert m.ai_model_config.image_quality == 75
    assert m.ai_model_config.image_scale == 90
    # Feedback loop
    assert m.feedback_loop_enabled is True
    assert m.feedback_loop_confidence_floor == 88
    assert m.feedback_loop_upload_mode == "OnRunComplete"


def test_normalize_upload_mode_handles_int_string_and_missing() -> None:
    from sorter.data.models import normalize_upload_mode

    # legacy enum int (Instant=0, OnRunComplete=1, Manual=2)
    assert normalize_upload_mode(0, feedback_enabled=True) == "Instant"
    assert normalize_upload_mode(1, feedback_enabled=True) == "OnRunComplete"
    assert normalize_upload_mode(2, feedback_enabled=True) == "Manual"
    # Stringified int (TEXT column may store "0") self-heals too.
    assert normalize_upload_mode("0", feedback_enabled=True) == "Instant"
    # Case-insensitive string match
    assert normalize_upload_mode("instant", feedback_enabled=True) == "Instant"
    assert normalize_upload_mode("OnRunComplete", feedback_enabled=False) == "OnRunComplete"
    # Missing / unknown → publisher default Instant when enabled, else Manual
    assert normalize_upload_mode(None, feedback_enabled=True) == "Instant"
    assert normalize_upload_mode(None, feedback_enabled=False) == "Manual"
    assert normalize_upload_mode("garbage", feedback_enabled=True) == "Instant"
    # bool must not be treated as an int index
    assert normalize_upload_mode(True, feedback_enabled=False) == "Manual"


def test_model_from_row_normalizes_legacy_int_upload_mode(tmp_path: Path) -> None:
    """A model row that stored the raw enum int (e.g. '0') reads back as the
    canonical name, so the upload-mode comparisons work without re-import."""
    db = _seed_db(tmp_path)
    cart = CartridgeRepo(db).create("9mmX")
    m = ModelRepo(db).create(
        Model(
            name="Comm",
            cartridge_id=cart.id,
            community_model_uid="uid-7",
            feedback_loop_enabled=True,
            feedback_loop_confidence_floor=97,
        )
    )
    # Simulate a legacy row that persisted the enum int.
    db.conn.execute("UPDATE models SET feedback_loop_upload_mode = '0' WHERE id = ?", (m.id,))
    reloaded = _get_model(db, m.id)
    assert reloaded.feedback_loop_upload_mode == "Instant"


def test_manifest_with_int_upload_mode_imports_as_name() -> None:
    """A legacy community export serializes the enum as an int (Instant=0);
    model_from_export_dict must store the canonical name, not the raw int."""
    m = model_from_export_dict(
        {
            "ModelName": "Comm",
            "CommunityModelUID": "uid-9",
            "FeedbackLoopEnabled": True,
            "FeedbackLoopConfidenceFloor": 90,
            "FeedbackLoopUploadMode": 0,
        }
    )
    assert m.feedback_loop_enabled is True
    assert m.feedback_loop_confidence_floor == 90
    assert m.feedback_loop_upload_mode == "Instant"


def test_model_mode_normalization_accepts_misc_spellings() -> None:
    from sorter.data.model_io import _normalize_model_mode

    # Snake case (this app's export)
    assert _normalize_model_mode("convnext_tiny") == "convnext_tiny"
    # legacy display string variants
    assert _normalize_model_mode("ConvNeXt-Large") == "convnext_large"
    assert _normalize_model_mode("ConvNeXtBase") == "convnext_base"
    # legacy enum int
    assert _normalize_model_mode(8) == "convnext_tiny"
    assert _normalize_model_mode(3) == "convnext_large"
    # Unknown / garbage → safe default
    assert _normalize_model_mode(None) == "convnext_tiny"
    assert _normalize_model_mode("not_a_model") == "convnext_tiny"


def test_export_for_share_writes_zip_and_manifest_sidecar(tmp_path: Path) -> None:
    from sorter.data.model_io import export_for_share

    db = _seed_db(tmp_path)
    cart = CartridgeRepo(db).get_or_create("9mm")
    saved = ModelRepo(db).create(Model(name="Share9", cartridge_id=cart.id))
    HeadstampRepo(db).add(saved.id, "CBC")

    images_dir = tmp_path / "imgs"
    images_dir.mkdir()
    (images_dir / "CBC__1.jpg").write_bytes(b"img")
    model_file = tmp_path / "m.pth"
    model_file.write_bytes(b"PTH")

    zip_out = tmp_path / "abc123.zip"
    zip_path, manifest_path = export_for_share(
        zip_out,
        saved,
        "9mm",
        ["CBC"],
        mode=ExportMode.MODEL_AND_IMAGES,
        model_file=model_file,
        images_dir=images_dir,
        community_uid="abc123",
        feedback_enabled=True,
        feedback_floor=88,
    )
    assert zip_path.exists()
    # Sidecar sits next to the zip with the .manifest.json extension.
    assert manifest_path == tmp_path / "abc123.manifest.json"
    assert manifest_path.exists()

    # The sidecar mirrors the in-zip manifest exactly.
    in_zip = read_manifest(zip_path)
    sidecar = json.loads(manifest_path.read_text())
    assert sidecar == in_zip

    # Community fields are stamped into ModelInfo.
    mi = in_zip["ModelInfo"]
    assert mi["community_model_uid"] == "abc123"
    assert mi["feedback_loop_enabled"] is True
    assert mi["feedback_loop_confidence_floor"] == 88
    assert mi["feedback_loop_upload_mode"] == "Instant"
    # Exporting did not mutate the caller's model.
    assert saved.community_model_uid is None


def test_write_manifest_sidecar_roundtrips(tmp_path: Path) -> None:
    from sorter.data.model_io import write_manifest_sidecar

    db = _seed_db(tmp_path)
    cart = CartridgeRepo(db).get_or_create("9mm")
    saved = ModelRepo(db).create(Model(name="M", cartridge_id=cart.id))
    zip_out = tmp_path / "x.zip"
    export_model(zip_out, saved, "9mm", [], mode=ExportMode.MODEL_ONLY)
    sidecar = write_manifest_sidecar(zip_out)
    assert sidecar == tmp_path / "x.manifest.json"
    assert json.loads(sidecar.read_text()) == read_manifest(zip_out)


def test_import_accepts_legacy_trainedmodel_zip(tmp_path: Path) -> None:
    """Community downloads use model/trainedmodel.zip for a PyTorch archive."""
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "community.zip"
    checkpoint = b"PK\x03\x04fake-pytorch-checkpoint"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_import_manifest()))
        zf.writestr("model/trainedmodel.zip", checkpoint)

    mod_target = tmp_path / "mods"
    _, model_id = import_model(
        zip_path,
        db=db,
        images_target_dir=tmp_path / "imgs",
        models_target_dir=mod_target,
    )

    imported = mod_target / f"{model_id}.pth"
    assert imported.read_bytes() == checkpoint
    assert _get_model(db, model_id).model_path == str(imported)


def test_import_rejects_arbitrary_model_zip(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    zip_path = tmp_path / "unexpected.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_import_manifest()))
        zf.writestr("model/payload.zip", b"not a supported checkpoint name")

    with pytest.raises(ValueError, match="unexpected model entry"):
        import_model(
            zip_path,
            db=db,
            images_target_dir=tmp_path / "imgs",
            models_target_dir=tmp_path / "mods",
        )


# ----- community model updates -------------------------------------------------


def _community_zip(
    path: Path,
    *,
    uid: str = "comm-uid-1",
    version: int = 1,
    name: str = "Community 9mm",
    headstamps: tuple[str, ...] = ("FC", "WIN"),
    checkpoint: bytes = b"CHECKPOINT-V1",
) -> Path:
    """A community-published archive: the shape `import_model` sees on update."""
    manifest = {
        "ModelName": name,
        "CartridgeName": "9mm",
        "Headstamps": list(headstamps),
        "ExportMode": "ModelOnly",
        "ModelInfo": {
            "name": name,
            "model_mode": "convnext_tiny",
            "community_model_uid": uid,
            "model_version": version,
            "trained_image_count": 100 * version,
            "feedback_loop_enabled": True,
            "feedback_loop_upload_mode": "Instant",
        },
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("model/trainedmodel.zip", checkpoint)
    return path


def test_community_update_replaces_model_in_place(tmp_path: Path) -> None:
    """A newer archive of an installed community model must refresh that model,
    not add a second copy to the library — which is what leaves the Community
    tab still offering the update after it has been installed."""
    db = _seed_db(tmp_path)
    mods = tmp_path / "mods"
    before = len(ModelRepo(db).list())

    _, first_id = import_model(
        _community_zip(tmp_path / "v1.zip"),
        db=db,
        images_target_dir=tmp_path / "imgs",
        models_target_dir=mods,
    )
    _, second_id = import_model(
        _community_zip(
            tmp_path / "v2.zip",
            version=2,
            headstamps=("FC", "WIN", "RP"),
            checkpoint=b"CHECKPOINT-V2",
        ),
        db=db,
        images_target_dir=tmp_path / "imgs",
        models_target_dir=mods,
    )

    assert second_id == first_id
    assert len(ModelRepo(db).list()) == before + 1

    updated = _get_model(db, first_id)
    assert updated.model_version == 2
    assert updated.trained_image_count == 200
    # The checkpoint on disk is the new one, at the same path.
    assert (mods / f"{first_id}.pth").read_bytes() == b"CHECKPOINT-V2"
    assert updated.model_path == str(mods / f"{first_id}.pth")
    # New classifications from the update land alongside the existing ones.
    assert sorted(h.name for h in HeadstampRepo(db).list_for_model(first_id)) == [
        "FC",
        "RP",
        "WIN",
    ]


def test_community_update_keeps_slots_and_templates(tmp_path: Path) -> None:
    """Slot assignments and sorting templates belong to the user, not the
    publisher: an in-place update must leave both untouched."""
    from sorter.data.config import Config

    db = _seed_db(tmp_path)
    _, model_id = import_model(
        _community_zip(tmp_path / "v1.zip"),
        db=db,
        images_target_dir=tmp_path / "imgs",
        models_target_dir=tmp_path / "mods",
    )

    SettingsRepo(db).set_active_model_id(model_id)
    config = Config(db).load()
    config.set_headstamp_slot("FC", 3)
    config.set_headstamp_slot("WIN", 5)
    config.create_slot_template("Range brass")
    template_names = [t.name for t in config.list_slot_templates()]

    import_model(
        _community_zip(tmp_path / "v2.zip", version=2, headstamps=("FC", "WIN", "RP")),
        db=db,
        images_target_dir=tmp_path / "imgs",
        models_target_dir=tmp_path / "mods",
    )

    assert SettingsRepo(db).get_active_model_id() == model_id
    fresh = Config(db).load()
    assert fresh.slot_for_headstamp("FC") == 3
    assert fresh.slot_for_headstamp("WIN") == 5
    assert fresh.slot_for_headstamp("RP") == 0  # new class: catch-all
    assert [t.name for t in fresh.list_slot_templates()] == template_names
    assert fresh.active_slot_template().name == "Range brass"


def test_community_update_keeps_local_name_and_feedback_optout(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    _, model_id = import_model(
        _community_zip(tmp_path / "v1.zip"),
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )
    repo = ModelRepo(db)
    local = repo.get(model_id)
    assert local is not None
    assert local.feedback_loop_enabled is True  # the publisher offers the loop
    local.name = "My Range Model"  # …and the user renames it…
    local.feedback_loop_enabled = False  # …and opts out
    repo.update(local)

    import_model(
        _community_zip(tmp_path / "v2.zip", version=2, name="Community 9mm v2"),
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )

    # An update re-offers the loop; it must not silently opt the user back in.
    after = repo.get(model_id)
    assert after is not None
    assert after.name == "My Range Model"
    assert after.feedback_loop_enabled is False


def test_import_can_force_a_separate_copy(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    zip_path = _community_zip(tmp_path / "v1.zip")
    _, first_id = import_model(
        zip_path,
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )
    _, second_id = import_model(
        zip_path,
        db=db,
        update_existing=False,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )
    assert second_id != first_id
    assert _get_model(db, second_id).name == "Community 9mm (2)"


def test_find_update_target(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    community = _community_zip(tmp_path / "v1.zip")
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_import_manifest()))

    assert find_update_target(community, db=db) is None  # not installed yet
    assert find_update_target(plain, db=db) is None  # not a community model

    _, model_id = import_model(
        community,
        db=db,
        images_target_dir=tmp_path / "i",
        models_target_dir=tmp_path / "m",
    )
    target = find_update_target(community, db=db)
    assert target is not None and target.id == model_id
