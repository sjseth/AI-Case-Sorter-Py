"""Parent-classification data layer: repo CRUD, unlink-on-delete, migration."""

from __future__ import annotations

from pathlib import Path

from sorter.db import Database
from sorter.models import Model
from sorter.repository import (
    CartridgeRepo,
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
)

from ._legacy_db import columns, write_legacy_db


def _new_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    return db


def _model_with_headstamps(db: Database, names: list[str]) -> int:
    cart_id = CartridgeRepo(db).create("9mm-parents").id
    model = ModelRepo(db).create(Model(name="m", cartridge_id=cart_id, model_mode="convnext_tiny"))
    repo = HeadstampRepo(db)
    for n in names:
        repo.add(model.id, n)
    return model.id


def test_parent_crud(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    model_id = _model_with_headstamps(db, [])
    repo = HeadstampParentRepo(db)

    p = repo.add(model_id, "WIN")
    assert p.id
    found = repo.find_by_name(model_id, "win")
    assert found is not None and found.id == p.id  # case-insensitive
    assert [x.name for x in repo.list_for_model(model_id)] == ["WIN"]

    repo.rename(p.id, "WINCHESTER")
    renamed = repo.get(p.id)
    assert renamed is not None and renamed.name == "WINCHESTER"

    repo.delete(p.id)
    assert repo.list_for_model(model_id) == []


def test_set_parent_round_trips(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    model_id = _model_with_headstamps(db, ["WIN-9", "WIN-45"])
    hs_repo = HeadstampRepo(db)
    p_repo = HeadstampParentRepo(db)

    parent = p_repo.add(model_id, "WIN")
    win9 = next(h for h in hs_repo.list_for_model(model_id) if h.name == "WIN-9")
    hs_repo.set_parent(win9.id, parent.id)

    reloaded = next(h for h in hs_repo.list_for_model(model_id) if h.id == win9.id)
    assert reloaded.parent_id == parent.id

    hs_repo.set_parent(win9.id, None)
    reloaded = next(h for h in hs_repo.list_for_model(model_id) if h.id == win9.id)
    assert reloaded.parent_id is None


def test_deleting_parent_unlinks_children(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    model_id = _model_with_headstamps(db, ["FC-9", "FC-45"])
    hs_repo = HeadstampRepo(db)
    p_repo = HeadstampParentRepo(db)

    parent = p_repo.add(model_id, "FC")
    for h in hs_repo.list_for_model(model_id):
        hs_repo.set_parent(h.id, parent.id)

    p_repo.delete(parent.id)

    # Children survive, but their parent link is cleared.
    remaining = hs_repo.list_for_model(model_id)
    assert {h.name for h in remaining} == {"FC-9", "FC-45"}
    assert all(h.parent_id is None for h in remaining)


def test_deleting_model_cascades_parents(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    cart_id = CartridgeRepo(db).create("casc").id
    m1 = ModelRepo(db).create(Model(name="m1", cartridge_id=cart_id, model_mode="convnext_tiny"))
    ModelRepo(db).create(Model(name="m2", cartridge_id=cart_id, model_mode="convnext_tiny"))
    p_repo = HeadstampParentRepo(db)
    p_repo.add(m1.id, "WIN")
    p_repo.add(m1.id, "FC")

    ModelRepo(db).delete(m1.id)  # sibling keeps the cartridge non-empty
    assert p_repo.list_for_model(m1.id) == []


def test_migration_adds_parent_id_to_v1_db(tmp_path: Path) -> None:
    """A pre-existing v1 headstamps table (no parent_id) is upgraded in place.

    The schema-level assertions about this migration live in
    `test_db.py`; what this one adds is that the parent *relationship* works
    end-to-end through the repos afterwards.
    """
    db_path = tmp_path / "legacy.db"
    write_legacy_db(db_path, user_version=1, with_parents_table=False)

    # Opening through Database should add the column and the parents table.
    db = Database(db_path)
    db.ensure_initialized()

    assert "parent_id" in columns(db.conn, "headstamps")

    # The new table exists and the parent relationship works end-to-end.
    p = HeadstampParentRepo(db).add(1, "WIN-GROUP")
    win = next(h for h in HeadstampRepo(db).list_for_model(1) if h.name == "WIN")
    assert win.parent_id is None
    HeadstampRepo(db).set_parent(win.id, p.id)
    reread = {h.name: h for h in HeadstampRepo(db).list_for_model(1)}
    assert reread["WIN"].parent_id == p.id
    assert reread["FC"].parent_id is None, "only the linked headstamp is touched"
