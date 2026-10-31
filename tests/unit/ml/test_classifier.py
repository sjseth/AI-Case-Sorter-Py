"""Tests for the classify dispatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from sorter.data.db import Database
from sorter.data.repository import ModelRepo, SettingsRepo
from sorter.ml import classifier


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "x.db")
    db.ensure_initialized()
    return db


def _activate_seeded_model(db: Database) -> int:
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    return seed.id


def test_falls_back_to_http_when_no_active_model(tmp_path: Path) -> None:
    """AI Config mode (no active model) routes to HTTP."""
    db = _seed_db(tmp_path)
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    with patch("sorter.ml.classifier.api_client.classify", return_value=("X", 42.0)) as m:
        with patch("sorter.ml.classifier.local_inference.classify") as m_local:
            result = classifier.classify_active(image, ["A"], {"endpoint_url": "http://x"}, db)
    assert result == ("X", 42.0)
    m.assert_called_once()
    m_local.assert_not_called()


def test_untrained_active_model_raises_instead_of_using_http(tmp_path: Path) -> None:
    """A local model with no checkpoint must NOT silently classify over HTTP.

    The old fallback shipped case images to whatever the AI Config tab last
    pointed at, without saying so.
    """
    db = _seed_db(tmp_path)
    _activate_seeded_model(db)
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    with patch("sorter.ml.classifier.api_client.classify") as m:
        with patch("sorter.ml.classifier.local_inference.classify") as m_local:
            with pytest.raises(classifier.NoLocalCheckpointError):
                classifier.classify_active(image, ["A"], {"endpoint_url": "http://x"}, db)
    m.assert_not_called()
    m_local.assert_not_called()


def test_uses_local_when_active_model_has_path(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    active_id = _activate_seeded_model(db)
    fake_model_file = tmp_path / "fake.pth"
    fake_model_file.write_bytes(b"dummy")
    model = ModelRepo(db).get(active_id)
    assert model is not None
    model.model_path = str(fake_model_file)
    ModelRepo(db).update(model)

    image = np.zeros((10, 10, 3), dtype=np.uint8)
    with patch("sorter.ml.classifier.local_inference.classify", return_value=("LOCAL_X", 90.0)) as m_local:
        with patch("sorter.ml.classifier.api_client.classify") as m_http:
            result = classifier.classify_active(image, [], {}, db)
    assert result == ("LOCAL_X", 90.0)
    m_local.assert_called_once()
    m_http.assert_not_called()


def test_no_db_falls_back_to_http(tmp_path: Path) -> None:
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    with patch("sorter.ml.classifier.api_client.classify", return_value=("Y", 50.0)) as m_http:
        result = classifier.classify_active(image, [], {}, None)
    assert result == ("Y", 50.0)
    # The return value alone doesn't prove the fallback: assert the HTTP path
    # is what produced it.
    m_http.assert_called_once()


def test_missing_model_file_raises_instead_of_using_http(tmp_path: Path) -> None:
    """The data-folder-was-renamed case: a stale model_path must fail loudly."""
    db = _seed_db(tmp_path)
    active_id = _activate_seeded_model(db)
    model = ModelRepo(db).get(active_id)
    assert model is not None
    model.model_path = str(tmp_path / "does_not_exist.pth")
    ModelRepo(db).update(model)

    image = np.zeros((10, 10, 3), dtype=np.uint8)
    with patch("sorter.ml.classifier.api_client.classify") as m:
        with patch("sorter.ml.classifier.local_inference.classify") as m_local:
            with pytest.raises(classifier.NoLocalCheckpointError) as exc:
                classifier.classify_active(image, [], {}, db)
    m.assert_not_called()
    m_local.assert_not_called()
    # The message has to name the path, or the user can't tell which of the
    # two failure modes they're in.
    assert "does_not_exist.pth" in str(exc.value)


def test_checkpoint_problem_is_quiet_when_nothing_is_wrong(tmp_path: Path) -> None:
    """No nagging in AI Config mode, nor for a model that can classify."""
    db = _seed_db(tmp_path)
    assert classifier.checkpoint_problem(db) is None  # AI Config mode
    assert classifier.checkpoint_problem(None) is None

    active_id = _activate_seeded_model(db)
    ckpt = tmp_path / "real.pth"
    ckpt.write_bytes(b"dummy")
    model = ModelRepo(db).get(active_id)
    assert model is not None
    model.model_path = str(ckpt)
    ModelRepo(db).update(model)
    assert classifier.checkpoint_problem(db) is None


def test_checkpoint_problem_explains_an_untrained_model(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    _activate_seeded_model(db)
    problem = classifier.checkpoint_problem(db)
    assert problem is not None
    assert "no trained model file" in problem
    # Must say it is NOT quietly switching backends.
    assert "AI Config" in problem


# ----- openai-mode models -----------------------------------------------------


def _activate_openai_model(db: Database, **config_kwargs) -> int:
    from sorter.data.models import AIModelConfig, Model

    cart_id = ModelRepo(db).list()[0].cartridge_id
    model = ModelRepo(db).create(
        Model(
            name="HTTP model",
            cartridge_id=cart_id,
            model_mode="openai",
            ai_model_config=AIModelConfig(**config_kwargs),
        )
    )
    SettingsRepo(db).set_active_model_id(model.id)
    return model.id


def test_openai_model_routes_to_http_with_its_own_config(tmp_path: Path) -> None:
    """The passed api_cfg is the app-level one — an openai model must ignore it.

    Leaking the app-level config in would be the mirror image of the removed
    HTTP fallback: cases silently classified against a server the active
    model never named.
    """
    db = _seed_db(tmp_path)
    _activate_openai_model(db, endpoint_url="http://model-server:9", api_key="k", model="qwen")
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    with patch("sorter.ml.classifier.api_client.classify", return_value=("FC", 88.0)) as m:
        with patch("sorter.ml.classifier.local_inference.classify") as m_local:
            result = classifier.classify_active(image, ["FC"], {"endpoint_url": "http://app-level"}, db)
    assert result == ("FC", 88.0)
    m_local.assert_not_called()
    cfg = m.call_args.args[2]
    assert cfg["endpoint_url"] == "http://model-server:9"
    assert cfg["model"] == "qwen"


def test_openai_model_needs_no_torch_and_has_no_checkpoint_problem(tmp_path: Path) -> None:
    """`uses_local_inference` is what the torch gate keys off; `checkpoint_problem`
    is what refuses Start. Both must wave an openai model through."""
    db = _seed_db(tmp_path)
    _activate_openai_model(db)
    assert not classifier.uses_local_inference(db)
    assert classifier.checkpoint_problem(db) is None


# ----- the torch security floor, at the load (security audit, 2026-10) ----------


def _activate_with_checkpoint(db: Database, tmp_path: Path, **fields) -> int:
    active_id = _activate_seeded_model(db)
    checkpoint = tmp_path / "evil.pth"
    checkpoint.write_bytes(b"crafted")
    model = ModelRepo(db).get(active_id)
    assert model is not None
    model.model_path = str(checkpoint)
    for name, value in fields.items():
        setattr(model, name, value)
    ModelRepo(db).update(model)
    return active_id


@pytest.fixture
def outdated_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classifier.local_inference, "is_installed", lambda: True)
    monkeypatch.setattr(classifier.local_inference, "installed_version", lambda: "2.9.1")
    monkeypatch.setattr(classifier.local_inference, "meets_min_version", lambda *_a, **_k: False)


@pytest.mark.parametrize(
    "fields",
    [
        {"model_type": "CommunityManaged"},
        {"model_type": "Standard", "checkpoint_origin": "imported"},
    ],
    ids=["community", "plain-import"],
)
def test_an_untrusted_checkpoint_never_reaches_an_outdated_torch(
    tmp_path: Path, outdated_torch: None, fields: dict
) -> None:
    """The backstop for a model activated mid-run: the Start gate only saw the
    model that was active at Start, but this runs for every case."""
    db = _seed_db(tmp_path)
    _activate_with_checkpoint(db, tmp_path, **fields)
    image = np.zeros((10, 10, 3), dtype=np.uint8)

    with patch("sorter.ml.classifier.local_inference.classify") as m_local:
        with pytest.raises(classifier.UntrustedCheckpointError, match="2.9.1"):
            classifier.classify_active(image, [], {}, db)
    m_local.assert_not_called()


def test_a_checkpoint_trained_here_still_loads_on_an_outdated_torch(tmp_path: Path, outdated_torch: None) -> None:
    """Unchanged for existing users: their own models are an offer, not a block."""
    db = _seed_db(tmp_path)
    _activate_with_checkpoint(db, tmp_path, checkpoint_origin="local")
    image = np.zeros((10, 10, 3), dtype=np.uint8)

    with patch("sorter.ml.classifier.local_inference.classify", return_value=("FC", 99.0)) as m_local:
        assert classifier.classify_active(image, [], {}, db) == ("FC", 99.0)
    m_local.assert_called_once()


def test_a_current_torch_loads_an_imported_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classifier.local_inference, "is_installed", lambda: True)
    monkeypatch.setattr(classifier.local_inference, "meets_min_version", lambda *_a, **_k: True)
    db = _seed_db(tmp_path)
    _activate_with_checkpoint(db, tmp_path, model_type="CommunityManaged")
    image = np.zeros((10, 10, 3), dtype=np.uint8)

    with patch("sorter.ml.classifier.local_inference.classify", return_value=("FC", 99.0)):
        assert classifier.classify_active(image, [], {}, db) == ("FC", 99.0)
