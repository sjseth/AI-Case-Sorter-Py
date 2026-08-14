"""The Qt update dialog, offscreen: check, version picker, staging, restart.

Nothing here touches the network. ``CASESORTER_DATA_DIR`` points at ``tmp_path``
(autouse, like the Models suite) because a staged update is a real directory
tree under the data root — an unset root would have these tests writing into the
developer's own ``updates/pending``.

Two levels, per PLAN's rule:

* Unit — canned ``UpdateInfo``s through the dialog's own seams
  (``check_now`` / ``list_releases`` / ``stage_update`` / ``spawn_launcher``),
  driven through the real queue + ``QTimer`` path by pumping the Qt event loop.
  Nothing calls a widget method from a worker.
* Integration — ``test_download_stages_a_real_archive`` leaves the dialog's
  ``stage_update`` seam alone and fakes ``requests.get`` instead (the seam
  ``tests/unit/update/test_updater.py`` already uses), against a real
  sdist-shaped ``.tar.gz`` built in ``tmp_path``. The download loop, tar
  validation, ``REQUIRED_ENTRY_SETS`` check, staging swap and ``pending.json``
  write are all production code.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest
import requests

pytest.importorskip("PySide6")

from sorter import paths
from sorter.ui import dialog_update
from sorter.ui.dialog_update import (
    NO_LAUNCHER_DETAIL,
    PICKER_LABEL,
    PRIMARY_DOWNLOAD,
    PRIMARY_RESTART,
    PRIMARY_RETRY,
    TITLE_AVAILABLE,
    TITLE_CHECK_FAILED,
    TITLE_CHECKING,
    TITLE_PENDING,
    TITLE_REINSTALL,
    TITLE_UP_TO_DATE,
    UpdateDialog,
    linkify_notes,
)
from sorter.update import updater
from sorter.update.updater import PendingUpdate, UpdateError, UpdateInfo

CURRENT = "1.5.0"


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Every path this dialog touches stays inside the test's tmp dir."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


@pytest.fixture(autouse=True)
def _pinned_version(monkeypatch) -> None:
    """Pin the running version: half the dialog's text is a comparison against it."""
    monkeypatch.setattr(updater, "current_version", lambda: CURRENT)
    monkeypatch.setattr(updater, "update_repo", lambda: "sjseth/AI-Case-Sorter-Py")


class _AppStub:
    """What the dialog reads off the shell — nothing more."""

    def __init__(self, db: Any = None) -> None:
        self.db = db
        self.pending: Any = None
        self.info_calls: list[Any] = []
        self.closed = False

    def note_pending_update(self, pending: Any) -> None:
        self.pending = pending

    def note_update_info(self, info: Any) -> None:
        self.info_calls.append(info)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def app_stub(config) -> _AppStub:
    return _AppStub(config.db)


@pytest.fixture
def make_dialog(qapp):
    dialogs: list[UpdateDialog] = []

    def _make(**kwargs: Any) -> UpdateDialog:
        dialog = UpdateDialog(None, **kwargs)
        dialogs.append(dialog)
        return dialog

    yield _make
    for dialog in dialogs:
        dialog.close()


def visible(dialog: UpdateDialog, widget: Any) -> bool:
    """Would this widget show if the dialog were on screen?

    ``isVisible()`` is False for every child of a window that was never shown,
    so it can't tell "hidden by the dialog" from "the dialog is offscreen".
    """
    return bool(widget.isVisibleTo(dialog))


def pump_until(qapp, predicate, timeout_s: float = 5.0) -> bool:
    """Run the Qt event loop until ``predicate`` holds — this is what fires the QTimers."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


def info(version: str = "2.0.0", **kwargs: Any) -> UpdateInfo:
    fields: dict[str, Any] = {
        "version": version,
        "tag": version,
        "url": f"https://example.invalid/ai_case_sorter-{version}.tar.gz",
        "notes": "### Features\n\n- something new by @someone in #42",
        "size": 4_000_000,
        "published_at": "2026-08-01T10:00:00Z",
    }
    fields.update(kwargs)
    return UpdateInfo(**fields)


# ----- states ------------------------------------------------------------------


def test_available_shows_notes_and_download(make_dialog):
    dialog = make_dialog(info=info())

    assert dialog.title_label.text() == TITLE_AVAILABLE
    assert dialog.version_label.text() == f"{CURRENT}  →  2.0.0"
    assert "4.0 MB" in dialog.detail_label.text()
    assert "something new" in dialog.notes.toPlainText()
    assert dialog.primary_button.text() == PRIMARY_DOWNLOAD
    assert not visible(dialog, dialog.progress)


def test_up_to_date_hides_download_but_keeps_the_picker(make_dialog):
    dialog = make_dialog(info=None)

    assert dialog.title_label.text() == TITLE_UP_TO_DATE
    assert visible(dialog, dialog.primary_button) is False
    # The picker is the only route to a release candidate or an older version,
    # so "up to date" must not be a dead end.
    assert visible(dialog, dialog.picker_button) is True
    assert dialog.picker_button.text() == PICKER_LABEL


def test_staged_update_opens_on_the_restart_prompt(make_dialog, tmp_path):
    pending = PendingUpdate(version="2.0.0", tag="2.0.0", path=tmp_path / "pending")
    dialog = make_dialog(info=None, pending=pending)

    assert dialog.title_label.text() == TITLE_PENDING
    assert dialog.primary_button.text() == PRIMARY_RESTART
    # Nothing else to choose: the download already happened.
    assert visible(dialog, dialog.picker_button) is False


# ----- explicit check ----------------------------------------------------------


def test_check_on_open_runs_a_check_and_reports_it(qapp, make_dialog, app_stub, monkeypatch):
    found = info("3.0.0")
    monkeypatch.setattr(updater, "check_for_update", lambda: found)

    dialog = make_dialog(info=None, app=app_stub, check_on_open=True)
    # The check is on a worker, so the dialog is on screen before it lands.
    assert dialog.title_label.text() == TITLE_CHECKING

    assert pump_until(qapp, lambda: dialog.title_label.text() == TITLE_AVAILABLE)
    assert dialog.version_label.text() == f"{CURRENT}  →  3.0.0"
    assert dialog.primary_button.text() == PRIMARY_DOWNLOAD
    # The shell learns what the check found, for its status-bar affordance.
    assert app_stub.info_calls == [found]


def test_check_on_open_with_nothing_newer(qapp, make_dialog, app_stub, monkeypatch):
    monkeypatch.setattr(updater, "check_for_update", lambda: None)

    dialog = make_dialog(info=None, app=app_stub, check_on_open=True)

    assert pump_until(qapp, lambda: dialog.title_label.text() == TITLE_UP_TO_DATE)
    assert visible(dialog, dialog.primary_button) is False
    assert app_stub.info_calls == [None]


def test_failed_check_is_shown_in_the_dialog(qapp, make_dialog, monkeypatch):
    def _boom() -> UpdateInfo | None:
        raise UpdateError("Could not reach the update server")

    monkeypatch.setattr(updater, "check_for_update", _boom)
    dialog = make_dialog(info=None, check_on_open=True)

    assert pump_until(qapp, lambda: dialog.title_label.text() == TITLE_CHECK_FAILED)
    assert "Could not reach the update server" in dialog.detail_label.text()
    assert visible(dialog, dialog.primary_button) is False
    # A failed check doesn't disable the picker: it's a different endpoint.
    assert dialog.picker_button.isEnabled()


def test_check_is_skipped_when_the_caller_already_has_a_result(make_dialog, monkeypatch):
    monkeypatch.setattr(updater, "check_for_update", lambda: pytest.fail("must not re-check"))
    dialog = make_dialog(info=info(), check_on_open=True)

    assert dialog.title_label.text() == TITLE_AVAILABLE


# ----- version picker ----------------------------------------------------------


def _picker_dialog(make_dialog, releases_by_flag: dict[bool, list[UpdateInfo]], calls: list[bool], **kwargs: Any):
    dialog = make_dialog(**kwargs)

    def _list(*, include_prereleases: bool = False) -> list[UpdateInfo]:
        calls.append(include_prereleases)
        return releases_by_flag[include_prereleases]

    dialog.list_releases = _list
    return dialog


def test_picker_populates_and_prereleases_refetch(qapp, make_dialog, monkeypatch):
    from PySide6.QtCore import QLocale

    from sorter.ui import formatting

    # The release date renders in the OS's regional format now; pin it so
    # the assertion doesn't depend on the machine running the suite.
    monkeypatch.setattr(formatting, "_locale", lambda: QLocale("sv_SE"))
    stable = [info("2.0.0"), info("1.5.0"), info("1.0.0")]
    with_pre = [info("2.1.0rc1"), *stable]
    calls: list[bool] = []
    dialog = _picker_dialog(make_dialog, {False: stable, True: with_pre}, calls, info=info("2.0.0"))

    dialog.picker_button.click()
    assert pump_until(qapp, lambda: visible(dialog, dialog.combo_row))

    assert calls == [False]
    assert dialog.version_combo.count() == 3
    assert dialog.version_combo.itemText(0) == "2.0.0  2026-08-01"
    # The running version is marked, so a reinstall is an obvious choice.
    assert dialog.version_combo.itemText(1).endswith("(current)")
    # Nothing was fetched before the button: the dialog opens on one check.
    assert dialog.version_combo.currentIndex() == 0

    dialog.prerelease_check.setChecked(True)
    assert pump_until(qapp, lambda: dialog.version_combo.count() == 4)
    assert calls == [False, True]
    assert dialog.version_combo.itemText(0).startswith("2.1.0rc1")


def test_picking_an_older_release_retargets_and_says_so(qapp, make_dialog):
    stable = [info("2.0.0"), info("1.0.0")]
    calls: list[bool] = []
    dialog = _picker_dialog(make_dialog, {False: stable, True: stable}, calls, info=info("2.0.0"))

    dialog.picker_button.click()
    assert pump_until(qapp, lambda: dialog.version_combo.count() == 2)

    dialog.version_combo.setCurrentIndex(1)

    assert dialog._selected is not None and dialog._selected.tag == "1.0.0"
    # Older than what's running, so it is a reinstall/downgrade, not an update.
    assert dialog.title_label.text() == TITLE_REINSTALL
    assert "not the newest available release" in dialog.detail_label.text()
    assert "2.0.0 is newer" in dialog.detail_label.text()
    assert dialog.primary_button.text() == PRIMARY_DOWNLOAD


def test_picker_reaches_releases_even_when_up_to_date(qapp, make_dialog):
    stable = [info("1.5.0"), info("1.0.0")]
    calls: list[bool] = []
    dialog = _picker_dialog(make_dialog, {False: stable, True: stable}, calls, info=None)
    assert dialog.title_label.text() == TITLE_UP_TO_DATE

    dialog.picker_button.click()
    assert pump_until(qapp, lambda: visible(dialog, dialog.combo_row))

    # Picking anything gives the "up to date" state a download target.
    dialog.version_combo.setCurrentIndex(1)
    assert dialog._selected is not None and dialog._selected.tag == "1.0.0"
    assert visible(dialog, dialog.primary_button) is True
    assert dialog.primary_button.text() == PRIMARY_DOWNLOAD


def test_picker_load_failure_leaves_the_button_usable(qapp, make_dialog):
    dialog = make_dialog(info=info())

    def _list(*, include_prereleases: bool = False) -> list[UpdateInfo]:
        raise UpdateError("HTTP 503")

    dialog.list_releases = _list
    dialog.picker_button.click()

    assert pump_until(qapp, lambda: "Could not load releases" in dialog.detail_label.text())
    assert "HTTP 503" in dialog.detail_label.text()
    assert dialog.picker_button.isEnabled()
    assert dialog.picker_button.text() == PICKER_LABEL


# ----- download / staging -------------------------------------------------------


def test_download_progress_reaches_the_widgets(qapp, make_dialog, app_stub):
    dialog = make_dialog(info=info(), app=app_stub)
    staged = PendingUpdate(version="2.0.0", tag="2.0.0", path=updater.pending_dir())

    def _stage(target: UpdateInfo, *, progress=None) -> PendingUpdate:
        # Worker thread: only the progress callback (a queue put) is touched.
        assert progress is not None
        progress(1_000_000, 4_000_000)
        progress(4_000_000, 4_000_000)
        return staged

    dialog.stage_update = _stage
    dialog.primary_button.click()

    assert pump_until(qapp, lambda: dialog.title_label.text() == TITLE_PENDING)
    assert dialog.progress.value() == 100
    assert dialog.primary_button.text() == PRIMARY_RESTART
    assert app_stub.pending is staged


def test_download_failure_offers_a_retry(qapp, make_dialog):
    dialog = make_dialog(info=info())

    def _stage(target: UpdateInfo, *, progress=None) -> PendingUpdate:
        raise UpdateError("Download failed: connection reset")

    dialog.stage_update = _stage
    dialog.primary_button.click()

    assert pump_until(qapp, lambda: dialog.primary_button.text() == PRIMARY_RETRY)
    assert "connection reset" in dialog.progress_label.text()
    assert dialog.primary_button.isEnabled()
    # The picker comes back too: a failed download may just be the wrong release.
    assert dialog.picker_button.isEnabled()


def test_cancelling_a_download_returns_to_the_offer(qapp, make_dialog):
    dialog = make_dialog(info=info())
    cancelled = []

    def _stage(target: UpdateInfo, *, progress=None) -> PendingUpdate:
        assert progress is not None
        for step in range(500):
            progress(step * 1000, 4_000_000)
            time.sleep(0.005)
        cancelled.append(False)
        raise AssertionError("cancel never reached the worker")

    dialog.stage_update = _stage
    dialog.primary_button.click()
    assert pump_until(qapp, lambda: dialog.progress_label.text().endswith("4.0 MB"))

    dialog.secondary_button.click()  # "Cancel" while downloading

    assert pump_until(qapp, lambda: dialog.primary_button.text() == PRIMARY_DOWNLOAD)
    assert cancelled == []
    assert not visible(dialog, dialog.progress)
    assert dialog.picker_button.isEnabled()


# ----- integration: the real staging path, offline ------------------------------


def _build_sdist(path: Path, *, version: str) -> bytes:
    """A minimal archive shaped like the real sdist: one top-level dir, src/ layout."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, body in (
            ("src/sorter/__init__.py", f'__version__ = "{version}"\n'),
            ("pyproject.toml", "[project]\nname = 'ai-case-sorter'\n"),
        ):
            data = body.encode()
            entry = tarfile.TarInfo(f"ai_case_sorter-{version}/{name}")
            entry.size = len(data)
            tf.addfile(entry, io.BytesIO(data))
    payload = buffer.getvalue()
    path.write_bytes(payload)
    return payload


class _FakeResponse:
    def __init__(self, url: str, payload: bytes) -> None:
        self.url = url
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 1):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]


def test_download_stages_a_real_archive(qapp, make_dialog, app_stub, tmp_path, monkeypatch):
    payload = _build_sdist(tmp_path / "sdist.tar.gz", version="9.9.9")
    # Same seam tests/unit/update/test_updater.py uses, and the only fake here:
    # the dialog's own default `stage_update` runs, unmodified.
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _FakeResponse(url, payload))

    dialog = make_dialog(info=info("9.9.9", size=len(payload)), app=app_stub)
    dialog.primary_button.click()

    assert pump_until(qapp, lambda: dialog.title_label.text() == TITLE_PENDING)

    staged = updater.pending_dir()
    assert (staged / "src" / "sorter" / "__init__.py").read_text() == '__version__ = "9.9.9"\n'
    # The sdist's `ai_case_sorter-9.9.9/` wrapper is stripped, not staged.
    assert not (staged / "ai_case_sorter-9.9.9").exists()

    # pending.json is a SIBLING of pending/, never inside it: everything under
    # pending/ is copied into the app folder verbatim (CLAUDE.md §6).
    meta = paths.updates_dir() / "pending.json"
    assert meta.is_file()
    assert not (staged / "pending.json").exists()
    assert json.loads(meta.read_text())["version"] == "9.9.9"

    # And the dialog is now the restart prompt, with the shell told about it.
    assert dialog.primary_button.text() == PRIMARY_RESTART
    assert app_stub.pending is not None and app_stub.pending.version == "9.9.9"
    # Real download bytes reached the progress widgets through the queue/poller.
    assert dialog.progress.value() == 100
    assert updater.pending_update() is not None


# ----- restart ------------------------------------------------------------------


def test_restart_spawns_the_launcher_and_closes_the_app(make_dialog, app_stub, tmp_path):
    launcher = tmp_path / "start.sh"
    launcher.write_text("#!/bin/sh\n")
    pending = PendingUpdate(version="2.0.0", tag="2.0.0", path=tmp_path / "pending")
    dialog = make_dialog(info=None, app=app_stub, pending=pending)

    spawned: list[Path] = []
    dialog.launcher_path = lambda: launcher
    dialog.spawn_launcher = spawned.append

    dialog.primary_button.click()

    assert spawned == [launcher]
    assert app_stub.closed is True


def test_restart_without_a_launcher_explains_instead(make_dialog, app_stub, tmp_path):
    pending = PendingUpdate(version="2.0.0", tag="2.0.0", path=tmp_path / "pending")
    dialog = make_dialog(info=None, app=app_stub, pending=pending)
    dialog.launcher_path = lambda: None
    dialog.spawn_launcher = lambda _p: pytest.fail("nothing to spawn")

    dialog.primary_button.click()

    assert dialog.detail_label.text() == NO_LAUNCHER_DETAIL
    assert not dialog.primary_button.isEnabled()
    assert app_stub.closed is False


# ----- startup-check setting ----------------------------------------------------


def test_check_on_startup_setting_round_trips(make_dialog, app_stub, config):
    from sorter.data.repository import SettingsRepo

    dialog = make_dialog(info=info(), app=app_stub)
    assert dialog.auto_check.isChecked() is True  # default when unset

    dialog.auto_check.setChecked(False)
    assert SettingsRepo(config.db).get(updater.SETTING_CHECK_ON_STARTUP, True) is False

    # A freshly opened dialog reads the stored value back.
    assert make_dialog(info=info(), app=app_stub).auto_check.isChecked() is False


def test_the_setting_is_optional(make_dialog):
    """No app (hence no db) must not break the checkbox — it just doesn't persist."""
    dialog = make_dialog(info=info())
    assert dialog.auto_check.isChecked() is True
    dialog.auto_check.setChecked(False)  # no exception


# ----- release notes ------------------------------------------------------------


def test_linkify_notes_adds_the_two_github_shorthands():
    out = linkify_notes("- fix by @someone in #42", "owner/repo")

    assert "[@someone](https://github.com/someone)" in out
    assert "[#42](https://github.com/owner/repo/issues/42)" in out


def test_linkify_notes_leaves_code_and_links_alone():
    source = "use `#42` and [#7](https://example.com/7)"
    assert linkify_notes(source, "owner/repo") == source


def test_linkify_notes_routes_bots_to_their_app_page():
    out = linkify_notes("by @renovate[bot]", "owner/repo")

    assert "https://github.com/apps/renovate" in out
    # The bot suffix's brackets are escaped, or they'd end the link label early.
    assert r"[@renovate\[bot\]]" in out


def test_linkify_notes_caps_an_enormous_body():
    out = linkify_notes("line\n" * (dialog_update.MAX_LINES + 500), "owner/repo")

    assert out.endswith(dialog_update.TRUNCATED_NOTE)
    assert out.count("line") <= dialog_update.MAX_LINES


def test_notes_links_open_only_over_http(make_dialog, monkeypatch):
    from PySide6.QtCore import QUrl

    opened: list[str] = []
    monkeypatch.setattr(dialog_update.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    dialog = make_dialog(info=info())

    dialog._open_link(QUrl("https://example.com/notes"))
    dialog._open_link(QUrl("file:///etc/passwd"))

    # A release body is attacker-influenced in principle (CASESORTER_UPDATE_REPO).
    assert opened == ["https://example.com/notes"]
