"""Software update — review, download, restart.

States, in order:

    checking    → an explicit check is in flight (Help → "Check for updates…")
    available   → version summary + release notes, "Download & install"
    downloading → progress bar, cancellable
    ready       → "Restart now"

The dialog never writes to the app folder. Downloading only *stages* the update
under the data root; the launcher applies it on the next start (CLAUDE.md §7).
"Restart now" re-execs the launcher and closes the app, which is what makes a
staged update take effect. Opening with an update already staged jumps straight
to ``ready``, so a user who picked "Later" can come back and restart.

Updates live under **Help → "Check for updates…"**, not in Settings (JL), and
two things follow from that:

* ``check_on_open=True`` lets the menu action open the dialog and run the check
  *inside* it, so the user gets a window immediately instead of a status-bar
  message. Passing an ``info=`` the shell already has (from the silent startup
  check) skips the check entirely.
* A check that fails is shown here rather than in the status bar; the dialog is
  the only surface the menu action opens.

Threading follows CLAUDE.md §8: a worker only ever puts a message on a
``queue.Queue`` and a main-thread ``QTimer`` drains it into the widgets. Two
independent queue/timer pairs, because a release-list load and a download can
be in flight at once and one queue would force the consumer to disentangle them
by payload shape. The initial check rides the metadata pair (it can't overlap a
picker load: both go through ``_loading_meta``).

Release notes are Markdown, rendered by ``QTextBrowser.setMarkdown``.
``linkify_notes`` supplies the two things GitHub does that CommonMark doesn't —
``@user`` and ``#123`` — plus the size caps, and links open through a scheme
check rather than ``setOpenExternalLinks``: a release body is
attacker-influenced in principle (``CASESORTER_UPDATE_REPO`` points wherever it
points).
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..update import updater
from ..update.updater import PendingUpdate, UpdateError, UpdateInfo
from . import formatting

DRAIN_MS = 50

TITLE_CHECKING = "Checking for updates…"
TITLE_CHECK_FAILED = "Couldn't check for updates"
TITLE_AVAILABLE = "Update available"
TITLE_REINSTALL = "Reinstall this version"
TITLE_UP_TO_DATE = "You're up to date"
TITLE_PENDING = "Update ready to install"

PICKER_LABEL = "Choose a different version…"
PICKER_LOADING = "Loading releases…"
PRERELEASE_LABEL = "Show prereleases"
AUTO_CHECK_LABEL = "Check for updates on startup"

PRIMARY_DOWNLOAD = "Download & install"
PRIMARY_DOWNLOADING = "Downloading…"
PRIMARY_RESTART = "Restart now"
PRIMARY_RETRY = "Try again"
SECONDARY_LATER = "Later"
SECONDARY_CLOSE = "Close"
SECONDARY_CANCEL = "Cancel"

UP_TO_DATE_DETAIL = "No newer release is available. Choose a different version below to install one anyway."
PENDING_DETAIL = (
    "The update has been downloaded. It installs the next time the app starts — "
    "restart now, or keep working and it will be applied on your next launch."
)
PENDING_NOTES = (
    "Nothing is changed until you restart.\n\n"
    "Your models, training images, and settings are stored separately from the "
    "app and are not affected by an update."
)
NO_NOTES = "No release notes were provided."
NO_RELEASES = "No published releases were found."
NO_LAUNCHER_DETAIL = "Close the app and start it again to finish installing the update."

# Tagged unions for the worker->main-thread queues: the "kind" string picks
# which payload shape goes with it, so a drain can narrow by literal.
_DownloadEvent = (
    tuple[Literal["progress"], tuple[int, int | None]]
    | tuple[Literal["done"], PendingUpdate]
    | tuple[Literal["error"], str]
)
_MetaEvent = (
    tuple[Literal["releases"], list[UpdateInfo]]
    | tuple[Literal["checked"], UpdateInfo | None]
    | tuple[Literal["error"], str]
)

# A real release body is a few KB; an enormous one must degrade to "truncated"
# rather than wedge the UI laying out megabytes of text.
MAX_SOURCE_CHARS = 100_000
MAX_LINES = 4_000
TRUNCATED_NOTE = "\n\n*[Release notes truncated]*"

# Inline tokens, first match wins. Links and code spans are matched only so the
# rewrites below skip over them; every branch is linear in the input.
_INLINE_RE = re.compile(
    r"(?P<link>\[[^\]\n]*\]\([^\s()]*\))"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<user>(?<![\w/])@(?P<user_name>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot])?))"
    r"|(?P<pr>(?<![\w#])#(?P<pr_num>\d+))"
)


def _user_url(name: str) -> str:
    # Bot accounts (`renovate[bot]`) have no normal profile — theirs is /apps/<name>.
    if name.endswith("[bot]"):
        return f"https://github.com/apps/{quote(name[: -len('[bot]')])}"
    return f"https://github.com/{quote(name)}"


def _pr_url(repo: str, number: str) -> str:
    # GitHub redirects /issues/<n> to /pull/<n> when it is one, so #N needn't be resolved.
    return f"https://github.com/{repo}/issues/{number}"


def linkify_notes(source: str, repo: str) -> str:
    """Markdown in, Markdown out: ``@user`` and ``#123`` become links, size capped.

    Everything else is left exactly as written — ``setMarkdown`` handles the
    headings, bullets, bold and code spans git-cliff emits. Never raises; the
    caller shows the source verbatim if it somehow does.
    """
    truncated = False
    if len(source) > MAX_SOURCE_CHARS:
        source = source[:MAX_SOURCE_CHARS]
        truncated = True
    lines = source.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        truncated = True
    source = "\n".join(lines)

    def _replace(match: re.Match[str]) -> str:
        if match.lastgroup is None:
            return match.group(0)
        name = match.group("user_name")
        if name is not None:
            # Escape the brackets a bot suffix carries, or the label ends the link early.
            label = f"@{name}".replace("[", r"\[").replace("]", r"\]")
            return f"[{label}]({_user_url(name)})"
        number = match.group("pr_num")
        if number is not None:
            return f"[#{number}]({_pr_url(repo, number)})"
        return match.group(0)  # a link or code span: left alone

    out = _INLINE_RE.sub(_replace, source)
    return out + TRUNCATED_NOTE if truncated else out


def spawn_launcher(launcher: Path) -> None:
    """Start the launcher script detached, so it survives this process exiting."""
    root = launcher.parent
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(launcher)], cwd=str(root), close_fds=True, creationflags=flags, shell=True)
    else:
        subprocess.Popen(["/bin/bash", str(launcher)], cwd=str(root), start_new_session=True, close_fds=True)


class UpdateDialog(QDialog):
    """Non-blocking modal: opened with ``open()``, never ``exec()``.

    ``check_now`` / ``list_releases`` / ``stage_update`` / ``launcher_path`` /
    ``spawn_launcher`` are instance attributes on purpose — they are the test
    seams, and replacing ``stage_update`` with the real one bound to a fake
    ``requests`` session exercises the whole staging path offline.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        info: UpdateInfo | None = None,
        app: Any = None,
        pending: PendingUpdate | None = None,
        check_on_open: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Software Update")
        self.setMinimumSize(520, 440)
        self.resize(640, 580)

        self._info = info
        # What "Download & install" actually targets. Defaults to whatever check
        # the caller already made; picking in the picker replaces it, including
        # with something older — that is the point of letting the user choose.
        self._selected = info
        self._app = app
        self._pending = pending if pending is not None else updater.pending_update()
        self._cancelled = False
        self._downloading = False
        self._loading_meta = False
        self._checking = False
        self._check_error = ""
        self._releases: list[UpdateInfo] = []

        self.check_now: Callable[[], UpdateInfo | None] = updater.check_for_update
        self.list_releases: Callable[..., list[UpdateInfo]] = updater.list_releases
        self.stage_update: Callable[..., PendingUpdate] = updater.stage_update
        self.launcher_path: Callable[[], Path | None] = updater.launcher_path
        self.spawn_launcher: Callable[[Path], None] = spawn_launcher

        self._events: queue.Queue[_DownloadEvent] = queue.Queue()
        self._meta_events: queue.Queue[_MetaEvent] = queue.Queue()

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._meta_timer = QTimer(self)
        self._meta_timer.timeout.connect(self._drain_meta)

        self._render()
        if check_on_open and self._pending is None and self._info is None:
            self.start_check()

    # ----- UI build -----------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        self.title_label = QLabel(self)
        self.title_label.setObjectName("updateTitle")
        title_font = QFont(self.title_label.font())
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setWordWrap(True)
        column.addWidget(self.title_label)

        self.version_label = QLabel(self)
        self.version_label.setObjectName("updateVersion")
        column.addWidget(self.version_label)

        self.detail_label = QLabel(self)
        self.detail_label.setObjectName("mutedLabel")
        self.detail_label.setWordWrap(True)
        column.addWidget(self.detail_label)

        # Collapsed to one button until the user asks: nothing here fires a
        # request on its own.
        picker_row = QHBoxLayout()
        self.picker_button = QPushButton(PICKER_LABEL, self)
        self.picker_button.clicked.connect(self.open_picker)
        picker_row.addWidget(self.picker_button)
        picker_row.addStretch(1)
        column.addLayout(picker_row)

        self.combo_row = QWidget(self)
        combo_layout = QHBoxLayout(self.combo_row)
        combo_layout.setContentsMargins(0, 0, 0, 0)
        combo_layout.addWidget(QLabel("Version:", self.combo_row))
        self.version_combo = QComboBox(self.combo_row)
        self.version_combo.setMinimumWidth(260)
        self.version_combo.currentIndexChanged.connect(self._on_pick_version)
        combo_layout.addWidget(self.version_combo)
        self.prerelease_check = QCheckBox(PRERELEASE_LABEL, self.combo_row)
        self.prerelease_check.toggled.connect(self._on_toggle_prereleases)
        combo_layout.addWidget(self.prerelease_check)
        combo_layout.addStretch(1)
        self.combo_row.setVisible(False)
        column.addWidget(self.combo_row)

        self.notes = QTextBrowser(self)
        self.notes.setObjectName("releaseNotes")
        # Links are opened by hand so the scheme can be checked first.
        self.notes.setOpenLinks(False)
        self.notes.setOpenExternalLinks(False)
        self.notes.anchorClicked.connect(self._open_link)
        column.addWidget(self.notes, 1)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        column.addWidget(self.progress)
        self.progress_label = QLabel(self)
        self.progress_label.setObjectName("mutedLabel")
        self.progress_label.setVisible(False)
        column.addWidget(self.progress_label)

        button_row = QHBoxLayout()
        self.auto_check = QCheckBox(AUTO_CHECK_LABEL, self)
        self.auto_check.setChecked(self._read_auto_check())
        self.auto_check.toggled.connect(self._on_toggle_auto)
        button_row.addWidget(self.auto_check)
        button_row.addStretch(1)
        # Blue "update" hue per theme.py's colour rules: refreshing something
        # already installed.
        self.primary_button = QPushButton(PRIMARY_DOWNLOAD, self)
        self.primary_button.setObjectName("update")
        self.primary_button.setDefault(True)
        self.primary_button.clicked.connect(self._on_primary)
        button_row.addWidget(self.primary_button)
        self.secondary_button = QPushButton(SECONDARY_LATER, self)
        self.secondary_button.clicked.connect(self._on_secondary)
        button_row.addWidget(self.secondary_button)
        column.addLayout(button_row)

    # ----- settings -----------------------------------------------------------

    def _settings(self) -> Any:
        db = getattr(self._app, "db", None)
        if db is None:
            return None
        from ..data.repository import SettingsRepo

        return SettingsRepo(db)

    def _read_auto_check(self) -> bool:
        repo = self._settings()
        if repo is None:
            return True
        try:
            return bool(repo.get(updater.SETTING_CHECK_ON_STARTUP, True))
        except Exception:
            return True

    def _on_toggle_auto(self, checked: bool) -> None:
        repo = self._settings()
        if repo is None:
            return
        try:
            repo.set(updater.SETTING_CHECK_ON_STARTUP, bool(checked))
        except Exception:
            pass

    # ----- explicit check -------------------------------------------------------

    def start_check(self) -> None:
        """Run the update check from inside the dialog (Help → Check for updates…)."""
        if self._loading_meta or self._pending is not None:
            return
        self._loading_meta = True
        self._checking = True
        self._check_error = ""
        self.picker_button.setEnabled(False)
        self._render()

        def _work() -> None:
            try:
                found = self.check_now()
            except UpdateError as exc:
                self._meta_events.put(("error", str(exc)))
            except Exception as exc:
                self._meta_events.put(("error", f"Unexpected error: {exc}"))
            else:
                self._meta_events.put(("checked", found))

        threading.Thread(target=_work, daemon=True).start()
        self._meta_timer.start(DRAIN_MS)

    def _check_done(self, found: UpdateInfo | None) -> None:
        self._loading_meta = False
        self._checking = False
        self.picker_button.setEnabled(True)
        self._info = found
        if found is not None:
            self._selected = found
        # Let the shell's status-bar affordance learn what this check found.
        note = getattr(self._app, "note_update_info", None)
        if callable(note):
            note(found)
        self._render()

    def _check_failed(self, message: str) -> None:
        self._loading_meta = False
        self._checking = False
        self._check_error = message
        self.picker_button.setEnabled(True)
        self._render()

    # ----- version picker -------------------------------------------------------

    def release_label(self, info: UpdateInfo) -> str:
        # OS-regional format, not the raw ISO date GitHub returns.
        date = formatting.format_date(info.published_at) if info.published_at else ""
        suffix = " (current)" if info.version == updater.current_version() else ""
        return f"{info.tag}  {date}{suffix}".strip()

    def open_picker(self) -> None:
        if self._loading_meta or self._pending is not None:
            return
        self._loading_meta = True
        self.picker_button.setEnabled(False)
        self.picker_button.setText(PICKER_LOADING)
        include_prereleases = self.prerelease_check.isChecked()

        def _work() -> None:
            try:
                releases = self.list_releases(include_prereleases=include_prereleases)
            except UpdateError as exc:
                self._meta_events.put(("error", str(exc)))
            except Exception as exc:
                self._meta_events.put(("error", f"Unexpected error: {exc}"))
            else:
                self._meta_events.put(("releases", releases))

        threading.Thread(target=_work, daemon=True).start()
        self._meta_timer.start(DRAIN_MS)

    def _drain_meta(self) -> None:
        """Main thread: the only place the check/picker workers reach a widget."""
        while True:
            try:
                event = self._meta_events.get_nowait()
            except queue.Empty:
                return
            match event:
                case ("releases", releases):
                    self._meta_timer.stop()
                    self._show_picker(releases)
                    return
                case ("checked", found):
                    self._meta_timer.stop()
                    self._check_done(found)
                    return
                case ("error", message):
                    self._meta_timer.stop()
                    if self._checking:
                        self._check_failed(str(message))
                    else:
                        self._picker_load_failed(str(message))
                    return

    def _picker_load_failed(self, message: str) -> None:
        self._loading_meta = False
        self.picker_button.setEnabled(True)
        self.picker_button.setText(PICKER_LABEL)
        self.detail_label.setText(f"Could not load releases: {message}")

    def _show_picker(self, releases: list[UpdateInfo]) -> None:
        self._loading_meta = False
        self._releases = releases
        self.picker_button.setEnabled(True)
        self.picker_button.setText(PICKER_LABEL)
        if not releases:
            self.detail_label.setText(NO_RELEASES)
            return

        # Repopulating fires currentIndexChanged for the intermediate states;
        # only the final index is a user pick.
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItems([self.release_label(r) for r in releases])

        # Keep the current selection if it survived a re-fetch (e.g. the
        # prerelease toggle); otherwise default to the newest one shown.
        idx = 0
        if self._selected is not None:
            for i, r in enumerate(releases):
                if r.tag == self._selected.tag:
                    idx = i
                    break
        self.version_combo.setCurrentIndex(idx)
        self.version_combo.blockSignals(False)
        self.combo_row.setVisible(True)
        self._on_pick_version()

    def _on_pick_version(self) -> None:
        idx = self.version_combo.currentIndex()
        if idx < 0 or idx >= len(self._releases):
            return
        self._selected = self._releases[idx]
        self._check_error = ""
        self._apply_selected()

    def _on_toggle_prereleases(self) -> None:
        # Re-fetch rather than filter client-side: the current selection may
        # itself be a prerelease that a stable-only list never carried, so a
        # cached list can't answer the toggle on its own.
        self.open_picker()

    def _apply_selected(self) -> None:
        info = self._selected
        if info is None:
            return
        current = updater.current_version()
        newer = updater.is_newer(info.version, current)
        self.title_label.setText(TITLE_AVAILABLE if newer else TITLE_REINSTALL)
        self.version_label.setText(f"{current}  →  {info.version}")

        newest = self._releases[0] if self._releases else self._info
        not_newest = ""
        if newest is not None and newest.tag != info.tag:
            not_newest = f" This is not the newest available release ({newest.tag} is newer)."
        self.detail_label.setText(self._available_detail(info) + not_newest)
        self._set_notes(info.notes or NO_NOTES)
        self._hide_progress()
        self.primary_button.setVisible(True)
        self.primary_button.setText(PRIMARY_DOWNLOAD)
        self.primary_button.setEnabled(True)
        self.secondary_button.setText(SECONDARY_LATER)

    # ----- rendering ----------------------------------------------------------

    @staticmethod
    def _available_detail(info: UpdateInfo) -> str:
        size = f" ({info.size / 1_000_000:.1f} MB)" if info.size else ""
        return (
            f"Release {info.tag}{size} is available. It downloads in the "
            "background and installs the next time you start the app."
        )

    def _set_notes(self, text: str) -> None:
        try:
            self.notes.setMarkdown(linkify_notes(text, updater.update_repo()))
        except Exception:
            self.notes.setPlainText(text)

    def _open_link(self, url: QUrl) -> None:
        """Open a release-note link — http(s) only; the body is untrusted input."""
        if url.scheme().lower() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def _hide_progress(self) -> None:
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)

    def _render(self) -> None:
        current = updater.current_version()
        if self._pending is not None:
            self.title_label.setText(TITLE_PENDING)
            self.version_label.setText(f"{current}  →  {self._pending.version}")
            self.detail_label.setText(PENDING_DETAIL)
            self._set_notes(PENDING_NOTES)
            self.picker_button.setVisible(False)
            self.combo_row.setVisible(False)
            self._hide_progress()
            self.primary_button.setVisible(True)
            self.primary_button.setText(PRIMARY_RESTART)
            self.primary_button.setEnabled(True)
            self.secondary_button.setText(SECONDARY_LATER)
            return

        if self._checking:
            self.title_label.setText(TITLE_CHECKING)
            self.version_label.setText(f"Version {current}")
            self.detail_label.setText("")
            self._set_notes("")
            self._hide_progress()
            self.primary_button.setVisible(False)
            self.secondary_button.setText(SECONDARY_CLOSE)
            return

        if self._check_error:
            self.title_label.setText(TITLE_CHECK_FAILED)
            self.version_label.setText(f"Version {current}")
            self.detail_label.setText(self._check_error)
            self._set_notes("")
            self._hide_progress()
            self.primary_button.setVisible(False)
            self.secondary_button.setText(SECONDARY_CLOSE)
            return

        info = self._selected or self._info
        if info is None:
            self.title_label.setText(TITLE_UP_TO_DATE)
            self.version_label.setText(f"Version {current}")
            self.detail_label.setText(UP_TO_DATE_DETAIL)
            self._set_notes("")
            self._hide_progress()
            # No download target, but the picker below still reaches every
            # published release — this is the "install one anyway" route.
            self.primary_button.setVisible(False)
            self.secondary_button.setText(SECONDARY_CLOSE)
            return

        # One renderer for both "what the check found" and "what the picker
        # chose": with no picker interaction the two are the same release, and
        # the "not the newest" line then resolves to nothing.
        self._selected = info
        self._apply_selected()

    # ----- actions ------------------------------------------------------------

    def _on_primary(self) -> None:
        if self._pending is not None:
            self._restart()
        else:
            self.start_download()

    def _on_secondary(self) -> None:
        if self._downloading:
            self.cancel_download()
            return
        self.close()

    def start_download(self) -> None:
        target = self._selected or self._info
        if self._downloading or target is None:
            return
        self._downloading = True
        self._cancelled = False
        self.primary_button.setEnabled(False)
        self.primary_button.setText(PRIMARY_DOWNLOADING)
        self.secondary_button.setText(SECONDARY_CANCEL)
        self.secondary_button.setEnabled(True)
        self._set_picker_enabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.progress_label.setText("Starting download…")
        self.progress_label.setVisible(True)

        info = target
        self._events = queue.Queue()

        def _work() -> None:
            try:
                pending = self.stage_update(info, progress=self._on_progress)
            except UpdateError as exc:
                self._events.put(("error", str(exc)))
            except Exception as exc:
                self._events.put(("error", f"Unexpected error: {exc}"))
            else:
                self._events.put(("done", pending))

        threading.Thread(target=_work, daemon=True).start()
        self._timer.start(DRAIN_MS)

    def _set_picker_enabled(self, enabled: bool) -> None:
        self.picker_button.setEnabled(enabled)
        self.version_combo.setEnabled(enabled)
        self.prerelease_check.setEnabled(enabled)

    def _drain(self) -> None:
        """Main thread: the only place the download worker reaches a widget."""
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            match event:
                case ("progress", (done, total)):
                    self._show_progress(done, total)
                case ("done", pending):
                    self._timer.stop()
                    self._download_done(pending)
                    return
                case ("error", message):
                    self._timer.stop()
                    self._download_failed(str(message))
                    return

    def _on_progress(self, done: int, total: int | None) -> None:
        """Called on the download thread — hand off, never touch a widget."""
        if self._cancelled:
            raise UpdateError("Download cancelled.")
        self._events.put(("progress", (done, total)))

    def _show_progress(self, done: int, total: int | None) -> None:
        mb = done / 1_000_000
        if total:
            self.progress.setValue(int(min(100.0, done * 100.0 / total)))
            self.progress_label.setText(f"{mb:.1f} MB of {total / 1_000_000:.1f} MB")
        else:
            self.progress.setValue(0)
            self.progress_label.setText(f"{mb:.1f} MB downloaded")

    def cancel_download(self) -> None:
        self._cancelled = True
        self.progress_label.setText("Cancelling…")
        self.secondary_button.setEnabled(False)

    def _download_failed(self, message: str) -> None:
        self._downloading = False
        self._set_picker_enabled(True)
        self.progress.setValue(0)
        self.secondary_button.setEnabled(True)
        if self._cancelled:
            self._hide_progress()
            self.progress_label.setText("")
            self.primary_button.setEnabled(True)
            self.primary_button.setText(PRIMARY_DOWNLOAD)
            self.secondary_button.setText(SECONDARY_LATER)
            return
        self.progress_label.setText(message)
        self.primary_button.setEnabled(True)
        self.primary_button.setText(PRIMARY_RETRY)
        self.secondary_button.setText(SECONDARY_CLOSE)

    def _download_done(self, pending: PendingUpdate) -> None:
        self._downloading = False
        self._pending = pending
        self.progress_label.setText("")
        self.secondary_button.setEnabled(True)
        self._render()
        note = getattr(self._app, "note_pending_update", None)
        if callable(note):
            note(pending)

    def _restart(self) -> None:
        """Re-exec the launcher, then shut the app down so the swap can happen."""
        launcher = self.launcher_path()
        if launcher is None:
            self.detail_label.setText(NO_LAUNCHER_DETAIL)
            self.primary_button.setEnabled(False)
            return
        try:
            self.spawn_launcher(launcher)
        except OSError as exc:
            self.detail_label.setText(
                f"Could not relaunch automatically ({exc}). Close the app and "
                "start it again to finish installing the update."
            )
            return

        self.close()
        close_app = getattr(self._app, "close", None)
        if callable(close_app):
            close_app()

    # ----- lifecycle ----------------------------------------------------------

    def _stop(self) -> None:
        # Stop the pollers before the widgets go away. The workers are daemons
        # and their queue writes are harmless once nothing drains them.
        self._cancelled = True
        self._downloading = False
        self._loading_meta = False
        self._timer.stop()
        self._meta_timer.stop()

    def closeEvent(self, event: Any) -> None:
        self._stop()
        super().closeEvent(event)

    def reject(self) -> None:
        """Esc and the window's close button both land here."""
        self._stop()
        super().reject()
