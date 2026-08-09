"""Package-mode batch routing + auto-select-trays routing in RunController.

All headless — the emulator + a monkeypatched classify stand in for hardware.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np

from sorter.config import Config
from sorter.db import Database
from sorter.events import EventBus
from sorter.repository import ModelRepo, SettingsRepo
from sorter.run_controller import RunController
from sorter.serial_emulator import EmulatorBroker


class _FakeCamera:
    def __init__(self) -> None:
        self.frame = np.full((480, 480, 3), 128, dtype=np.uint8)

    def capture_frame(self) -> np.ndarray:
        return self.frame


def _make(tmp_path) -> tuple[RunController, Config, Database]:
    em = EmulatorBroker(response_delay_s=0.001)
    em.try_open()
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    cfg = Config(db).load()
    cfg.api["api_key"] = "fake"
    cfg.save()
    cfg.add_headstamp("CBC")
    cfg.add_headstamp("FC")
    cfg.set_run_confidence_floor(0)
    ctrl = RunController(config=cfg, broker=em, camera=_FakeCamera(), bus=EventBus(), db=db)
    return ctrl, cfg, db


# ----- config-level package assignment ---------------------------------------


def test_package_slot_map_is_many_to_many(tmp_path) -> None:
    _ctrl, cfg, _ = _make(tmp_path)
    cfg.set_run_package_mode(True)
    cfg.set_package_slot_headstamp(1, "CBC", True)
    cfg.set_package_slot_headstamp(2, "CBC", True)
    cfg.set_package_slot_headstamp(3, "FC", True)
    assert cfg.slots_for_headstamp_package("CBC") == [1, 2]
    assert cfg.slots_for_headstamp_package("FC") == [3]
    # Unticking removes just that slot.
    cfg.set_package_slot_headstamp(1, "CBC", False)
    assert cfg.slots_for_headstamp_package("CBC") == [2]


def test_first_empty_slot_skips_assigned(tmp_path) -> None:
    _ctrl, cfg, _ = _make(tmp_path)
    cfg.set_headstamp_slot("CBC", 1)
    cfg.set_headstamp_slot("FC", 2)
    assert cfg.first_empty_slot(package=False) == 3


def test_first_empty_slot_ignores_other_modes_config(tmp_path) -> None:
    """Auto-select must only consult the slot config for the active mode."""
    _ctrl, cfg, db = _make(tmp_path)
    from sorter.repository import HeadstampParentRepo, SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()
    assert mid is not None

    # A parent assigned to slot 1 (a *parent-mode* configuration) plus a
    # package assignment in slot 1 must NOT mark slot 1 occupied for a plain
    # child-mode run.
    parent = HeadstampParentRepo(db).add(mid, "Brass")
    HeadstampParentRepo(db).update_slot(parent.id, 1)
    cfg.set_package_slot_headstamp(1, "CBC", True)

    # Child mode (parent classifications off): slot 1 is free.
    assert cfg.use_parent_classifications is False
    assert cfg.first_empty_slot(package=False) == 1

    # Parent mode: the parent's slot 1 now counts as occupied.
    cfg.set_use_parent_classifications(True)
    assert cfg.first_empty_slot() == 2


def test_first_empty_slot_package_uses_package_map_only(tmp_path) -> None:
    _ctrl, cfg, _ = _make(tmp_path)
    cfg.set_headstamp_slot("CBC", 1)  # single-slot (non-package) assignment
    cfg.set_package_slot_headstamp(2, "FC", True)  # package-mode assignment
    # Package mode sees only the package map → slot 1 is free.
    assert cfg.first_empty_slot(package=True) == 1
    # Child mode sees only headstamp slots → slot 2 is free.
    assert cfg.first_empty_slot(package=False) == 2


# ----- package routing --------------------------------------------------------


def test_package_fills_then_advances_then_halts(tmp_path) -> None:
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_run_package_mode(True)
    cfg.set_run_package_size(2)
    cfg.set_package_slot_headstamp(1, "CBC", True)
    cfg.set_package_slot_headstamp(2, "CBC", True)

    full_events: list[dict] = []
    halt_events: list[dict] = []
    ctrl.bus.subscribe("run/package_full", full_events.append)
    ctrl.bus.subscribe("run/package_halt", halt_events.append)

    slots = []
    with patch("sorter.classifier.classify_active", return_value=("CBC", 100)):
        for _ in range(4):
            r = ctrl.run_once()
            slots.append((r["slot"], r.get("halt")))
        ctrl.bus.drain()
        # Fifth case: every CBC slot is full → halt.
        r5 = ctrl.run_once()
        ctrl.bus.drain()

    # Slot 1 fills (count 1,2), then slot 2 fills (1,2).
    assert [s for s, _ in slots] == [1, 1, 2, 2]
    assert r5["halt"] is True
    assert r5["slot"] == 0
    # A package_full bell fired for each completed batch.
    assert len(full_events) == 2
    assert halt_events == []  # halt event is posted by _loop, not run_once


def test_package_counts_persist_across_restart(tmp_path, monkeypatch) -> None:
    """Stopping and restarting a run resumes batches where they left off."""
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_run_package_mode(True)
    cfg.set_run_package_size(10)
    cfg.set_package_slot_headstamp(1, "CBC", True)
    with patch("sorter.classifier.classify_active", return_value=("CBC", 100)):
        for _ in range(8):
            ctrl.run_once()
    assert ctrl.package_count(1) == 8

    # start() must NOT zero the counters. Stub the loop so the worker thread
    # exits immediately without running more cycles.
    monkeypatch.setattr(ctrl, "_loop", lambda: None)
    ctrl.start()
    if ctrl._thread is not None:
        ctrl._thread.join(timeout=1)
    assert ctrl.package_count(1) == 8

    # The explicit reset still clears them.
    ctrl.reset_package_counts()
    assert ctrl.package_count(1) == 0


def test_reset_package_slot_lets_it_refill(tmp_path) -> None:
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_run_package_mode(True)
    cfg.set_run_package_size(2)
    cfg.set_package_slot_headstamp(1, "CBC", True)

    with patch("sorter.classifier.classify_active", return_value=("CBC", 100)):
        ctrl.run_once()
        ctrl.run_once()  # slot 1 now full (2/2)
        assert ctrl.package_count(1) == 2
        ctrl.reset_package_slot(1)
        assert ctrl.package_count(1) == 0
        r = ctrl.run_once()  # refills slot 1
    assert r["slot"] == 1
    assert r.get("halt") is not True


def test_package_unconfigured_label_goes_catch_all(tmp_path) -> None:
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_run_package_mode(True)
    cfg.set_package_slot_headstamp(1, "CBC", True)
    with patch("sorter.classifier.classify_active", return_value=("FC", 100)):
        r = ctrl.run_once()
    assert r["slot"] == 0
    assert r.get("halt") is not True


# ----- auto-select trays ------------------------------------------------------


def test_auto_select_assigns_unmapped_to_empty_slot(tmp_path) -> None:
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_headstamp_slot("CBC", 1)  # CBC already in slot 1
    cfg.set_run_auto_select_trays(True)

    events: list[dict] = []
    ctrl.bus.subscribe("run/assignment_changed", events.append)
    with patch("sorter.classifier.classify_active", return_value=("FC", 100)):
        r = ctrl.run_once()
    ctrl.bus.drain()
    # FC was unmapped → first empty slot (2).
    assert r["slot"] == 2
    assert cfg.slot_for_headstamp("FC") == 2
    assert events and events[0]["slot"] == 2


def test_auto_select_respects_existing_assignment(tmp_path) -> None:
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_headstamp_slot("FC", 5)
    cfg.set_run_auto_select_trays(True)
    with patch("sorter.classifier.classify_active", return_value=("FC", 100)):
        r = ctrl.run_once()
    assert r["slot"] == 5  # untouched
    assert cfg.slot_for_headstamp("FC") == 5


def test_auto_select_skips_below_floor(tmp_path) -> None:
    ctrl, cfg, _ = _make(tmp_path)
    cfg.set_run_confidence_floor(80)
    cfg.set_run_auto_select_trays(True)
    with patch("sorter.classifier.classify_active", return_value=("FC", 10)):
        r = ctrl.run_once()
    assert r["slot"] == 0
    assert cfg.slot_for_headstamp("FC") == 0  # left unassigned (slot 0)
