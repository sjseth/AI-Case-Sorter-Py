"""Sort-While-Training slot selection (needs a Tk display)."""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk

from sorter.control.events import EventBus
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.repository import ModelRepo, SettingsRepo
from sorter.ui.tab_train import TrainTab


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    r.destroy()


class _FakeBroker:
    def __init__(self) -> None:
        self.feed_slots: list[int] = []

    def force_sort_and_move(self, slot: int) -> bool:
        self.feed_slots.append(int(slot))
        return True


class _FakeApp:
    def __init__(self, db, broker) -> None:
        self.db = db
        self.broker = broker

    def capture_frame(self):
        return None  # short-circuits _work after the feed command

    def run_worker(self, fn, *, on_done=None, on_error=None):
        try:
            result = fn()
        except Exception as exc:  # pragma: no cover - defensive
            if on_error:
                on_error(exc)
        else:
            if on_done:
                on_done(result)

    def set_status(self, _msg):
        pass


def _make(root, tmp_path, monkeypatch):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    db = Database(tmp_path / "t.db")
    db.ensure_initialized()
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    cfg = Config(db).load()
    cfg.add_headstamp("CBC", slot=3)
    cfg.add_headstamp("FC", slot=5)
    broker = _FakeBroker()
    tab = TrainTab(root, config=cfg, bus=EventBus(), app=_FakeApp(db, broker))
    return tab, broker, cfg


def test_manual_feed_uses_dropdown_label(root, tmp_path, monkeypatch) -> None:
    tab, broker, _cfg = _make(root, tmp_path, monkeypatch)
    try:
        tab._sort_while_training_var.set(True)
        tab.label_var.set("CBC")  # user's dropdown selection
        tab._feed()
        assert broker.feed_slots[-1] == 3  # CBC's run slot, not 0
    finally:
        tab.destroy()


def test_autofeed_after_save_uses_saved_label_not_cleared_dropdown(root, tmp_path, monkeypatch) -> None:
    tab, broker, _cfg = _make(root, tmp_path, monkeypatch)
    try:
        tab._sort_while_training_var.set(True)
        # The save path clears the dropdown, then auto-feeds with the saved
        # label — the just-labelled case must still route to that label's slot.
        tab.label_var.set("")
        tab._feed(sort_label="FC")
        assert broker.feed_slots[-1] == 5  # FC's slot, despite empty dropdown
    finally:
        tab.destroy()


def test_disabled_sort_while_training_feeds_catch_all(root, tmp_path, monkeypatch) -> None:
    tab, broker, _cfg = _make(root, tmp_path, monkeypatch)
    try:
        tab._sort_while_training_var.set(False)
        tab.label_var.set("CBC")
        tab._feed()
        assert broker.feed_slots[-1] == 0  # off → always catch-all
    finally:
        tab.destroy()
