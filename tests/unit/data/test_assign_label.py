"""assign_label_to_empty_slot, including the two auto-select bugs it closes.

(a) a label the context does not know must not be reported as assigned.
(b) parent mode must write the parent's slot, because that is what routing reads.
"""

from __future__ import annotations

from pathlib import Path

from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.repository import HeadstampParentRepo, HeadstampRepo, ModelRepo, SettingsRepo


def _config(tmp_path: Path) -> tuple[Config, int]:
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    cfg = Config(db).load()
    return cfg, seed.id


def test_an_unknown_label_does_not_consume_a_slot(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("WIN", slot=3)
    assert cfg.assign_headstamp_to_empty_slot("NO SUCH") is None
    assert cfg.assign_label_to_empty_slot("") is None
    assert cfg.assign_label_to_empty_slot("   ") is None
    assert cfg.first_empty_slot() == 1
    assert cfg.slot_for_headstamp("NO SUCH") is None
    assert [entry["name"] for entry in cfg.headstamps] == ["WIN"]


def test_an_already_routed_headstamp_is_left_alone(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("BPS", slot=4)
    assert cfg.assign_label_to_empty_slot("BPS") is None
    assert cfg.slot_for_headstamp("BPS") == 4


def test_standard_mode_assigns_the_first_empty_slot(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("WIN", slot=3)
    cfg.add_headstamp("BPS", slot=0)
    assert cfg.assign_label_to_empty_slot("BPS") == 1
    assert cfg.slot_for_headstamp("BPS") == 1
    # The slot is taken; the next unassigned headstamp gets the next one.
    cfg.add_headstamp("IK", slot=0)
    assert cfg.assign_headstamp_to_empty_slot("IK") == 2


def test_no_empty_slot_returns_none(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    for slot in range(1, 8):
        cfg.add_headstamp(f"H{slot}", slot=slot)
    cfg.add_headstamp("BPS", slot=0)
    assert cfg.first_empty_slot() is None
    assert cfg.assign_label_to_empty_slot("BPS") is None
    assert cfg.slot_for_headstamp("BPS") == 0


def test_parent_mode_assigns_the_parent_and_leaves_the_child_slot(tmp_path: Path) -> None:
    cfg, mid = _config(tmp_path)
    cfg.add_headstamp("WIN", slot=3)
    cfg.add_headstamp("FC", slot=5)  # orphan; occupies 5 in parent mode
    brass = HeadstampParentRepo(cfg.db).add(mid, "Brass")
    win = next(h for h in HeadstampRepo(cfg.db).list_for_model(mid) if h.name == "WIN")
    HeadstampRepo(cfg.db).set_parent(win.id, brass.id)
    cfg.set_use_parent_classifications(True)

    assert cfg.assign_label_to_empty_slot("WIN") == 1
    assert cfg.slot_for_headstamp("WIN") == 1
    parent = HeadstampParentRepo(cfg.db).get(brass.id)
    assert parent is not None and parent.slot == 1
    child = next(h for h in HeadstampRepo(cfg.db).list_for_model(mid) if h.name == "WIN")
    assert child.slot == 3
    # Already routed through the parent: a second call must not move it.
    assert cfg.assign_label_to_empty_slot("WIN") is None

    # The panel's key is the parent name itself.
    nickle = HeadstampParentRepo(cfg.db).add(mid, "Nickle")
    assert cfg.assign_label_to_empty_slot("nickle") == 2
    stored = HeadstampParentRepo(cfg.db).get(nickle.id)
    assert stored is not None and stored.slot == 2


def test_parent_mode_orphan_falls_through_to_its_own_slot(tmp_path: Path) -> None:
    cfg, mid = _config(tmp_path)
    cfg.add_headstamp("FC", slot=0)
    HeadstampParentRepo(cfg.db).add(mid, "Brass")
    cfg.set_use_parent_classifications(True)
    assert cfg.assign_label_to_empty_slot("FC") == 1
    assert cfg.slot_for_headstamp("FC") == 1


def test_package_mode_fills_the_first_empty_package_slot(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("CBC", slot=0)
    cfg.add_headstamp("FC", slot=0)
    cfg.set_run_package_mode(True)
    cfg.set_package_slot_headstamp(1, "CBC", True)
    assert cfg.assign_label_to_empty_slot("FC") == 2
    assert cfg.slots_for_headstamp_package("FC") == [2]
    assert cfg.assign_label_to_empty_slot("FC") is None
    # Package mode has always accepted a name that is not a headstamp row;
    # the panel refuses those, the helper does not.
    assert cfg.assign_label_to_empty_slot("NOT A HEADSTAMP") == 3


def test_assign_label_to_slot_shares_an_occupied_bin_and_the_template(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("WMA", slot=4)
    cfg.add_headstamp("WMA NATO", slot=4)
    cfg.add_headstamp("SIG", slot=0)
    assert cfg.assign_label_to_slot("SIG", 4) == 4
    assert cfg.slot_for_headstamp("SIG") == 4
    assert cfg.slot_for_headstamp("WMA") == 4
    assert cfg.slot_for_headstamp("WMA NATO") == 4
    headstamps = cfg.active_slot_template().assignments["headstamps"]
    assert headstamps["SIG"] == 4
    assert headstamps["WMA"] == 4
    assert headstamps["WMA NATO"] == 4
    # Already routed: a second call must not move it onto another bin.
    assert cfg.assign_label_to_slot("SIG", 2) is None
    assert cfg.slot_for_headstamp("SIG") == 4


def test_assign_label_to_slot_refuses_unknown_blank_and_out_of_range(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("BPS", slot=0)
    assert cfg.assign_label_to_slot("", 1) is None
    assert cfg.assign_label_to_slot("   ", 1) is None
    assert cfg.assign_label_to_slot("NO SUCH", 4) is None
    assert cfg.assign_label_to_slot("BPS", 0) is None
    assert cfg.assign_label_to_slot("BPS", 8) is None
    assert cfg.slot_for_headstamp("BPS") == 0
    assert cfg.first_empty_slot() == 1
    # The config layer does not require the target to be occupied.
    assert cfg.assign_label_to_slot("BPS", 7) == 7
    assert cfg.slot_for_headstamp("BPS") == 7


def test_parent_mode_shares_the_parents_slot_and_leaves_the_child(tmp_path: Path) -> None:
    cfg, mid = _config(tmp_path)
    cfg.add_headstamp("WIN", slot=3)
    cfg.add_headstamp("FC", slot=5)
    brass = HeadstampParentRepo(cfg.db).add(mid, "Brass")
    win = next(h for h in HeadstampRepo(cfg.db).list_for_model(mid) if h.name == "WIN")
    HeadstampRepo(cfg.db).set_parent(win.id, brass.id)
    cfg.set_use_parent_classifications(True)

    assert cfg.assign_label_to_slot("WIN", 5) == 5
    assert cfg.slot_for_headstamp("WIN") == 5
    assert cfg.slot_for_headstamp("FC") == 5
    parent = HeadstampParentRepo(cfg.db).get(brass.id)
    assert parent is not None and parent.slot == 5
    child = next(h for h in HeadstampRepo(cfg.db).list_for_model(mid) if h.name == "WIN")
    assert child.slot == 3
    assert cfg.assign_label_to_slot("WIN", 2) is None
    parent = HeadstampParentRepo(cfg.db).get(brass.id)
    assert parent is not None and parent.slot == 5


def test_package_mode_adds_a_name_beside_the_one_already_there(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    cfg.add_headstamp("CBC", slot=0)
    cfg.add_headstamp("FC", slot=0)
    cfg.set_run_package_mode(True)
    cfg.set_package_slot_headstamp(1, "CBC", True)
    assert cfg.assign_label_to_slot("FC", 1) == 1
    assert cfg.slots_for_headstamp_package("FC") == [1]
    assert cfg.slots_for_headstamp_package("CBC") == [1]
    assert cfg.assign_label_to_slot("FC", 2) is None
    slots = cfg.active_slot_template().assignments["slots"]
    assert slots["1"] == ["CBC", "FC"]


def test_ai_config_mode_assigns_only_a_known_headstamp(tmp_path: Path) -> None:
    cfg, _mid = _config(tmp_path)
    SettingsRepo(cfg.db).clear_active_model()
    assert cfg.settings.get_active_model_id() is None
    cfg.add_headstamp("BPS", slot=0)
    cfg.add_headstamp("WIN", slot=4)
    assert cfg.assign_label_to_empty_slot("BPS") == 1
    assert cfg.slot_for_headstamp("BPS") == 1
    assert cfg.assign_label_to_empty_slot("NOPE") is None
    assert cfg.first_empty_slot() == 2
    assert cfg.assign_label_to_empty_slot("WIN") is None
    assert cfg.slot_for_headstamp("WIN") == 4
    cfg.add_headstamp("SIG", slot=0)
    assert cfg.assign_label_to_slot("SIG", 4) == 4
    assert cfg.slot_for_headstamp("SIG") == 4
    assert cfg.slot_for_headstamp("WIN") == 4
    assert cfg.assign_label_to_slot("NOPE", 4) is None
