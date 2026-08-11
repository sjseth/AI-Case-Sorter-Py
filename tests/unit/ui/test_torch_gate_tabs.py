"""The install gate as wired into the tabs (needs a Tk display).

The regression these lock down: a fresh install had no torch, and nothing on
the download → activate → Start path checked for it, so the run died on the
first case with a `pip install .[ml]` message. Only the Train tab's
"Start training" button ever offered the install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk

from sorter.control.events import EventBus
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, ModelRepo, SettingsRepo
from sorter.ui import torch_gate
from sorter.ui.tab_run import RunTab
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


class _FakeDialog:
    instances: list[_FakeDialog] = []

    def __init__(self, parent, *, on_success=None, on_cancel=None, reason=None):
        self.on_success = on_success
        self.on_cancel = on_cancel
        self.reason = reason
        _FakeDialog.instances.append(self)


@pytest.fixture
def fake_dialog(monkeypatch):
    _FakeDialog.instances = []
    monkeypatch.setattr(torch_gate, "TorchInstallDialog", _FakeDialog)
    return _FakeDialog


@pytest.fixture
def no_torch(monkeypatch):
    """Start with torch absent; flip `installed` to model a finished pip run.

    The gate re-checks on re-entry, so a test that fires `on_success` without
    flipping this would just get a second dialog — which is exactly what the
    app does when an "successful" install still leaves torch unimportable.
    """
    state = {"installed": False}
    monkeypatch.setattr(
        torch_gate.local_inference,
        "is_installed",
        lambda: state["installed"],
    )
    return state


class _FakeController:
    def __init__(self) -> None:
        self.is_running = False
        self.started = 0
        self.cycles = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        pass

    def clear_wish_list(self) -> None:
        pass

    def cycle_once(self) -> None:
        self.cycles += 1


class _FakeBroker:
    is_connected = True

    def force_sort_and_move(self, slot: int) -> bool:
        return True


class _FakeApp:
    def __init__(self, db) -> None:
        self.db = db
        self.auth = None
        self.broker = _FakeBroker()
        self.run_controller = _FakeController()

    def capture_frame(self):
        return None

    def run_worker(self, fn, *, on_done=None, on_error=None):
        try:
            result = fn()
        except Exception as exc:
            if on_error:
                on_error(exc)
        else:
            if on_done:
                on_done(result)

    def set_status(self, _msg):
        pass


def _db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    db = Database(tmp_path / "t.db")
    db.ensure_initialized()
    return db


def _activate_local_model(db: Database, tmp_path: Path) -> Model:
    cart = CartridgeRepo(db).create("9mm-tabs")
    ckpt = tmp_path / "trained.pth"
    ckpt.write_bytes(b"CHECKPOINT")
    m = ModelRepo(db).create(Model(name="Downloaded", cartridge_id=cart.id))
    m.model_path = str(ckpt)
    ModelRepo(db).update(m)
    SettingsRepo(db).set_active_model_id(m.id)
    return m


# ----- Run tab --------------------------------------------------------------


def test_start_offers_install_and_does_not_start_the_run(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """The whole point: a downloaded model + Start must not feed a case."""
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._toggle_run()
        assert len(fake_dialog.instances) == 1
        assert app.run_controller.started == 0  # nothing fed
        # Install completes → the run starts, no second click needed.
        no_torch["installed"] = True
        fake_dialog.instances[0].on_success()
        assert app.run_controller.started == 1
    finally:
        tab.destroy()


def test_ai_config_mode_start_is_never_gated(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """An AI Config user classifies over HTTP and must never see the prompt."""
    db = _db(tmp_path, monkeypatch)
    SettingsRepo(db).clear_active_model()
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._toggle_run()
        assert fake_dialog.instances == []
        assert app.run_controller.started == 1
    finally:
        tab.destroy()


def test_start_is_not_gated_when_torch_is_present(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
) -> None:
    monkeypatch.setattr(torch_gate.local_inference, "is_installed", lambda: True)
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._toggle_run()
        assert fake_dialog.instances == []
        assert app.run_controller.started == 1
    finally:
        tab.destroy()


def test_manual_feed_offers_install_and_does_not_cycle(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._manual_feed()
        assert len(fake_dialog.instances) == 1
        assert app.run_controller.cycles == 0
        no_torch["installed"] = True
        fake_dialog.instances[0].on_success()
        assert app.run_controller.cycles == 1
    finally:
        tab.destroy()


def test_stopping_a_run_is_never_gated(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """Stop must always work — never trap a running machine behind a modal."""
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._set_running(True)
        tab._toggle_run()
        assert fake_dialog.instances == []
    finally:
        tab.destroy()


def test_start_refuses_a_model_whose_checkpoint_is_missing(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """The data-folder-rename case: refuse, don't quietly sort via HTTP.

    Also must not offer the torch install — torch isn't the problem, and
    installing 2 GB wouldn't fix a missing checkpoint.
    """
    db = _db(tmp_path, monkeypatch)
    m = _activate_local_model(db, tmp_path)
    assert m.model_path is not None  # _activate_local_model always sets one
    Path(m.model_path).unlink()  # data folder renamed / model deleted
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        errors = []
        monkeypatch.setattr(
            "sorter.ui.tab_run.messagebox.showerror",
            lambda *a, **k: errors.append(a),
        )
        tab._toggle_run()
        assert app.run_controller.started == 0
        assert fake_dialog.instances == []
        assert errors and "isn't there" in errors[0][1]
    finally:
        tab.destroy()


def test_manual_feed_refuses_a_model_whose_checkpoint_is_missing(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    db = _db(tmp_path, monkeypatch)
    m = _activate_local_model(db, tmp_path)
    assert m.model_path is not None  # _activate_local_model always sets one
    Path(m.model_path).unlink()
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        monkeypatch.setattr(
            "sorter.ui.tab_run.messagebox.showerror",
            lambda *a, **k: None,
        )
        tab._manual_feed()
        assert app.run_controller.cycles == 0
    finally:
        tab.destroy()


def test_ai_config_start_is_not_blocked_by_the_checkpoint_check(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """AI Config mode has no checkpoint by definition — it must still run."""
    db = _db(tmp_path, monkeypatch)
    SettingsRepo(db).clear_active_model()
    app = _FakeApp(db)
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        errors = []
        monkeypatch.setattr(
            "sorter.ui.tab_run.messagebox.showerror",
            lambda *a, **k: errors.append(a),
        )
        tab._toggle_run()
        assert errors == []
        assert app.run_controller.started == 1
    finally:
        tab.destroy()


def test_stop_still_works_with_a_missing_checkpoint(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """Never trap a running machine behind a dialog it can't clear."""
    db = _db(tmp_path, monkeypatch)
    m = _activate_local_model(db, tmp_path)
    assert m.model_path is not None  # _activate_local_model always sets one
    Path(m.model_path).unlink()
    app = _FakeApp(db)
    stopped = []
    monkeypatch.setattr(app.run_controller, "stop", lambda: stopped.append(1))
    tab = RunTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        monkeypatch.setattr(
            "sorter.ui.tab_run.messagebox.showerror",
            lambda *a, **k: None,
        )
        tab._set_running(True)
        tab._toggle_run()
        assert stopped == [1]
    finally:
        tab.destroy()


# ----- Train tab ------------------------------------------------------------


def test_train_feed_offers_install_but_still_feeds_when_declined(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """Capturing images must survive a declined install.

    Building a training set is exactly the no-torch workflow, so the Feed
    button offers the install rather than gating on it.
    """
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    app = _FakeApp(db)
    tab = TrainTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        fed = []
        monkeypatch.setattr(app.broker, "force_sort_and_move", lambda slot: fed.append(slot) or True)
        tab._feed()
        assert len(fake_dialog.instances) == 1
        assert fed == []  # held while the modal is up
        fake_dialog.instances[0].on_cancel()  # "no thanks"
        assert fed == [0]  # …and the feed still happened
    finally:
        tab.destroy()


def test_train_feed_asks_only_once_per_session(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    app = _FakeApp(db)
    tab = TrainTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._feed()
        fake_dialog.instances[0].on_cancel()
        tab._feed()
        tab._feed()
        assert len(fake_dialog.instances) == 1  # not nagged on every case
    finally:
        tab.destroy()


def test_train_feed_preserves_the_sort_label_across_the_prompt(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """The auto-feed chain passes a label; the prompt must not drop it."""
    db = _db(tmp_path, monkeypatch)
    _activate_local_model(db, tmp_path)
    cfg = Config(db).load()
    cfg.add_headstamp("FC", slot=5)
    app = _FakeApp(db)
    tab = TrainTab(root, config=cfg, bus=EventBus(), app=app)
    try:
        fed = []
        monkeypatch.setattr(app.broker, "force_sort_and_move", lambda slot: fed.append(slot) or True)
        tab._sort_while_training_var.set(True)
        tab._feed(sort_label="FC")
        fake_dialog.instances[0].on_cancel()
        assert fed == [5]  # FC's slot, not the catch-all
    finally:
        tab.destroy()


def test_train_feed_never_prompts_for_a_model_with_no_checkpoint(
    root,
    tmp_path,
    monkeypatch,
    fake_dialog,
    no_torch,
) -> None:
    """A brand-new model predicts nothing, so there's nothing to install for."""
    db = _db(tmp_path, monkeypatch)
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    app = _FakeApp(db)
    tab = TrainTab(root, config=Config(db).load(), bus=EventBus(), app=app)
    try:
        tab._feed()
        assert fake_dialog.instances == []
    finally:
        tab.destroy()
