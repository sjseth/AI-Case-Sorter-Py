"""Tests for importing a legacy WinForms ("AI Brass Sorter") installation.

Every fixture here is synthetic. The shapes are copied from a real 1.3.8
install, but nothing in the suite touches `C:\\Program Files` — see
`_write_install` for the layout being mimicked.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.models import is_trainable
from sorter.data.repository import (
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)
from sorter.data.winforms_import import (
    CONFIG_DB_NAME,
    FIRST_RUN_SEEN_KEY,
    MLNET_WARNING,
    MODEL_FAILED_WARNING,
    ImportOptions,
    candidate_install_dirs,
    find_installation,
    import_installation,
    looks_like_installation,
    mark_first_run_offered,
    should_offer_first_run,
    survey,
)

# ----- synthetic legacy install ----------------------------------------------

# A model row as the legacy app writes it: PascalCase, enum ints for
# ModelType/ModelMode, and the two separate training-config blobs.
_MODEL_OWN: dict[str, Any] = {
    "Id": 3,
    "Name": "9mm Base Model",
    "CartridgeId": 2,
    "UsePrimerMask": False,
    "PrimerMaskSize": 0,
    "HidePrimer": True,
    "EnableImageProcessing": True,
    "ImageProcessingConfig": {"imageProcessingMode": 0, "edgeFilter1": 100},
    "AIModelConfig": None,
    "PythonTrainingConfig": None,
    "ModelTrainingConfig": None,
    "UseParentClassificationsAtRuntime": False,
    "FeedbackLoopEnabled": False,
    "FeedbackLoopConfidenceFloor": 95,
    "FeedbackLoopUploadMode": 0,
    "CommunityModelUID": None,
    "ModelVersion": 1,
    "ModelType": 0,  # Standard — the user's own
    "ModelMode": 0,
}

_MODEL_COMMUNITY: dict[str, Any] = {
    "Id": 4,
    "Name": "9mm Default",
    "CartridgeId": 2,
    "UsePrimerMask": False,
    "PrimerMaskSize": 140,
    "HidePrimer": True,
    "EnableImageProcessing": True,
    "ModelTrainingConfig": {"ModelName": "convnext_small", "ImageSize": 480},
    "UseParentClassificationsAtRuntime": True,
    "FeedbackLoopEnabled": True,
    "FeedbackLoopConfidenceFloor": 97,
    "FeedbackLoopUploadMode": 0,
    "CommunityModelUID": "12e3af83-9d6c-4f6c-b045-7a984420543d",
    "ModelVersion": 16,
    "ModelType": 1,  # ReadOnly — a community download
    "ModelMode": 7,  # convnext_small
}

# ModelMode 2 is the legacy OpenAI enum: this model classified over HTTP rather
# than from a local checkpoint. Reported on PR #125 against a real install.
_MODEL_OPENAI: dict[str, Any] = {
    "Id": 5,
    "Name": "9mm over HTTP",
    "CartridgeId": 2,
    "ModelType": 0,
    "ModelMode": 2,
    "AIModelConfig": {
        "OpenAI_EndpointUrl": "http://192.168.1.9:1234/v1/chat/completions",
        "OpenAI_Model": "qwen2-vl",
    },
}

_DEFAULTS: dict[str, Any] = {
    "SlotQuantity": 8,
    "DefaultSerialPort": "COM3",
    "DefaultModelId": 4,
    "IP_Resolution": 5,
    "IP_Sensitivity": 1.0,
    "IP_Padding": 1,
    "IP_BgCliff": 25,
    "InitSettings": [
        {"Key": "sortspeed", "Value": "90"},
        {"Key": "feedsteps", "Value": "60"},
        {"Key": "cameraledlevel", "Value": "220"},
        {"Key": "notarealboardsetting", "Value": "1"},
    ],
}


def _torch_zip(path: Path) -> None:
    """A stand-in for `training/models/<id>.zip`.

    `torch.save` writes a zip container holding `<name>/data.pkl`; that entry
    is the whole signal `_checkpoint_kind` keys off, so the fixture needs
    nothing else to be classified correctly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("trainedmodel/data.pkl", b"\x80\x02}q\x00.")
        zf.writestr("trainedmodel/data/0", b"\x00" * 16)


def _mlnet_zip(path: Path) -> None:
    """The legacy ML.NET pipeline's model, which shares the `.zip` extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("TransformerChain/Model.key", b"key")
        zf.writestr("Schema", b"schema")


def _write_install(
    root: Path,
    *,
    models: list[dict[str, Any]] | None = None,
    headstamps: list[dict[str, Any]] | None = None,
    parents: list[dict[str, Any]] | None = None,
    parent_links: list[dict[str, Any]] | None = None,
    slots: list[dict[str, Any]] | None = None,
    defaults: dict[str, Any] | None = None,
    settings_json: dict[str, Any] | None = None,
) -> Path:
    """Lay down a legacy install tree under `root` and return it."""
    data = root / "Data"
    data.mkdir(parents=True, exist_ok=True)
    document = {
        "Models": models if models is not None else [_MODEL_OWN, _MODEL_COMMUNITY],
        "Cartridges": [{"Id": 2, "Name": "9mm"}],
        "Headstamps": headstamps if headstamps is not None else [],
        "HeadStampParents": parents or [],
        "HeadStampParentLinks": parent_links or [],
        "SlotConfigs": slots or [],
        "SortingTemplates": [],
        "CommunityNotes": [],
        "Defaults": defaults if defaults is not None else dict(_DEFAULTS),
    }
    # The app writes these with a BOM; `utf-8-sig` on the read side is what
    # keeps the first key from arriving as "\ufeffModels".
    (root / CONFIG_DB_NAME).write_text(json.dumps(document), encoding="utf-8-sig")
    if settings_json is not None:
        (data / "Settings.json").write_text(json.dumps(settings_json), encoding="utf-8-sig")
    return root


def _add_images(root: Path, legacy_id: int, names: list[str]) -> None:
    folder = root / "training" / "images" / str(legacy_id)
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"\xff\xd8\xff\xe0jpeg-ish")


def _model(db: Database, name: str) -> Any:
    """The imported row, by name.

    Not ``list()[-1]``: ``ModelRepo.list`` sorts by name, and a fresh DB is
    seeded with a "Default" model that shares the table.
    """
    found = next((m for m in ModelRepo(db).list() if m.name == name), None)
    assert found is not None, f"{name!r} not imported"
    return found


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    """A real SQLite DB with the data root redirected under tmp_path."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "appdata"))
    database = Database(tmp_path / "appdata" / "config" / "casesorter.db")
    database.ensure_initialized()
    return database


@pytest.fixture
def config(db: Database) -> Config:
    return Config(db).load()


# ----- discovery --------------------------------------------------------------


def test_candidate_dirs_follow_program_files_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert candidate_install_dirs() == [Path(r"D:\Apps") / "SJSeth" / "AI Brass Sorter"]


def test_find_installation_returns_none_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole feature has to stay invisible to a user with no Windows app."""
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nothing-here"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert find_installation() is None


def test_find_installation_picks_up_a_real_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pf" / "SJSeth" / "AI Brass Sorter"
    _write_install(root)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert find_installation() == root
    assert looks_like_installation(root)
    assert not looks_like_installation(tmp_path / "elsewhere")


# ----- survey -----------------------------------------------------------------


def test_survey_counts_what_is_there(tmp_path: Path) -> None:
    root = _write_install(
        tmp_path / "legacy",
        headstamps=[
            {"Id": 1, "Name": "GECO", "Model_Id": 3},
            {"Id": 2, "Name": "S&B", "Model_Id": 3},
            {"Id": 3, "Name": "FC", "Model_Id": 4},
        ],
    )
    _add_images(root, 3, ["GECO__1.jpg", "S&B__2.jpg", "notes.txt"])
    _torch_zip(root / "training" / "models" / "4.zip")

    found = survey(root)
    assert not found.is_empty
    assert [m.legacy_id for m in found.models] == [3, 4]
    own, community = found.models
    assert own.image_count == 2  # notes.txt is not an image
    assert own.headstamp_count == 2
    assert own.checkpoint_kind == "none"
    assert community.headstamp_count == 1
    assert community.has_usable_checkpoint
    assert found.total_images == 2
    assert found.total_headstamps == 3
    assert found.has_serial and found.has_image_processing
    assert not found.has_ai_config


def test_survey_flags_an_mlnet_only_model(tmp_path: Path) -> None:
    """An ML.NET checkpoint cannot classify here, and must not be copied as one."""
    root = _write_install(tmp_path / "legacy", models=[_MODEL_OWN])
    _mlnet_zip(root / "training" / "models" / "3.zip")
    found = survey(root)
    assert found.models[0].checkpoint_kind == "mlnet"
    assert not found.models[0].has_usable_checkpoint
    # Equality against the exported constant, not a substring probe: the
    # message is one string owned by the module, and CodeQL reads a
    # `"ML.NET" in ...` test as a hostname allowlist check.
    assert MLNET_WARNING.format(name="9mm Base Model") in found.warnings


def test_survey_does_not_warn_about_an_openai_mode_model(tmp_path: Path) -> None:
    """ "openai" is a first-class mode now, so the row imports faithfully."""
    root = _write_install(tmp_path / "legacy", models=[_MODEL_OPENAI])
    found = survey(root)
    assert found.warnings == []
    assert found.has_ai_config


def test_survey_rejects_a_folder_that_is_not_an_installation(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        survey(tmp_path)


def test_survey_is_empty_for_a_bare_installation(tmp_path: Path) -> None:
    """Nothing to offer → the first-run check stays silent."""
    root = _write_install(tmp_path / "legacy", models=[], defaults={})
    assert survey(root).is_empty


# ----- importing models -------------------------------------------------------


def test_import_brings_models_across_with_ownership_intact(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy")
    _torch_zip(root / "training" / "models" / "4.zip")

    result = import_installation(root, db=db, config=config, options=ImportOptions())

    assert result.models_imported == 2
    assert result.models_updated == 0
    assert result.checkpoints_copied == 1

    by_name = {m.name: m for m in ModelRepo(db).list()}
    own = by_name["9mm Base Model"]
    community = by_name["9mm Default"]
    # ModelType 0 is the user's own work and stays trainable; ModelType 1 is
    # someone else's checkpoint and must not be.
    assert own.model_type == "Standard"
    assert is_trainable(own)
    assert community.model_type == "ReadOnly"
    assert not is_trainable(community)
    # ModelMode 7 is the legacy enum for convnext_small.
    assert community.model_mode == "convnext_small"
    assert community.community_model_uid == "12e3af83-9d6c-4f6c-b045-7a984420543d"
    assert Path(community.model_path or "").is_file()


def test_import_accepts_an_openai_mode_model(tmp_path: Path, db: Database, config: Config) -> None:
    """The whole import used to die here.

    `ModelMode` 2 mapped to a literal "openai", which `ModelRepo` rejects, so
    one such row aborted the transaction and nothing at all was imported —
    the failure reported against a real install on PR #125.
    """
    root = _write_install(tmp_path / "legacy", models=[_MODEL_OWN, _MODEL_OPENAI])
    _add_images(root, 5, ["GECO__1.jpg"])

    result = import_installation(root, db=db, config=config, options=ImportOptions())

    assert result.models_imported == 2
    imported = _model(db, "9mm over HTTP")
    # First-class: the row keeps its mode and its own server settings, so it
    # classifies over HTTP here exactly as it did there (PR #125 review).
    assert imported.model_mode == "openai"
    assert not is_trainable(imported)
    assert imported.ai_model_config.endpoint_url == "http://192.168.1.9:1234/v1/chat/completions"
    assert imported.ai_model_config.model == "qwen2-vl"
    assert result.images_copied == 1


def test_one_unimportable_model_does_not_cost_the_others(
    tmp_path: Path, db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single bad row is worth a warning, not an install's worth of images."""
    root = _write_install(tmp_path / "legacy")
    _add_images(root, 4, ["FC__1.jpg"])

    real_create = ModelRepo.create

    def refuse_one(self: ModelRepo, model: Any) -> Any:
        if model.name == "9mm Base Model":
            raise ValueError("Unsupported model_mode: 'nonsense'")
        return real_create(self, model)

    monkeypatch.setattr(ModelRepo, "create", refuse_one)
    result = import_installation(root, db=db, config=config, options=ImportOptions())

    assert result.models_imported == 1
    assert MODEL_FAILED_WARNING.format(name="9mm Base Model", error="Unsupported model_mode: 'nonsense'") in (
        result.warnings
    )
    names = {m.name for m in ModelRepo(db).list()}
    assert "9mm Default" in names
    # Rolled back to just before the failing row, counts included.
    assert "9mm Base Model" not in names
    assert result.images_copied == 1


def test_import_copies_training_images(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy", models=[_MODEL_OWN])
    _add_images(root, 3, ["GECO__1.jpg", "S&B__2.jpg"])

    result = import_installation(root, db=db, config=config, options=ImportOptions())

    assert result.images_copied == 2
    model = _model(db, "9mm Base Model")
    from sorter import paths

    landed = sorted(p.name for p in paths.model_images_dir(model.id).iterdir())
    # The `{label}__{ticks}.jpg` names carry the labels, so they must survive.
    assert landed == ["GECO__1.jpg", "S&B__2.jpg"]


def test_import_skips_an_mlnet_checkpoint_but_keeps_the_model(tmp_path: Path, db: Database, config: Config) -> None:
    """A shell to retrain into: metadata and images yes, unusable weights no."""
    root = _write_install(tmp_path / "legacy", models=[_MODEL_OWN])
    _mlnet_zip(root / "training" / "models" / "3.zip")
    _add_images(root, 3, ["GECO__1.jpg"])

    result = import_installation(root, db=db, config=config, options=ImportOptions())

    assert result.models_imported == 1
    assert result.checkpoints_copied == 0
    assert result.images_copied == 1
    imported = _model(db, "9mm Base Model")
    assert imported.model_path is None
    assert is_trainable(imported)


def test_import_leaves_the_source_installation_untouched(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy")
    _add_images(root, 3, ["GECO__1.jpg"])
    _torch_zip(root / "training" / "models" / "4.zip")
    before = {p.relative_to(root): p.stat().st_size for p in root.rglob("*") if p.is_file()}

    import_installation(root, db=db, config=config, options=ImportOptions())

    after = {p.relative_to(root): p.stat().st_size for p in root.rglob("*") if p.is_file()}
    assert before == after


# ----- headstamps, parents and slots -----------------------------------------


def test_import_restores_headstamps_parents_and_slots(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL_OWN],
        headstamps=[
            {"Id": 1, "Name": "G.F.L. (Fiocci)", "Model_Id": 3},
            {"Id": 2, "Name": "GECO", "Model_Id": 3},
            {"Id": 9, "Name": "OTHER MODEL", "Model_Id": 99},
        ],
        parents=[{"Id": 1, "Name": "FIOCCHI", "Model_Id": 3}],
        parent_links=[{"Id": 1, "ParentId": 1, "HeadStampId": 1, "ModelId": 3}],
        slots=[
            {
                "SlotNumber": 2,
                "ModelId": 3,
                "PackageMode": False,
                "Config": [{"Headstamp": {"Id": 1, "Name": "G.F.L. (Fiocci)"}, "Counter": 0}],
                "ParentConfig": [],
            },
            {
                "SlotNumber": 5,
                "ModelId": 3,
                "PackageMode": False,
                "Config": [],
                "ParentConfig": [{"Name": "FIOCCHI"}],
            },
        ],
    )

    result = import_installation(root, db=db, config=config, options=ImportOptions())

    model = _model(db, "9mm Base Model")
    headstamps = {h.name: h for h in HeadstampRepo(db).list_for_model(model.id)}
    # A headstamp belonging to another model must not leak into this one.
    assert set(headstamps) == {"G.F.L. (Fiocci)", "GECO"}
    assert headstamps["G.F.L. (Fiocci)"].slot == 2
    assert headstamps["GECO"].slot == 0
    parents = HeadstampParentRepo(db).list_for_model(model.id)
    assert [(p.name, p.slot) for p in parents] == [("FIOCCHI", 5)]
    assert headstamps["G.F.L. (Fiocci)"].parent_id == parents[0].id
    assert result.headstamps_imported == 2
    assert result.parents_imported == 1
    assert result.slots_assigned == 2


def test_import_restores_package_mode_slots(tmp_path: Path, db: Database, config: Config) -> None:
    """Package assignments are many-to-many and live in a settings key, not a row."""
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL_OWN],
        headstamps=[{"Id": 1, "Name": "GECO", "Model_Id": 3}],
        slots=[
            {
                "SlotNumber": 1,
                "ModelId": 3,
                "PackageMode": True,
                "Config": [{"Headstamp": {"Name": "GECO"}}],
                "ParentConfig": [],
            },
            {
                "SlotNumber": 2,
                "ModelId": 3,
                "PackageMode": True,
                "Config": [{"Headstamp": {"Name": "GECO"}}],
                "ParentConfig": [],
            },
        ],
    )

    import_installation(root, db=db, config=config, options=ImportOptions())

    model = _model(db, "9mm Base Model")
    SettingsRepo(db).set_active_model_id(model.id)
    assert config.package_slot_map() == {1: ["GECO"], 2: ["GECO"]}
    # The standard-mode row is untouched by a package-mode assignment.
    assert HeadstampRepo(db).list_for_model(model.id)[0].slot == 0


def test_parent_runtime_preference_follows_the_model(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy", models=[_MODEL_COMMUNITY])
    import_installation(root, db=db, config=config, options=ImportOptions())
    SettingsRepo(db).set_active_model_id(_model(db, "9mm Default").id)
    assert config.use_parent_classifications


# ----- re-import --------------------------------------------------------------


def test_reimport_updates_in_place_and_keeps_slots(tmp_path: Path, db: Database, config: Config) -> None:
    """Running the import twice must not fork the library or lose a layout."""
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL_OWN],
        headstamps=[{"Id": 1, "Name": "GECO", "Model_Id": 3}],
        slots=[
            {
                "SlotNumber": 4,
                "ModelId": 3,
                "PackageMode": False,
                "Config": [{"Headstamp": {"Name": "GECO"}}],
                "ParentConfig": [],
            }
        ],
    )
    _add_images(root, 3, ["GECO__1.jpg"])

    first = import_installation(root, db=db, config=config, options=ImportOptions())
    before = len(ModelRepo(db).list())
    second = import_installation(root, db=db, config=config, options=ImportOptions())

    assert first.models_imported == 1
    assert second.models_imported == 0
    assert second.models_updated == 1
    assert len(ModelRepo(db).list()) == before
    # Already-copied images are skipped rather than re-copied.
    assert second.images_copied == 0
    model = _model(db, "9mm Base Model")
    assert HeadstampRepo(db).list_for_model(model.id)[0].slot == 4


def test_reimport_recognises_an_already_installed_community_model(tmp_path: Path, db: Database, config: Config) -> None:
    """A UID match wins over the per-root memory: same model, wherever it came from."""
    root_a = _write_install(tmp_path / "a", models=[_MODEL_COMMUNITY])
    root_b = _write_install(tmp_path / "b", models=[_MODEL_COMMUNITY])

    import_installation(root_a, db=db, config=config, options=ImportOptions())
    count = len(ModelRepo(db).list())
    result = import_installation(root_b, db=db, config=config, options=ImportOptions())

    assert result.models_updated == 1
    assert len(ModelRepo(db).list()) == count


# ----- settings ---------------------------------------------------------------


def test_serial_settings_import(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy", settings_json={"serialBaudRate": 19200})

    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, serial=True),
    )

    assert result.serial_imported
    reloaded = Config(db).load()
    assert reloaded.serial["port"] == "COM3"
    assert reloaded.serial["baud"] == 19200
    assert reloaded.serial["slot_quantity"] == 8
    init = reloaded.serial["init_settings"]
    assert init["sortspeed"] == 90
    assert init["cameraledlevel"] == 220
    # An unrecognised key would be written straight to the board on connect.
    assert "notarealboardsetting" not in init
    # Keys the legacy install didn't carry keep this app's defaults.
    assert init["fan"] == 100


def test_image_processing_import_touches_linescan_only(tmp_path: Path, db: Database, config: Config) -> None:
    """The legacy pipeline has no Hough stage — its numbers must not reach ours."""
    root = _write_install(tmp_path / "legacy")
    hough_before = dict(config.image_proc["hough"])

    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, image_processing=True),
    )

    assert result.image_processing_imported
    reloaded = Config(db).load()
    assert reloaded.image_proc["linescan"]["scan_precision"] == 5
    assert reloaded.image_proc["linescan"]["bg_cliff"] == 25
    assert reloaded.image_proc["hough"] == hough_before
    assert reloaded.image_proc["strategy"] == "hough"


def test_ai_config_import(tmp_path: Path, db: Database, config: Config) -> None:
    model = dict(_MODEL_OWN)
    model["AIModelConfig"] = {
        "OpenAI_EndpointUrl": "http://192.168.1.5:8000",
        "OpenAI_Model": "qwen-vl",
        "OpenAI_SystemPrompt": "Read the headstamp: {{headstamps}}",
        "ImageQuality": 90,
    }
    root = _write_install(tmp_path / "legacy", models=[model])

    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, ai_config=True),
    )

    assert result.ai_config_imported
    api = Config(db).load().api
    assert api["endpoint_url"] == "http://192.168.1.5:8000"
    assert api["model"] == "qwen-vl"
    assert api["image_quality"] == 90


def test_ai_config_import_reports_when_there_is_nothing_to_take(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy")
    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, ai_config=True),
    )
    assert not result.ai_config_imported


def test_ai_config_prefers_the_model_that_classified_over_http(tmp_path: Path, db: Database, config: Config) -> None:
    """The legacy app writes the blob on every model, most of them blank.

    Taking whichever is declared first would import an empty endpoint over the
    one the user actually configured, and still report success.
    """
    blank = dict(_MODEL_OWN)
    blank["AIModelConfig"] = {"OpenAI_EndpointUrl": "", "OpenAI_Model": "", "OpenAI_SystemPrompt": ""}
    root = _write_install(tmp_path / "legacy", models=[blank, _MODEL_OPENAI])

    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, ai_config=True),
    )

    assert result.ai_config_imported
    api = Config(db).load().api
    assert api["endpoint_url"] == "http://192.168.1.9:1234/v1/chat/completions"
    assert api["model"] == "qwen2-vl"


def test_ai_config_ignores_a_present_but_blank_blob(tmp_path: Path, db: Database, config: Config) -> None:
    blank = dict(_MODEL_OWN)
    blank["AIModelConfig"] = {"OpenAI_EndpointUrl": "", "OpenAI_Model": "", "ImageQuality": 90}
    root = _write_install(tmp_path / "legacy", models=[blank])

    assert not survey(root).has_ai_config
    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, ai_config=True),
    )
    assert not result.ai_config_imported


# ----- options and activation -------------------------------------------------


def test_unticking_models_folds_off_its_dependents(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(
        tmp_path / "legacy",
        headstamps=[{"Id": 1, "Name": "GECO", "Model_Id": 3}],
    )
    _add_images(root, 3, ["GECO__1.jpg"])

    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False, training_images=True, headstamps=True, serial=True),
    )

    assert result.models_imported == 0
    assert result.images_copied == 0
    assert result.headstamps_imported == 0
    assert result.serial_imported


def test_nothing_ticked_is_a_no_op(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy")
    before = len(ModelRepo(db).list())
    result = import_installation(
        root,
        db=db,
        config=config,
        options=ImportOptions(models=False),
    )
    assert result.models_imported == 0
    assert len(ModelRepo(db).list()) == before


def test_import_adopts_the_legacy_active_model(tmp_path: Path, db: Database, config: Config) -> None:
    root = _write_install(tmp_path / "legacy")  # Defaults.DefaultModelId == 4
    result = import_installation(root, db=db, config=config, options=ImportOptions())
    active = SettingsRepo(db).get_active_model_id()
    assert active is not None
    assert result.activated_model_id == active
    assert active == _model(db, "9mm Default").id


def test_import_never_overrides_a_choice_already_made_here(tmp_path: Path, db: Database, config: Config) -> None:
    """The import is an offer, not a takeover."""
    existing = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(existing.id)
    root = _write_install(tmp_path / "legacy")

    result = import_installation(root, db=db, config=config, options=ImportOptions())

    assert result.activated_model_id is None
    assert SettingsRepo(db).get_active_model_id() == existing.id


# ----- the first-run offer ----------------------------------------------------


def test_first_run_offer_appears_once(tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pf" / "SJSeth" / "AI Brass Sorter"
    _write_install(root)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    assert should_offer_first_run(db) == root
    mark_first_run_offered(db)
    assert should_offer_first_run(db) is None
    assert SettingsRepo(db).get(FIRST_RUN_SEEN_KEY) is True


def test_first_run_offer_stays_silent_for_an_app_already_in_use(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seeded fresh DB always has one model, so "has models" is not the test —
    an active model, or a checkpoint on disk, is."""
    root = tmp_path / "pf" / "SJSeth" / "AI Brass Sorter"
    _write_install(root)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert should_offer_first_run(db) == root

    SettingsRepo(db).set_active_model_id(ModelRepo(db).list()[0].id)
    assert should_offer_first_run(db) is None


def test_first_run_offer_stays_silent_with_no_windows_app(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "empty"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert should_offer_first_run(db) is None


def test_reimport_skips_an_unchanged_checkpoint(tmp_path: Path, db: Database, config: Config) -> None:
    """Same rule as the images: a re-run must not re-copy a multi-hundred-MB
    checkpoint that hasn't changed (real-install validation, PR #125)."""
    root = _write_install(tmp_path / "legacy", models=[_MODEL_COMMUNITY])
    _torch_zip(root / "training" / "models" / "4.zip")

    first = import_installation(root, db=db, config=config, options=ImportOptions())
    second = import_installation(root, db=db, config=config, options=ImportOptions())

    assert first.checkpoints_copied == 1
    assert second.checkpoints_copied == 0
    model = _model(db, "9mm Default")
    assert model.model_path is not None and Path(model.model_path).is_file()
