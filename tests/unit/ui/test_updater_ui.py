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

from sorter.data.db import Database
from sorter.data.repository import SettingsRepo
from sorter.ui import markdown_render
from sorter.ui.app import CHECK_FOR_UPDATES_LABEL, MainWindow
from sorter.ui.dialog_update import UpdateDialog
from sorter.ui.markdown_render import render_release_notes
from sorter.ui.theme import PALETTE, apply_theme, get_fonts
from sorter.update import updater
from sorter.update.updater import PendingUpdate, UpdateInfo


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
    """The shape a real sdist has: src/ layout, plus the root main.py shim.

    Was the pre-#58 flat layout, which passed only because
    ``REQUIRED_ENTRY_SETS`` still carries the legacy tuple for archives
    "somehow received" -- so the one end-to-end download-and-stage test in the
    suite exercised an archive the project no longer produces, and would have
    broken on the day that tuple is finally dropped, for a reason having
    nothing to do with the UI.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in {
            "ai_case_sorter-9.9.9/main.py": "new\n",
            "ai_case_sorter-9.9.9/bootstrap.py": "new\n",
            "ai_case_sorter-9.9.9/src/sorter/__init__.py": '__version__ = "9.9.9"\n',
            "ai_case_sorter-9.9.9/src/sorter/__main__.py": "new\n",
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
    # Staged, not applied. Both entry points, since both ship and a launcher
    # in the field may run either (see main.py's docstring).
    assert (updater.pending_dir() / "src" / "sorter" / "__main__.py").is_file()
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


# ----- version picker -----------------------------------------------------


def _releases_payload() -> list[dict]:
    return [
        {
            "tag_name": "v9.9.9",
            "body": "- Faster sorting",
            "assets": [],
            "published_at": "2026-03-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v9.9.9-rc1",
            "body": "- Release candidate",
            "assets": [],
            "published_at": "2026-02-15T00:00:00Z",
            "draft": False,
            "prerelease": True,
        },
        {
            "tag_name": "v9.9.8",
            "body": "- Older release",
            "assets": [],
            "published_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]


class _ListResp:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_dialog_defaults_to_latest_without_opening_the_picker(root, monkeypatch) -> None:
    """A user who never touches "Choose a different version..." must see
    exactly the pre-picker behaviour -- including no network call beyond the
    single check MainWindow already made."""

    def _boom(*a, **k):
        raise AssertionError("must not fetch the release list unless asked")

    monkeypatch.setattr(requests, "get", _boom)
    dlg = UpdateDialog(root, info=_info(), app=_FakeApp())
    assert dlg._selected is dlg._info
    assert dlg._primary.cget("text") == "Download & Install"
    assert dlg._combo_row.winfo_manager() == ""
    dlg.destroy()


def test_picker_lists_releases_and_hides_prereleases_by_default(root, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _ListResp(200, _releases_payload()))
    dlg = UpdateDialog(root, info=_info(), app=_FakeApp())

    dlg._open_picker()
    assert _pump_until(root, lambda: dlg._combo_row.winfo_manager() != ""), "picker never appeared"

    assert len(dlg._releases) == 2  # the prerelease is excluded by default
    assert [r.tag for r in dlg._releases] == ["v9.9.9", "v9.9.8"]
    # Default selection stays the latest stable release passed in.
    assert dlg._selected is not None and dlg._selected.tag == "v9.9.9"
    dlg.destroy()


def test_picker_can_select_an_older_release_and_flags_it_as_not_newest(root, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _ListResp(200, _releases_payload()))
    dlg = UpdateDialog(root, info=_info(), app=_FakeApp())

    dlg._open_picker()
    assert _pump_until(root, lambda: dlg._combo_row.winfo_manager() != ""), "picker never appeared"

    dlg._version_combo.current(1)  # v9.9.8, the older release
    dlg._on_pick_version()

    assert dlg._selected is not None and dlg._selected.tag == "v9.9.8"
    assert "not the newest" in dlg._detail_var.get()
    assert "Older release" in dlg._notes.get("1.0", tk.END)
    # Installing the selection is still offered, older or not.
    assert dlg._primary.cget("text") == "Download & Install"
    assert str(dlg._primary.cget("state")) == "normal"
    dlg.destroy()


def test_picker_can_opt_into_prereleases(root, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _ListResp(200, _releases_payload()))
    dlg = UpdateDialog(root, info=_info(), app=_FakeApp())

    dlg._open_picker()
    assert _pump_until(root, lambda: dlg._combo_row.winfo_manager() != ""), "picker never appeared"

    dlg._show_prereleases_var.set(True)
    dlg._on_toggle_prereleases()
    assert _pump_until(root, lambda: len(dlg._releases) == 3), "prerelease never showed up"
    assert "v9.9.9-rc1" in [r.tag for r in dlg._releases]
    dlg.destroy()


def test_picker_selection_drives_what_gets_staged(root, monkeypatch) -> None:
    """Downloading after picking a release in the picker has to install
    *that* release, not whatever the plain check originally offered."""

    def _fake_get(url, *a, **k):
        if url.endswith("/releases"):
            return _ListResp(200, _releases_payload())
        return _StreamResp(_archive())

    monkeypatch.setattr(requests, "get", _fake_get)
    app = _FakeApp()
    dlg = UpdateDialog(root, info=_info(), app=app)

    dlg._open_picker()
    assert _pump_until(root, lambda: dlg._combo_row.winfo_manager() != ""), "picker never appeared"
    dlg._version_combo.current(1)  # v9.9.8
    dlg._on_pick_version()

    # stage_update is monkeypatched at the module level below so the fake
    # archive (built for tag 9.9.9) doesn't have to also match 9.9.8's name.
    staged: list = []
    real_stage_update = updater.stage_update

    def _spy_stage_update(info, **kwargs):
        staged.append(info)
        return real_stage_update(_info(), **kwargs)  # reuse a valid archive regardless of target

    monkeypatch.setattr(updater, "stage_update", _spy_stage_update)
    dlg._on_primary()
    assert _pump_until(root, lambda: dlg._pending is not None), "download never completed"
    assert staged and staged[0].tag == "v9.9.8"
    dlg.destroy()


def test_picker_available_when_already_up_to_date(root, monkeypatch) -> None:
    """Acceptance: installing an older/different version has to work even
    when the plain check says there's nothing newer."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _ListResp(200, _releases_payload()))
    dlg = UpdateDialog(root, info=None, app=_FakeApp())
    assert dlg._primary.winfo_manager() == ""  # nothing to install yet

    dlg._open_picker()
    assert _pump_until(root, lambda: dlg._combo_row.winfo_manager() != ""), "picker never appeared"
    dlg._version_combo.current(1)
    dlg._on_pick_version()

    assert dlg._primary.winfo_manager() != ""
    assert dlg._primary.cget("text") == "Download & Install"
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
    _on_update_button_click = MainWindow._on_update_button_click

    def __init__(self, root, db=None) -> None:
        self.db = db
        self.root = root
        self.update_button_var = tk.StringVar(value=CHECK_FOR_UPDATES_LABEL)
        self.update_button = ttk.Button(root, textvariable=self.update_button_var)
        # Mirrors MainWindow: always packed, the label carries the state.
        self.update_button.pack()
        self.dialogs_opened = 0
        self._update_info = None
        self._pending_update = None
        self.status = ""
        self.worker_calls = 0

    def set_status(self, msg: str) -> None:
        self.status = msg

    def open_update_dialog(self) -> None:
        # Counted rather than constructed: these tests are about when the app
        # decides to show the dialog, not about the dialog itself.
        self.dialogs_opened += 1

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
    """A silent startup check that finds nothing must not say anything.

    The button is always present now, so "quiet" is about the label and the
    status line, not about whether the widget is packed.
    """
    win = _StubWindow(root)
    monkeypatch.setattr(updater, "check_for_update", lambda **k: None)

    win._check_for_updates_on_startup()
    root.update()

    assert win.update_button_var.get() == CHECK_FOR_UPDATES_LABEL
    assert win.status == ""
    assert win.dialogs_opened == 0


def test_startup_check_swallows_network_errors(root, monkeypatch) -> None:
    win = _StubWindow(root)

    def _boom(**k):
        raise updater.UpdateError("offline")

    monkeypatch.setattr(updater, "check_for_update", _boom)
    win._check_for_updates_on_startup()
    root.update()

    # The button is always packed now, so "swallowed" means the failure never
    # reached the label, the status line, or a dialog.
    assert win.update_button_var.get() == CHECK_FOR_UPDATES_LABEL
    assert win.status == ""  # a silent check never nags
    assert win.dialogs_opened == 0


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


def test_update_button_is_always_available_as_a_manual_check(root, monkeypatch) -> None:
    """The button must be reachable with no update pending.

    It used to be packed only once a check had found something, which left a
    user who was up to date unable to open the dialog at all -- and so unable
    to read release notes or reach the version picker.
    """
    win = _StubWindow(root)
    root.update()

    assert win.update_button.winfo_manager() != ""
    assert win.update_button_var.get() == CHECK_FOR_UPDATES_LABEL


def test_manual_check_opens_the_dialog_even_when_up_to_date(root, monkeypatch) -> None:
    win = _StubWindow(root)
    monkeypatch.setattr(updater, "check_for_update", lambda **k: None)

    win._on_update_button_click()
    root.update()

    assert win.dialogs_opened == 1
    assert win.status == "You're up to date."
    assert win.update_button_var.get() == CHECK_FOR_UPDATES_LABEL


def test_manual_check_finding_a_release_relabels_the_button(root, monkeypatch) -> None:
    win = _StubWindow(root)
    monkeypatch.setattr(updater, "check_for_update", lambda **k: _info())

    win._on_update_button_click()
    root.update()

    assert win.update_button_var.get() == "Update to 9.9.9"


def test_button_click_skips_the_check_once_something_is_known(root, monkeypatch) -> None:
    """With a result already in hand the button is a shortcut, not a re-check."""
    win = _StubWindow(root)
    win._update_info = _info()

    def _boom(**k):
        raise AssertionError("must not re-check when a result is already known")

    monkeypatch.setattr(updater, "check_for_update", _boom)

    win._on_update_button_click()
    root.update()

    assert win.dialogs_opened == 1
    assert win.worker_calls == 0


def test_a_later_check_clears_a_stale_update_label(root, monkeypatch) -> None:
    """Going from "update found" back to "nothing" must not strand the label."""
    win = _StubWindow(root)
    win._show_update_button("Update to 9.9.9")
    monkeypatch.setattr(updater, "check_for_update", lambda **k: None)

    win.check_for_updates(silent=True)
    root.update()

    assert win.update_button_var.get() == CHECK_FOR_UPDATES_LABEL


# ----- release-notes Markdown rendering ---------------------------------------
#
# The subset covered here is exactly what cliff.toml's `body` template
# produces for this project's real releases (see markdown_render.py's module
# docstring): `###` headings, `-` bullets, a `**BREAKING**` bold prefix,
# `by @user` / `in #123` references, and inline `` `code` `` spans that show
# up routinely in this repo's commit-message style. Links / bare URLs are
# also covered even though the template never emits them, since the parser
# treats them the same defensive way.


def _notes_widget(root):
    fonts = get_fonts(root)
    return tk.Text(root), fonts


def test_heading_and_bullets_get_their_own_tags(root) -> None:
    widget, fonts = _notes_widget(root)
    render_release_notes(
        widget,
        "### Bug Fixes\n- Fix the thing\n- Fix another thing",
        fonts=fonts,
        repo="sjseth/AI-Case-Sorter-Py",
    )
    text = widget.get("1.0", tk.END)
    assert "Bug Fixes" in text
    assert text.count("•") == 2  # bullet glyph, not the literal "- "
    assert widget.tag_ranges("h3")  # the heading text carries the h3 tag
    assert widget.tag_ranges("bullet")
    assert str(widget.cget("state")) == "disabled"  # left read-only


def test_bold_breaking_marker_and_inline_code_get_their_tags(root) -> None:
    widget, fonts = _notes_widget(root)
    render_release_notes(
        widget,
        "- **BREAKING** Require Python 3.12, rename `ui.theme`",
        fonts=fonts,
        repo="sjseth/AI-Case-Sorter-Py",
    )
    text = widget.get("1.0", tk.END)
    # The `**`/`` ` `` markers themselves must not leak into the rendered text.
    assert "**" not in text
    assert "BREAKING Require Python 3.12" in text
    assert "`" not in text
    assert "ui.theme" in text
    assert widget.tag_ranges("bold")
    assert widget.tag_ranges("code")


def test_user_and_pr_references_become_clickable_links(root, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(markdown_render.webbrowser, "open", lambda url: opened.append(url))

    widget, fonts = _notes_widget(root)
    render_release_notes(
        widget,
        "- Stop installing dev dependencies by @jimisola in #50",
        fonts=fonts,
        repo="sjseth/AI-Case-Sorter-Py",
    )
    tags = widget._md_link_tags
    assert len(tags) == 2  # @jimisola and #50

    # Each link tag is colored (not left at the default text color) and
    # underlined, so it visually reads as a link.
    for tag in tags:
        assert widget.tag_cget(tag, "foreground") == PALETTE["accent"]
        assert widget.tag_cget(tag, "underline")
        assert widget.tag_ranges(tag)  # actually covers some text

    assert (
        markdown_render._pr_url("sjseth/AI-Case-Sorter-Py", "50")
        == "https://github.com/sjseth/AI-Case-Sorter-Py/issues/50"
    )

    # Now click the first link (the @jimisola span) for real, via a
    # synthetic Tk event at its on-screen position, to exercise the actual
    # bound handler rather than just the URL-building helpers it calls. The
    # `root` fixture starts withdrawn (no display geometry, so `bbox` can't
    # resolve), hence the temporary deiconify.
    widget.pack()
    root.deiconify()
    root.update()
    start = widget.tag_ranges(tags[0])[0]
    x, y, _w, h = widget.bbox(start)
    cx, cy = x + 1, y + h // 2
    # Tk only knows which tag the pointer is "current"-ly over from motion
    # tracking, so a bare synthetic Button-1 (with no real pointer movement
    # behind it) doesn't trigger a tag's binding -- a Motion event first
    # brings the "current" tag up to date, exactly as a real click would via
    # the mouse move that precedes it.
    widget.event_generate("<Motion>", x=cx, y=cy)
    widget.event_generate("<Button-1>", x=cx, y=cy)
    widget.event_generate("<ButtonRelease-1>", x=cx, y=cy)
    root.update()
    root.withdraw()
    assert opened == ["https://github.com/jimisola"]


def test_bot_username_links_to_the_apps_page(root) -> None:
    assert markdown_render._user_url("renovate[bot]") == "https://github.com/apps/renovate"


def test_open_url_refuses_non_http_schemes(root, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(markdown_render.webbrowser, "open", lambda url: opened.append(url))

    markdown_render._open_url("javascript:alert(1)")
    markdown_render._open_url("file:///etc/passwd")

    assert opened == []  # neither scheme was ever handed to the browser


def test_markdown_links_and_bare_urls_are_clickable(root) -> None:
    widget, fonts = _notes_widget(root)
    render_release_notes(
        widget,
        "See [the docs](https://example.com/docs) or https://example.com/bare directly.",
        fonts=fonts,
        repo="x/y",
    )
    text = widget.get("1.0", tk.END)
    assert "the docs" in text
    assert "https://example.com/docs" not in text  # the raw URL is hidden behind the label
    assert "https://example.com/bare" in text  # a bare URL has no label to hide behind
    assert len(widget._md_link_tags) == 2


def test_malformed_input_degrades_to_plain_text(root, monkeypatch) -> None:
    """Any parsing failure must fall back to showing the raw body, never raise."""
    monkeypatch.setattr(markdown_render, "_render_body", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))

    widget, fonts = _notes_widget(root)
    junk = "### heading\n- bullet **oops"
    render_release_notes(widget, junk, fonts=fonts, repo="x/y")

    assert widget.get("1.0", "end-1c") == junk
    assert widget._md_link_tags == []
    assert str(widget.cget("state")) == "disabled"


def test_empty_body_renders_without_error(root) -> None:
    widget, fonts = _notes_widget(root)
    render_release_notes(widget, "", fonts=fonts, repo="x/y")
    assert widget.get("1.0", tk.END).strip() == ""


def test_huge_body_is_truncated_rather_than_hanging(root) -> None:
    widget, fonts = _notes_widget(root)
    huge = ("- " + "x" * 200 + "\n") * 20_000
    render_release_notes(widget, huge, fonts=fonts, repo="x/y")
    assert "truncated" in widget.get("1.0", tk.END)


def test_notes_tags_follow_the_theme(root) -> None:
    """The same source rendered under two themes must pick up each palette's
    colors -- confirms tags are (re)configured from the live PALETTE on every
    render rather than baked in once."""
    apply_theme(root, theme="Dark")
    widget, fonts = _notes_widget(root)
    render_release_notes(widget, "### Heading\n- item by @user in #1", fonts=fonts, repo="x/y")
    dark_h3 = widget.tag_cget("h3", "foreground")
    dark_link = widget.tag_cget(widget._md_link_tags[0], "foreground")

    apply_theme(root, theme="Light")
    render_release_notes(widget, "### Heading\n- item by @user in #1", fonts=fonts, repo="x/y")
    light_h3 = widget.tag_cget("h3", "foreground")
    light_link = widget.tag_cget(widget._md_link_tags[0], "foreground")

    assert dark_h3 != light_h3
    assert dark_link != light_link
    apply_theme(root, theme="Dark")  # leave the shared root as other tests expect it
