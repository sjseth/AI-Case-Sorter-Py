"""Run-tab sorting-template bar wiring (needs a Tk display)."""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk

from sorter.control.events import EventBus
from sorter.data.config import DEFAULT_SLOT_TEMPLATE_NAME, Config
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


def test_bar_shows_the_default_template(root, tmp_path, monkeypatch) -> None:
    tab, _app, _cfg = _make(root, tmp_path, monkeypatch)
    try:
        assert list(tab._template_combo["values"]) == [DEFAULT_SLOT_TEMPLATE_NAME]
        assert tab._template_var.get() == DEFAULT_SLOT_TEMPLATE_NAME
        assert tab._template_hint_var.get() == ""
    finally:
        tab.destroy()


def test_selecting_a_template_applies_its_layout(root, tmp_path, monkeypatch) -> None:
    tab, app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        cfg.set_headstamp_slot("CBC", 2)
        default = cfg.active_slot_template()
        cfg.create_slot_template("Blank", copy_current=False)
        tab._refresh_templates()

        tab._template_combo.current([t.name for t in tab._templates].index(default.name))
        tab._on_template_selected()

        assert cfg.active_slot_template().id == default.id
        assert {h["name"]: h["slot"] for h in cfg.headstamps}["CBC"] == 2
        assert "Default" in app.status
        # Slot cards re-rendered against the loaded layout.
        card = next(c for c in tab._slot_cards if c.slot_number == 2)
        assert card._headstamps_var.get() == "CBC"
    finally:
        tab.destroy()


def test_package_mode_swaps_the_template_list(root, tmp_path, monkeypatch) -> None:
    tab, _app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        cfg.create_slot_template("Standard only")
        tab._refresh_templates()
        assert "Standard only" in list(tab._template_combo["values"])

        tab._package_var.set(True)
        tab._on_toggle_package_mode()
        assert list(tab._template_combo["values"]) == [DEFAULT_SLOT_TEMPLATE_NAME]
        assert tab._template_hint_var.get() == "Package-mode layout"

        tab._package_var.set(False)
        tab._on_toggle_package_mode()
        assert "Standard only" in list(tab._template_combo["values"])
    finally:
        tab.destroy()


def test_templates_are_locked_while_a_run_is_active(root, tmp_path, monkeypatch) -> None:
    tab, _app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        default = cfg.active_slot_template()
        other = cfg.create_slot_template("Other", copy_current=False)
        tab._refresh_templates()
        tab._set_running(True)

        warned = {}
        monkeypatch.setattr(
            "sorter.ui.tab_run.messagebox.showinfo",
            lambda *a, **k: warned.setdefault("shown", True),
        )
        tab._template_combo.current([t.name for t in tab._templates].index(default.name))
        tab._on_template_selected()

        assert warned.get("shown") is True
        assert cfg.active_slot_template().id == other.id  # unchanged
        assert tab._template_var.get() == other.name  # combobox snapped back
    finally:
        tab.destroy()


def test_active_model_change_reloads_the_template_list(root, tmp_path, monkeypatch) -> None:
    tab, _app, cfg = _make(root, tmp_path, monkeypatch)
    try:
        cfg.create_slot_template("Model one")
        tab._refresh_templates()
        SettingsRepo(cfg.db).clear_active_model()  # -> AI Config mode
        tab._on_active_model_changed()
        assert list(tab._template_combo["values"]) == [DEFAULT_SLOT_TEMPLATE_NAME]
    finally:
        tab.destroy()
