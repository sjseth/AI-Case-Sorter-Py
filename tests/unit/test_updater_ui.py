"""Update dialog + MainWindow update wiring (needs a Tk display)."""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk
from tkinter import ttk

import requests

from sorter import updater
from sorter.db import Database
from sorter.repository import SettingsRepo
from sorter.ui.app import MainWindow
from sorter.ui.dialog_update import UpdateDialog
from sorter.ui.theme import apply_theme
from sorter.updater import PendingUpdate, UpdateInfo


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    apply_theme(r)
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def _data_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CASESORTER_UPDATE_DISABLED", raising=False)


class _FakeApp:
    def __init__(self, db=None) -> None:
        self.db = db
        self.noted: PendingUpdate | None = None
        self.closed = False

    def note_pending_update(self, pending) -> None:
        self.noted = pending

    def _on_close(self) -> None:
        self.closed = True


def _info() -> UpdateInfo:
    return UpdateInfo(
        version="9.9.9", tag="v9.9.9", url="https://x/app.tar.gz", notes="- Faster sorting", size=2_500_000
    )


def _archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in {
            "ai_case_sorter-9.9.9/main.py": "new\n",
            "ai_case_sorter-9.9.9/sorter/__init__.py": '__version__ = "9.9.9"\n',
        }.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _pump_until(root, predicate, *, timeout: float = 5.0) -> bool:
    """Drive the Tk event loop until `predicate` holds or we give up.

    The download runs on a worker thread and marshals results back with
    `after(0, ...)`, so the test has to both wait for the thread and pump the
    event loop for those callbacks to fire.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _StreamResp:
    def __init__(self, payload: bytes, url: str = "https://x/app.tar.gz") -> None:
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload))}
        # requests exposes the *final* URL after redirects; _download
        # re-checks it, since a redirect can downgrade https -> http.
        self.url = url

    def raise_for_status(self) -> None: ...

    def iter_content(self, chunk_size: int = 1):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ----- dialog states ----------------------------------------------------------


def test_dialog_shows_an_available_update(root) -> None:
    dlg = UpdateDialog(root, info=_info(), app=_FakeApp())
    assert dlg._title_var.get() == "Update available"
    assert updater.current_version() in dlg._version_var.get()
    assert "9.9.9" in dlg._version_var.get()
    assert "2.5 MB" in dlg._detail_var.get()
    assert "Faster sorting" in dlg._notes.get("1.0", tk.END)
    assert dlg._primary.cget("text") == "Download & Install"
    dlg.destroy()


def test_dialog_reports_being_up_to_date(root) -> None:
    dlg = UpdateDialog(root, info=None, app=_FakeApp())
    assert dlg._title_var.get() == "You're up to date"
    # Nothing to do -> no primary action offered.
    assert dlg._primary.winfo_manager() == ""
    assert dlg._secondary.cget("text") == "Close"
    dlg.destroy()


def test_dialog_jumps_straight_to_restart_when_already_staged(root) -> None:
    pending = PendingUpdate(version="9.9.9", tag="v9.9.9", path=Path("/tmp/x"), staged_at="now")
    dlg = UpdateDialog(root, info=None, app=_FakeApp(), pending=pending)
    assert dlg._title_var.get() == "Update ready to install"
    assert dlg._primary.cget("text") == "Restart Now"
    dlg.destroy()


def test_dialog_picks_up_a_staged_update_it_was_not_told_about(root, monkeypatch) -> None:
    """A user who picked "Later" reopens the dialog and gets the restart prompt."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _StreamResp(_archive()))
    updater.stage_update(_info())

    dlg = UpdateDialog(root, info=None, app=_FakeApp())
    assert dlg._title_var.get() == "Update ready to install"
    dlg.destroy()


# ----- download flow ----------------------------------------------------------


def test_download_stages_and_switches_to_restart(root, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _StreamResp(_archive()))
    app = _FakeApp()
    dlg = UpdateDialog(root, info=_info(), app=app)

    dlg._on_primary()
    assert _pump_until(root, lambda: dlg._pending is not None), "download never completed"
    assert dlg._pending is not None
    assert dlg._pending.version == "9.9.9"
    assert dlg._primary.cget("text") == "Restart Now"
    # The app is told, so the status-bar button can flip to "Restart to update".
    assert app.noted is not None and app.noted.version == "9.9.9"
    # Staged, not applied.
    assert (updater.pending_dir() / "main.py").is_file()
    dlg.destroy()


def test_download_failure_offers_a_retry(root, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _StreamResp(b"not a tarball"))
    dlg = UpdateDialog(root, info=_info(), app=_FakeApp())

    dlg._on_primary()
    assert _pump_until(root, lambda: dlg._primary.cget("text") == "Try Again"), "failure was never surfaced"
    assert str(dlg._primary.cget("state")) == "normal"
    assert "tar.gz" in dlg._progress_var.get()
    assert updater.pending_update() is None
    dlg.destroy()


def test_restart_without_a_launcher_does_not_close_the_app(root, monkeypatch) -> None:
    """A bare `python main.py` checkout has no start script to re-exec."""
    monkeypatch.setattr(updater, "launcher_path", lambda: None)
    pending = PendingUpdate(version="9.9.9", tag="v9.9.9", path=Path("/tmp/x"), staged_at="now")
    app = _FakeApp()
    dlg = UpdateDialog(root, info=None, app=app, pending=pending)

    dlg._on_primary()
    assert app.closed is False
    assert "start it again" in dlg._detail_var.get()
    dlg.destroy()


# ----- the startup-check opt-out ---------------------------------------------


def test_auto_check_setting_round_trips(root, tmp_path: Path) -> None:
    db = Database()
    db.ensure_initialized()
    try:
        dlg = UpdateDialog(root, info=_info(), app=_FakeApp(db))
        assert dlg._auto_var.get() is True  # default on

        dlg._auto_var.set(False)
        dlg._on_toggle_auto()
        assert SettingsRepo(db).get(updater.SETTING_CHECK_ON_STARTUP) is False

        # A freshly opened dialog reflects the stored choice.
        dlg2 = UpdateDialog(root, info=_info(), app=_FakeApp(db))
        assert dlg2._auto_var.get() is False
        dlg.destroy()
        dlg2.destroy()
    finally:
        db.close()


# ----- MainWindow wiring ------------------------------------------------------


class _StubWindow:
    """Just enough MainWindow surface to exercise the update helpers.

    Constructing a real MainWindow starts the camera and serial auto-connect,
    which no test should do. The methods under test are borrowed from
    MainWindow unchanged, so this exercises the real implementations against
    stub state rather than a reimplementation of them.
    """

    _check_for_updates_on_startup = MainWindow._check_for_updates_on_startup
    _auto_check_enabled = MainWindow._auto_check_enabled
    check_for_updates = MainWindow.check_for_updates
    note_pending_update = MainWindow.note_pending_update
    _show_update_button = MainWindow._show_update_button

    def __init__(self, root, db=None) -> None:
        self.db = db
        self.root = root
        self.update_button_var = tk.StringVar(value="Update available")
        self.update_button = ttk.Button(root, textvariable=self.update_button_var)
        self._update_info = None
        self._pending_update = None
        self.status = ""
        self.worker_calls = 0

    def set_status(self, msg: str) -> None:
        self.status = msg

    def run_worker(self, fn, *, on_done=None, on_error=None) -> None:
        self.worker_calls += 1
        try:
            result = fn()
        except Exception as exc:
            if on_error:
                on_error(exc)
        else:
            if on_done:
                on_done(result)


def test_startup_check_reveals_the_button_when_an_update_exists(root, monkeypatch) -> None:
    win = _StubWindow(root)
    monkeypatch.setattr(updater, "check_for_update", lambda **k: _info())

    win._check_for_updates_on_startup()
    root.update()

    assert win.update_button.winfo_manager() == "pack"
    assert win.update_button_var.get() == "Update to 9.9.9"


def test_startup_check_stays_quiet_when_current(root, monkeypatch) -> None:
    win = _StubWindow(root)
    monkeypatch.setattr(updater, "check_for_update", lambda **k: None)

    win._check_for_updates_on_startup()
    root.update()

    assert win.update_button.winfo_manager() == ""
    assert win.status == ""


def test_startup_check_swallows_network_errors(root, monkeypatch) -> None:
    win = _StubWindow(root)

    def _boom(**k):
        raise updater.UpdateError("offline")

    monkeypatch.setattr(updater, "check_for_update", _boom)
    win._check_for_updates_on_startup()
    root.update()

    assert win.update_button.winfo_manager() == ""
    assert win.status == ""  # a silent check never nags


def test_explicit_check_reports_failure(root, monkeypatch) -> None:
    win = _StubWindow(root)

    def _boom(**k):
        raise updater.UpdateError("offline")

    monkeypatch.setattr(updater, "check_for_update", _boom)
    win.check_for_updates(silent=False)
    root.update()

    assert "offline" in win.status


def test_staged_update_outranks_a_fresh_check(root, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _StreamResp(_archive()))
    updater.stage_update(_info())

    win = _StubWindow(root)

    def _must_not_run(**k):
        raise AssertionError("must not check when an update is already staged")

    monkeypatch.setattr(updater, "check_for_update", _must_not_run)
    win._check_for_updates_on_startup()
    root.update()

    assert win.update_button_var.get() == "Restart to update"
    assert win.update_button.winfo_manager() == "pack"


def test_startup_check_honours_the_disable_env(root, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_UPDATE_DISABLED", "1")
    win = _StubWindow(root)

    def _must_not_run(**k):
        raise AssertionError("must not check when disabled")

    monkeypatch.setattr(updater, "check_for_update", _must_not_run)
    win._check_for_updates_on_startup()
    assert win.worker_calls == 0


def test_startup_check_honours_the_stored_opt_out(root, monkeypatch) -> None:
    db = Database()
    db.ensure_initialized()
    try:
        SettingsRepo(db).set(updater.SETTING_CHECK_ON_STARTUP, False)
        win = _StubWindow(root, db)

        def _must_not_run(**k):
            raise AssertionError("must not check when opted out")

        monkeypatch.setattr(updater, "check_for_update", _must_not_run)
        win._check_for_updates_on_startup()
        assert win.worker_calls == 0
    finally:
        db.close()
