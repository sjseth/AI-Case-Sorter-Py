"""UI-level tests for the feedback-loop touchpoints.

Needs a Tk display (run under ``xvfb-run`` in headless CI). Skipped where the
GUI stack isn't importable, like the other UI tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk

from sorter import paths
from sorter.control.events import EventBus
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.data.models import Model
from sorter.data.repository import (
    CartridgeRepo,
    ModelRepo,
    SettingsRepo,
)
from sorter.ui.dialog_model_editor import (
    _FEEDBACK_MODE_BY_LABEL,
    _FEEDBACK_MODE_LABELS,
    ModelEditorDialog,
    _is_community_model,
)
from sorter.ui.tab_run import RunTab


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:  # no display
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    database = Database(tmp_path / "ui.db")
    database.ensure_initialized()
    return database


def _community_model(db, *, enabled=True, floor=92, mode="Instant") -> Model:
    cart = CartridgeRepo(db).get_or_create("9mm")
    m = Model(
        name="Comm",
        cartridge_id=cart.id,
        community_model_uid="uid-1",
        model_version=2,
        feedback_loop_enabled=enabled,
        feedback_loop_confidence_floor=floor,
        feedback_loop_upload_mode=mode,
    )
    return ModelRepo(db).create(m)


def _stage_feedback_file(model_id: int) -> None:
    """Drop a dummy staged feedback JPEG so has_pending() sees a queue."""
    d = paths.model_feedback_dir(model_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "WIN__10__639180000000000000.jpg").write_bytes(b"x")


class _FakeApp:
    def __init__(self, db) -> None:
        self.db = db
        self.auth = None
        self.run_controller = None
        self.broker = None
        self.status = ""
        self.worker_calls: list = []

    def run_worker(self, fn, *, on_done=None, on_error=None):
        self.worker_calls.append((fn, on_done, on_error))

    def set_status(self, msg):
        self.status = msg


# ----- pure helpers ----------------------------------------------------------


def test_is_community_model_helper() -> None:
    assert _is_community_model(Model(community_model_uid="u"))
    assert _is_community_model(Model(model_type="CommunityManaged"))
    assert not _is_community_model(Model())
    assert not _is_community_model(None)


def test_feedback_mode_label_mapping_round_trips() -> None:
    from sorter.data.models import FEEDBACK_UPLOAD_MODES

    for value in FEEDBACK_UPLOAD_MODES:
        assert _FEEDBACK_MODE_BY_LABEL[_FEEDBACK_MODE_LABELS[value]] == value


# ----- editor opt-out dialog -------------------------------------------------


def test_editor_shows_feedback_section_for_community_model(root, db) -> None:
    m = _community_model(db)
    dlg = ModelEditorDialog(root, db, existing=m)
    try:
        assert dlg._is_community is True
        assert dlg.fb_enabled_var.get() is True
        # Combobox enabled because feedback is on.
        assert str(dlg.fb_mode_combo.cget("state")) == "readonly"
    finally:
        dlg.destroy()


def test_editor_hides_feedback_section_for_standard_model(root, db) -> None:
    cart = CartridgeRepo(db).get_or_create("9mm")
    m = ModelRepo(db).create(Model(name="Std", cartridge_id=cart.id))
    dlg = ModelEditorDialog(root, db, existing=m)
    try:
        assert dlg._is_community is False
        assert not hasattr(dlg, "fb_enabled_var")
    finally:
        dlg.destroy()


def test_editor_save_opts_out_and_changes_mode_but_not_floor(root, db) -> None:
    m = _community_model(db, enabled=True, floor=92, mode="Instant")
    saved_ids: list[int] = []
    dlg = ModelEditorDialog(root, db, existing=m, on_saved=saved_ids.append)
    # User opts out and switches the upload mode.
    dlg.fb_enabled_var.set(False)
    dlg.fb_mode_var.set(_FEEDBACK_MODE_LABELS["OnRunComplete"])
    dlg._save()

    assert saved_ids == [m.id]
    reloaded = ModelRepo(db).get(m.id)
    assert reloaded is not None
    assert reloaded.feedback_loop_enabled is False  # opted out
    assert reloaded.feedback_loop_upload_mode == "OnRunComplete"
    assert reloaded.feedback_loop_confidence_floor == 92  # floor untouched


def test_editor_toggle_disables_mode_combo(root, db) -> None:
    m = _community_model(db, enabled=True)
    dlg = ModelEditorDialog(root, db, existing=m)
    try:
        dlg.fb_enabled_var.set(False)
        dlg._on_fb_toggle()
        assert str(dlg.fb_mode_combo.cget("state")) == "disabled"
    finally:
        dlg.destroy()


# ----- run-tab upload button + routing ---------------------------------------


def _make_run_tab(root, db):
    SettingsRepo(db)  # ensure table touched
    cfg = Config(db).load()
    app = _FakeApp(db)
    tab = RunTab(root, config=cfg, bus=EventBus(), app=app)
    return tab, app, cfg


def test_manual_button_visible_only_with_pending(root, db) -> None:
    m = _community_model(db, mode="Manual")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, _app, _cfg = _make_run_tab(root, db)
    try:
        # No pending rows yet → hidden.
        tab._update_feedback_button()
        assert tab._feedback_btn.winfo_manager() == ""
        # Enqueue a pending row → button shows.
        _stage_feedback_file(m.id)
        tab._update_feedback_button()
        assert tab._feedback_btn.winfo_manager() == "pack"
    finally:
        tab.destroy()


def test_manual_button_hidden_for_instant_mode(root, db) -> None:
    m = _community_model(db, mode="Instant")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, _app, _cfg = _make_run_tab(root, db)
    try:
        _stage_feedback_file(m.id)
        tab._update_feedback_button()
        # Instant uploads automatically — no manual button even with a queue.
        assert tab._feedback_btn.winfo_manager() == ""
    finally:
        tab.destroy()


def test_feedback_queued_instant_triggers_drain(root, db) -> None:
    m = _community_model(db, mode="Instant")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, _app, _cfg = _make_run_tab(root, db)
    try:
        calls: list[int] = []
        tab._trigger_feedback_drain = lambda mid: calls.append(mid)
        tab._on_feedback_queued({"model_id": m.id, "upload_mode": "Instant"})
        assert calls == [m.id]
        # Manual mode should NOT auto-drain on queue.
        calls.clear()
        tab._on_feedback_queued({"model_id": m.id, "upload_mode": "Manual"})
        assert calls == []
    finally:
        tab.destroy()


def test_feedback_queued_skips_declined_model(root, db) -> None:
    m = _community_model(db, mode="Instant")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, _app, _cfg = _make_run_tab(root, db)
    try:
        tab._feedback_declined_models.add(m.id)
        calls: list[int] = []
        tab._trigger_feedback_drain = lambda mid: calls.append(mid)
        tab._on_feedback_queued({"model_id": m.id, "upload_mode": "Instant"})
        assert calls == []  # declined this session → no further attempts
    finally:
        tab.destroy()


def test_trigger_drain_coalesces_while_inflight(root, db) -> None:
    m = _community_model(db, mode="Instant")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, app, _cfg = _make_run_tab(root, db)
    try:
        tab._trigger_feedback_drain(m.id)
        tab._trigger_feedback_drain(m.id)  # coalesced — second is a no-op
        assert len(app.worker_calls) == 1
        assert tab._feedback_upload_inflight is True
    finally:
        tab.destroy()


# ----- run-tab wish-list fetch ------------------------------------------------


class _FakeController:
    """Stands in for RunController's wish-list surface."""

    def __init__(self) -> None:
        self.cleared = 0
        self.refreshed: list = []

    def refresh_wish_list(self, *, auth):
        self.refreshed.append(auth)
        return ["FC"]

    def clear_wish_list(self) -> None:
        self.cleared += 1


def test_wish_list_fetched_on_worker_at_run_start(root, db) -> None:
    m = _community_model(db, mode="Instant")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, app, _cfg = _make_run_tab(root, db)
    try:
        app.auth = object()
        ctrl = _FakeController()
        tab._begin_wish_list_fetch(ctrl)
        # Stale list dropped up front, fetch deferred to a worker thread so the
        # run isn't blocked on the round-trip.
        assert ctrl.cleared == 1
        assert len(app.worker_calls) == 1
        assert app.worker_calls[0][0]() == ["FC"]
        assert ctrl.refreshed == [app.auth]
    finally:
        tab.destroy()


def test_wish_list_not_fetched_when_signed_out(root, db) -> None:
    m = _community_model(db, mode="Instant")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, app, _cfg = _make_run_tab(root, db)
    try:
        app.auth = None
        ctrl = _FakeController()
        tab._begin_wish_list_fetch(ctrl)
        assert app.worker_calls == []
        assert ctrl.cleared == 1
    finally:
        tab.destroy()


def test_wish_list_not_fetched_when_feedback_loop_off(root, db) -> None:
    """No community-model round-trip (and no auth touch) for an opted-out model."""
    m = _community_model(db, enabled=False)
    SettingsRepo(db).set_active_model_id(m.id)
    tab, app, _cfg = _make_run_tab(root, db)
    try:
        app.auth = object()
        ctrl = _FakeController()
        tab._begin_wish_list_fetch(ctrl)
        assert app.worker_calls == []
        assert ctrl.refreshed == []
    finally:
        tab.destroy()


def test_wish_list_cleared_when_the_run_stops(root, db) -> None:
    m = _community_model(db, mode="Manual")
    SettingsRepo(db).set_active_model_id(m.id)
    tab, app, _cfg = _make_run_tab(root, db)
    try:
        ctrl = _FakeController()
        app.run_controller = ctrl
        tab._on_run_stopped()
        assert ctrl.cleared == 1
    finally:
        tab.destroy()
