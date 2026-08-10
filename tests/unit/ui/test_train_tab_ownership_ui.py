"""The Train tab is hidden for models the user doesn't own (needs Tk)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk
from tkinter import ttk

from sorter.config import Config
from sorter.db import Database
from sorter.events import EventBus
from sorter.models import Model
from sorter.repository import CartridgeRepo, ModelRepo, SettingsRepo
from sorter.ui.app import MainWindow
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


class _VisibilityHarness:
    """Minimal stand-in for MainWindow.

    Building a real MainWindow auto-connects serial and camera; all
    `_apply_train_tab_visibility` actually touches is the db, the notebook
    and the Train container, so we borrow the unbound method.
    """

    def __init__(self, root, db) -> None:
        self.db = db
        self.notebook = ttk.Notebook(root)
        self.notebook.add(ttk.Frame(self.notebook), text="Run")
        self.notebook.add(ttk.Frame(self.notebook), text="Models")
        self._train_tab_container = ttk.Frame(self.notebook)
        self.notebook.add(self._train_tab_container, text="Train")

    def train_tab_visible(self) -> bool:
        # Borrowing the unbound method: it only touches .db, .notebook, and
        # ._train_tab_container, all of which this harness provides, so the
        # structural stand-in is safe even though it isn't a MainWindow.
        MainWindow._apply_train_tab_visibility(cast(MainWindow, self))
        # `tabs()` still lists a hidden tab, so ask for its state — that's
        # what decides whether the user can actually reach it.
        for tid in self.notebook.tabs():
            if self.notebook.tab(tid, "text") == "Train":
                return self.notebook.tab(tid, "state") != "hidden"
        return False

    _restore_train_tab_position = MainWindow._restore_train_tab_position


def _db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    db = Database(tmp_path / "t.db")
    db.ensure_initialized()
    return db


def _activate(db: Database, *, model_type: str, uid: str | None = None) -> Model:
    cart = CartridgeRepo(db).create(f"c-{model_type}-{uid}")
    m = ModelRepo(db).create(Model(name="M", cartridge_id=cart.id, model_type=model_type, community_model_uid=uid))
    SettingsRepo(db).set_active_model_id(m.id)
    return m


def test_train_tab_shows_for_your_own_model(root, tmp_path, monkeypatch) -> None:
    db = _db(tmp_path, monkeypatch)
    _activate(db, model_type="Standard")
    assert _VisibilityHarness(root, db).train_tab_visible() is True


def test_train_tab_shows_for_a_model_you_shared(root, tmp_path, monkeypatch) -> None:
    """A UID from sharing your own model must not cost you the Train tab."""
    db = _db(tmp_path, monkeypatch)
    _activate(db, model_type="Standard", uid="uid-mine")
    assert _VisibilityHarness(root, db).train_tab_visible() is True


def test_train_tab_hides_for_a_community_download(root, tmp_path, monkeypatch) -> None:
    db = _db(tmp_path, monkeypatch)
    _activate(db, model_type="CommunityManaged", uid="uid-theirs")
    assert _VisibilityHarness(root, db).train_tab_visible() is False


def test_train_tab_hides_for_a_readonly_model(root, tmp_path, monkeypatch) -> None:
    db = _db(tmp_path, monkeypatch)
    _activate(db, model_type="ReadOnly")
    assert _VisibilityHarness(root, db).train_tab_visible() is False


def test_train_tab_hides_in_ai_config_mode(root, tmp_path, monkeypatch) -> None:
    db = _db(tmp_path, monkeypatch)
    SettingsRepo(db).clear_active_model()
    assert _VisibilityHarness(root, db).train_tab_visible() is False


# ----- the tab's own guard --------------------------------------------------


class _FakeApp:
    def __init__(self, db) -> None:
        self.db = db
        self.auth = None
        self.broker = None

    def capture_frame(self):
        return None

    def run_worker(self, fn, *, on_done=None, on_error=None):
        result = fn()
        if on_done:
            on_done(result)

    def set_status(self, _msg):
        pass


def test_start_training_refuses_a_community_model(
    root,
    tmp_path,
    monkeypatch,
) -> None:
    """Defense in depth: the tab is hidden, but if the active model changes
    while it's open, Start must still refuse rather than fork someone else's
    model."""
    db = _db(tmp_path, monkeypatch)
    _activate(db, model_type="CommunityManaged", uid="uid-theirs")
    tab = TrainTab(root, config=Config(db).load(), bus=EventBus(), app=_FakeApp(db))
    try:
        shown = []
        monkeypatch.setattr(
            "sorter.ui.tab_train.messagebox.showinfo",
            lambda *a, **k: shown.append(a),
        )
        spawned = []
        monkeypatch.setattr(
            tab.training_manager,
            "spawn",
            lambda *a, **k: spawned.append(a),
        )
        tab._start_training()
        assert spawned == []
        assert shown and "Community model" in shown[0][0]
    finally:
        tab.destroy()
