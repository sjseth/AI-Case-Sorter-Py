"""Run-tab package-mode + auto-select wiring (needs a Tk display)."""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk

from sorter.control.events import EventBus
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.repository import ModelRepo, SettingsRepo
from sorter.ui.tab_run import RunTab


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    r.destroy()


class _FakeApp:
    def __init__(self, db, root) -> None:
        self.db = db
        self.root = root
        self.auth = None
        self.run_controller = None
        self.broker = None
        self.status = ""

    def run_worker(self, fn, *, on_done=None, on_error=None):
        pass

    def set_status(self, msg):
        self.status = msg


def _make(root, tmp_path, monkeypatch):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    db = Database(tmp_path / "ui.db")
    db.ensure_initialized()
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    cfg = Config(db).load()
    cfg.add_headstamp("CBC")
    cfg.add_headstamp("FC")
    app = _FakeApp(db, root)
    tab = RunTab(root, config=cfg, bus=EventBus(), app=app)
    return tab, app, cfg


def test_toggle_package_mode_shows_batch_and_reset(root, tmp_path, monkeypatch) -> None:
    tab, _app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        assert tab._batch_row.winfo_manager() == ""  # hidden off
        tab._package_var.set(True)
        tab._on_toggle_package_mode()
        assert cfg.run_package_mode is True
        assert tab._batch_row.winfo_manager() == "pack"  # batch size shown
        # Non-catch-all cards now expose the live reset button.
        non_catch = [c for c in tab._slot_cards if c.slot_number > 0][0]
        assert non_catch._reset_row.winfo_manager() == "pack"
        assert tab._slot_cards[0]._reset_row.winfo_manager() == ""  # not slot 0
    finally:
        tab.destroy()


def test_package_details_panel_multi_assign(root, tmp_path, monkeypatch) -> None:
    tab, _app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        cfg.set_run_package_mode(True)
        tab.details.show_slot(1)
        cfg.set_package_slot_headstamp(1, "CBC", True)
        cfg.set_package_slot_headstamp(2, "CBC", True)
        # The same headstamp lives in both slots — no one-slot lock.
        assert cfg.slots_for_headstamp_package("CBC") == [1, 2]
    finally:
        tab.destroy()


def test_card_reset_zeroes_count(root, tmp_path, monkeypatch) -> None:
    tab, _app, _cfg = _make(root, tmp_path, monkeypatch)
    try:
        tab._slot_counts[1] = 7
        for c in tab._slot_cards:
            if c.slot_number == 1:
                c.set_count(7)
        tab._on_card_reset(1)
        assert tab._slot_counts[1] == 0
    finally:
        tab.destroy()


def test_auto_select_toggle_persists(root, tmp_path, monkeypatch) -> None:
    tab, _app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        tab._auto_select_var.set(True)
        tab._on_toggle_auto_select()
        assert cfg.run_auto_select_trays is True
    finally:
        tab.destroy()
