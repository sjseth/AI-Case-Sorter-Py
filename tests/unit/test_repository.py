"""Repository CRUD and constraint tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from sorter.db import Database
from sorter.models import Model
from sorter.repository import (
    CartridgeRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)


def _new_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    return db


def test_cartridge_crud(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    repo = CartridgeRepo(db)
    initial_count = len(repo.list())
    new_cart = repo.create(".223 Rem")
    assert new_cart.id
    assert repo.find_by_name(".223 Rem").id == new_cart.id
    assert len(repo.list()) == initial_count + 1
    repo.rename(new_cart.id, ".223 Remington")
    assert repo.get(new_cart.id).name == ".223 Remington"


def test_model_round_trip(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    cart_repo = CartridgeRepo(db)
    repo = ModelRepo(db)

    cart = cart_repo.create("45ACP")
    model = Model(name="my45", cartridge_id=cart.id, model_mode="convnext_small")
    model.training_config.epochs = 25
    model.image_processing.primer_mode = "use"
    saved = repo.create(model)
    assert saved.id

    loaded = repo.get(saved.id)
    assert loaded.name == "my45"
    assert loaded.model_mode == "convnext_small"
    assert loaded.training_config.epochs == 25
    assert loaded.image_processing.primer_mode == "use"


def test_model_rejects_unsupported_mode(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    repo = ModelRepo(db)
    cart_id = CartridgeRepo(db).create("xx").id
    with pytest.raises(ValueError):
        repo.create(Model(name="bad", cartridge_id=cart_id, model_mode="resnet50"))


def test_cannot_delete_last_model_in_cartridge(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    repo = ModelRepo(db)
    cart_id = CartridgeRepo(db).create("99mm").id
    only_model = repo.create(Model(name="only", cartridge_id=cart_id, model_mode="convnext_tiny"))
    with pytest.raises(ValueError):
        repo.delete(only_model.id)


def test_cannot_delete_active_model_without_replacement(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    repo = ModelRepo(db)
    settings = SettingsRepo(db)
    # The seeded model is no longer auto-activated; pick it and activate it
    # explicitly, then add a sibling so the "last in cartridge" rule does
    # not pre-empt the active-model rule.
    seed_model = repo.list()[0]
    settings.set_active_model_id(seed_model.id)
    sibling = repo.create(
        Model(
            name="sibling",
            cartridge_id=seed_model.cartridge_id,
            model_mode="convnext_tiny",
        )
    )
    with pytest.raises(ValueError):
        repo.delete(seed_model.id)
    repo.delete(seed_model.id, replacement_active_id=sibling.id)
    assert settings.get_active_model_id() == sibling.id


def test_clear_active_model_returns_to_ai_config_mode(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    settings = SettingsRepo(db)
    seed_model = ModelRepo(db).list()[0]
    settings.set_active_model_id(seed_model.id)
    assert settings.get_active_model_id() == seed_model.id

    settings.clear_active_model()
    assert settings.get_active_model_id() is None


def test_headstamp_replace_atomically(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    repo = HeadstampRepo(db)
    # The seeded model is no longer auto-activated, but we can still target
    # it directly through the repo.
    model_id = ModelRepo(db).list()[0].id
    repo.add(model_id, "ALPHA", slot=1)
    repo.add(model_id, "BETA", slot=2)

    repo.replace_for_model(
        model_id,
        [
            {"name": "GAMMA", "slot": 3},
            {"name": "DELTA", "slot": 4},
        ],
    )
    names = sorted(h.name for h in repo.list_for_model(model_id))
    assert names == ["DELTA", "GAMMA"]


def test_headstamps_cascade_on_model_delete(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    cart_id = CartridgeRepo(db).create("9x18").id
    model = ModelRepo(db).create(Model(name="m1", cartridge_id=cart_id, model_mode="convnext_tiny"))
    # The row itself is the point: ModelRepo.delete refuses to remove the last
    # model in a cartridge, so the delete below needs a second model to get past
    # that guard and reach the cascade. Created, never referenced by name.
    ModelRepo(db).create(Model(name="m2", cartridge_id=cart_id, model_mode="convnext_tiny"))
    HeadstampRepo(db).add(model.id, "X")
    HeadstampRepo(db).add(model.id, "Y")
    ModelRepo(db).delete(model.id)
    assert HeadstampRepo(db).list_for_model(model.id) == []


def test_find_by_community_uid_prefers_the_active_duplicate(tmp_path: Path) -> None:
    """Libraries built by older versions can hold several rows for one UID
    (every update was imported as a new model). The lookup has to pick one
    row deterministically — the active model, else the oldest."""
    db = _new_db(tmp_path)
    cart = CartridgeRepo(db).get_or_create("9mm")
    repo = ModelRepo(db)
    first = repo.create(Model(name="Comm", cartridge_id=cart.id, community_model_uid="uid-dup"))
    second = repo.create(Model(name="Comm (2)", cartridge_id=cart.id, community_model_uid="uid-dup"))

    # No active model: oldest wins.
    assert repo.find_by_community_uid("uid-dup").id == first.id

    # Active model wins, whichever duplicate it is.
    settings = SettingsRepo(db)
    settings.set_active_model_id(second.id)
    assert repo.find_by_community_uid("uid-dup").id == second.id
    settings.set_active_model_id(first.id)
    assert repo.find_by_community_uid("uid-dup").id == first.id

    assert repo.find_by_community_uid("nope") is None
