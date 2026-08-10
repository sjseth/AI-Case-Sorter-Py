"""Update dialog — review, download, restart to install.

Three states, in order:

    available   → version summary + release notes, "Download & Install"
    downloading → progress bar, cancellable
    ready       → "Restart to update"

The dialog never writes to the app folder. Downloading only *stages* the
update under the data root; the launcher applies it on the next start (see
``sorter/apply_update.py``). "Restart Now" re-execs the launcher script and
closes the app — which is what makes the staged update take effect.

Opening the dialog when an update is already staged jumps straight to
``ready``, so a user who picked "Later" can come back and restart.

Version picker
---------------
The dialog opens showing only the latest stable release — exactly what it
always showed — so a user who never touches the picker sees today's
behaviour unchanged. Nothing is fetched over the network beyond the single
check ``MainWindow`` already made before opening the dialog.

Clicking "Choose a different version…" is what triggers the extra request:
``updater.list_releases()`` runs on a worker thread and the picker (a
combobox of every published, non-prerelease release plus a "Show
prereleases" opt-in) appears once it returns. Picking an entry there changes
what "Download & Install" targets — including an older release than what's
currently offered — and the detail text says so explicitly whenever the
selection isn't the newest one available.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Literal

from .. import updater
from ..updater import PendingUpdate, UpdateError, UpdateInfo
from .markdown_render import render_release_notes
from .theme import PALETTE, get_fonts

# Tagged union for the worker->main-thread queue below: the "kind" string
# picks which payload shape goes with it, so _poll_events can narrow by
# literal instead of casting.
_DownloadEvent = (
    tuple[Literal["progress"], tuple[int, int | None]]
    | tuple[Literal["done"], PendingUpdate]
    | tuple[Literal["error"], str]
)

# Same shape, for the (separate) worker that lists releases for the picker.
# Kept as its own queue/type rather than folding into _DownloadEvent: the two
# workers can be in flight at once in principle (picker load while a stale
# download error is still displayed), and a shared queue would need the
# consumer to disentangle them by payload shape instead of by which queue it
# came from.
_ReleasesEvent = tuple[Literal["releases"], list[UpdateInfo]] | tuple[Literal["error"], str]


class UpdateDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        info: UpdateInfo | None,
        app=None,
        pending: PendingUpdate | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Software Update")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.geometry("620x560")
        self.minsize(520, 440)
        self.configure(bg=PALETTE["bg_surface"])
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._info = info
        # What "Download & Install" actually targets. Defaults to the plain
        # latest-stable check the caller already made; picking a release in
        # the picker replaces it, including with something older than
        # `_info` — that's the whole point of letting the user choose.
        self._selected = info
        self._app = app
        self._pending = pending if pending is not None else updater.pending_update()
        self._cancelled = False
        self._downloading = False
        self._events: queue.Queue[_DownloadEvent] = queue.Queue()
        self._poll_id: str | None = None

        self._releases: list[UpdateInfo] = []
        self._loading_releases = False
        self._releases_events: queue.Queue[_ReleasesEvent] = queue.Queue()
        self._releases_poll_id: str | None = None

        # Buttons first with side=BOTTOM so the resizable notes area above
        # can't squeeze them off-screen (same ordering trick as the PyTorch
        # install dialog).
        self._btns = ttk.Frame(self, padding=(12, 0, 12, 12))
        self._btns.pack(side=tk.BOTTOM, fill=tk.X)

        wrap = ttk.Frame(self, padding=12)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._build_header(wrap)
        self._build_picker(wrap)
        self._build_notes(wrap)
        self._build_progress(wrap)
        self._build_buttons()
        self._render()

    # ----- UI build -----------------------------------------------------------

    def _build_header(self, parent: tk.Misc) -> None:
        self._title_var = tk.StringVar()
        ttk.Label(parent, textvariable=self._title_var, style="Header.TLabel").pack(anchor="w")
        self._version_var = tk.StringVar()
        ttk.Label(parent, textvariable=self._version_var, style="Accent.TLabel").pack(anchor="w", pady=(4, 0))
        self._detail_var = tk.StringVar()
        ttk.Label(parent, textvariable=self._detail_var, style="Muted.TLabel", wraplength=560, justify=tk.LEFT).pack(
            anchor="w",
            pady=(6, 8),
            fill=tk.X,
        )

    def _build_picker(self, parent: tk.Misc) -> None:
        # Collapsed to a single link-like button until the user asks for it —
        # nothing here fires a request on its own.
        self._picker_link_row = ttk.Frame(parent)
        self._picker_link_row.pack(anchor="w", pady=(0, 8))
        self._picker_button = ttk.Button(
            self._picker_link_row, text="Choose a different version…", command=self._open_picker
        )
        self._picker_button.pack(side=tk.LEFT)

        self._combo_row = ttk.Frame(parent)
        # Not packed yet — _show_picker() reveals it once releases load.
        ttk.Label(self._combo_row, text="Version:").pack(side=tk.LEFT)
        self._version_combo_var = tk.StringVar()
        self._version_combo = ttk.Combobox(
            self._combo_row, textvariable=self._version_combo_var, state="readonly", width=42
        )
        self._version_combo.pack(side=tk.LEFT, padx=(6, 12))
        self._version_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_pick_version())
        self._show_prereleases_var = tk.BooleanVar(value=False)
        self._prerelease_check = ttk.Checkbutton(
            self._combo_row,
            text="Show prereleases",
            variable=self._show_prereleases_var,
            command=self._on_toggle_prereleases,
        )
        self._prerelease_check.pack(side=tk.LEFT)

    def _build_notes(self, parent: tk.Misc) -> None:
        self._notes_wrap = ttk.Frame(parent, style="Card.TFrame", padding=4)
        self._notes_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._notes = tk.Text(
            self._notes_wrap,
            height=10,
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text"],
            insertbackground=PALETTE["accent"],
            highlightthickness=0,
            borderwidth=0,
            padx=8,
            pady=6,
        )
        self._notes.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Scrollbar(self._notes_wrap, orient=tk.VERTICAL, command=self._notes.yview).pack(side=tk.RIGHT, fill=tk.Y)
        # Font choice doesn't change with the theme (it's a host-availability
        # pick, not a palette value), so this is computed once per dialog and
        # reused by every `_set_notes` call rather than re-probed each time.
        self._fonts = get_fonts(self)

    def _build_progress(self, parent: tk.Misc) -> None:
        self._progress_row = ttk.Frame(parent)
        self._progress = ttk.Progressbar(self._progress_row, mode="determinate", maximum=100)
        self._progress.pack(side=tk.TOP, fill=tk.X)
        self._progress_var = tk.StringVar(value="")
        ttk.Label(self._progress_row, textvariable=self._progress_var, style="Muted.TLabel").pack(
            anchor="w", pady=(4, 0)
        )

    def _build_buttons(self) -> None:
        self._secondary = ttk.Button(self._btns, text="Later", command=self._close)
        self._secondary.pack(side=tk.RIGHT)
        # Blue "update" hue: refreshing something already installed.
        self._primary = ttk.Button(
            self._btns, text="Download & Install", style="Update.TButton", command=self._on_primary
        )
        self._primary.pack(side=tk.RIGHT, padx=(0, 8))

        self._auto_var = tk.BooleanVar(value=self._read_auto_check())
        self._auto_check = ttk.Checkbutton(
            self._btns,
            text="Check for updates on startup",
            variable=self._auto_var,
            command=self._on_toggle_auto,
        )
        self._auto_check.pack(side=tk.LEFT)

    # ----- settings -----------------------------------------------------------

    def _settings(self):
        db = getattr(self._app, "db", None)
        if db is None:
            return None
        from ..repository import SettingsRepo

        return SettingsRepo(db)

    def _read_auto_check(self) -> bool:
        repo = self._settings()
        if repo is None:
            return True
        return bool(repo.get(updater.SETTING_CHECK_ON_STARTUP, True))

    def _on_toggle_auto(self) -> None:
        repo = self._settings()
        if repo is None:
            return
        try:
            repo.set(updater.SETTING_CHECK_ON_STARTUP, bool(self._auto_var.get()))
        except Exception:
            pass

    # ----- version picker -------------------------------------------------------

    def _release_label(self, info: UpdateInfo) -> str:
        date = info.published_at.split("T", 1)[0] if info.published_at else ""
        suffix = " (current)" if info.version == updater.current_version() else ""
        return f"{info.tag}  {date}{suffix}".strip()

    def _open_picker(self) -> None:
        if self._loading_releases or self._pending is not None:
            return
        self._loading_releases = True
        self._picker_button.config(state=tk.DISABLED, text="Loading releases…")
        self._releases_events = queue.Queue()
        include_prereleases = self._show_prereleases_var.get()

        def _work() -> None:
            try:
                releases = updater.list_releases(include_prereleases=include_prereleases)
            except UpdateError as exc:
                self._releases_events.put(("error", str(exc)))
            except Exception as exc:
                self._releases_events.put(("error", f"Unexpected error: {exc}"))
            else:
                self._releases_events.put(("releases", releases))

        threading.Thread(target=_work, daemon=True).start()
        self._poll_releases()

    def _poll_releases(self) -> None:
        self._releases_poll_id = None
        terminal = False
        try:
            while True:
                match self._releases_events.get_nowait():
                    case ("releases", releases):
                        self._show_picker(releases)
                        terminal = True
                        break
                    case ("error", message):
                        self._picker_load_failed(str(message))
                        terminal = True
                        break
        except queue.Empty:
            pass
        except tk.TclError:
            return  # dialog went away mid-drain

        if not terminal:
            try:
                self._releases_poll_id = self.after(50, self._poll_releases)
            except tk.TclError:
                pass

    def _picker_load_failed(self, message: str) -> None:
        self._loading_releases = False
        self._picker_button.config(state=tk.NORMAL, text="Choose a different version…")
        self._detail_var.set(f"Could not load releases: {message}")

    def _show_picker(self, releases: list[UpdateInfo]) -> None:
        self._loading_releases = False
        self._releases = releases
        self._picker_button.config(state=tk.NORMAL, text="Choose a different version…")
        if not releases:
            self._detail_var.set("No published releases were found.")
            return

        self._version_combo["values"] = [self._release_label(r) for r in releases]

        # Keep the current selection if it's still in the (possibly
        # re-fetched, e.g. after toggling prereleases) list; otherwise default
        # to the newest one shown.
        idx = 0
        if self._selected is not None:
            for i, r in enumerate(releases):
                if r.tag == self._selected.tag:
                    idx = i
                    break
        self._version_combo.current(idx)
        self._combo_row.pack(anchor="w", pady=(0, 8), after=self._picker_link_row)
        self._on_pick_version()

    def _on_pick_version(self) -> None:
        idx = self._version_combo.current()
        if idx < 0 or idx >= len(self._releases):
            return
        self._selected = self._releases[idx]
        self._apply_selected()

    def _on_toggle_prereleases(self) -> None:
        # Re-fetch rather than filter client-side: the "current" combobox
        # selection may itself be a prerelease that was never in a
        # stable-only list, so a stale cached list can't answer this toggle
        # on its own.
        self._open_picker()

    def _apply_selected(self) -> None:
        info = self._selected
        if info is None:
            return
        current = updater.current_version()
        newer = updater.is_newer(info.version, current)
        self._title_var.set("Update available" if newer else "Reinstall this version")
        self._version_var.set(f"{current}  →  {info.version}")
        size = f" ({info.size / 1_000_000:.1f} MB)" if info.size else ""

        newest = self._releases[0] if self._releases else self._info
        not_newest = ""
        if newest is not None and newest.tag != info.tag:
            not_newest = f" This is not the newest available release ({newest.tag} is newer)."

        self._detail_var.set(
            f"Release {info.tag}{size} is available. It downloads in the "
            f"background and installs the next time you start the app.{not_newest}"
        )
        self._set_notes(info.notes or "No release notes were provided.")
        self._progress_row.pack_forget()
        self._primary.pack(side=tk.RIGHT, padx=(0, 8))
        self._primary.config(text="Download & Install", state=tk.NORMAL)
        self._secondary.config(text="Later", command=self._close)

    # ----- rendering ----------------------------------------------------------

    def _set_notes(self, text: str) -> None:
        # Release bodies are Markdown (git-cliff generates them from
        # cliff.toml); render the subset this project's own notes actually
        # use instead of dumping the raw `### heading` / `- bullet` syntax at
        # the user. See markdown_render.py's module docstring for exactly
        # which shapes were pinned against real release bodies.
        render_release_notes(self._notes, text, fonts=self._fonts, repo=updater.update_repo())

    def _render(self) -> None:
        current = updater.current_version()
        if self._pending is not None:
            self._title_var.set("Update ready to install")
            self._version_var.set(f"{current}  →  {self._pending.version}")
            self._detail_var.set(
                "The update has been downloaded. It installs the next time the "
                "app starts — restart now, or keep working and it will be "
                "applied on your next launch."
            )
            self._set_notes(
                "Nothing is changed until you restart.\n\n"
                "Your models, training images, and settings are stored "
                "separately from the app and are not affected by an update."
            )
            self._picker_link_row.pack_forget()
            self._combo_row.pack_forget()
            self._progress_row.pack_forget()
            self._primary.config(text="Restart Now", state=tk.NORMAL)
            self._secondary.config(text="Later", command=self._close)
            return

        info = self._info
        if info is None:
            self._title_var.set("You're up to date")
            self._version_var.set(f"Version {current}")
            self._detail_var.set(
                "No newer release is available. Choose a different version below to install one anyway."
            )
            self._set_notes("")
            self._progress_row.pack_forget()
            self._primary.pack_forget()
            self._secondary.config(text="Close", command=self._close)
            return

        self._title_var.set("Update available")
        self._version_var.set(f"{current}  →  {info.version}")
        size = f" ({info.size / 1_000_000:.1f} MB)" if info.size else ""
        self._detail_var.set(
            f"Release {info.tag}{size} is available. It downloads in the "
            "background and installs the next time you start the app."
        )
        self._set_notes(info.notes or "No release notes were provided.")
        self._progress_row.pack_forget()
        self._primary.config(text="Download & Install", state=tk.NORMAL)
        self._secondary.config(text="Later", command=self._close)

    # ----- actions ------------------------------------------------------------

    def _on_primary(self) -> None:
        if self._pending is not None:
            self._restart()
        else:
            self._start_download()

    def _start_download(self) -> None:
        target = self._selected or self._info
        if self._downloading or target is None:
            return
        self._downloading = True
        self._cancelled = False
        self._primary.config(state=tk.DISABLED, text="Downloading…")
        self._secondary.config(text="Cancel", command=self._cancel_download)
        self._set_picker_enabled(False)
        self._progress_row.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        self._progress.config(value=0)
        self._progress_var.set("Starting download…")

        info = target

        # Tkinter is not thread-safe and `after()` is not an exception to
        # that — calling it from a worker thread registers a Tcl command off
        # the main thread. So the worker only ever touches this queue, and a
        # main-thread poller below turns its messages into widget updates.
        # Same shape as the app-wide EventBus, scoped to one dialog.
        self._events: queue.Queue[_DownloadEvent] = queue.Queue()

        def _work() -> None:
            try:
                pending = updater.stage_update(info, progress=self._on_progress)
            except UpdateError as exc:
                self._events.put(("error", str(exc)))
            except Exception as exc:
                self._events.put(("error", f"Unexpected error: {exc}"))
            else:
                self._events.put(("done", pending))

        threading.Thread(target=_work, daemon=True).start()
        self._poll_events()

    def _set_picker_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        try:
            self._picker_button.config(state=state)
        except tk.TclError:
            pass
        try:
            if self._combo_row.winfo_manager():
                self._version_combo.config(state="readonly" if enabled else tk.DISABLED)
                self._prerelease_check.config(state=state)
        except tk.TclError:
            pass

    def _poll_events(self) -> None:
        """Drain worker messages on the main thread. Rescheduled while active."""
        self._poll_id = None
        terminal = False
        try:
            while True:
                # match/case (rather than an if/elif on `kind` with a
                # separately-unpacked `payload`) is what lets the checker
                # narrow the payload's type per branch of this tagged union.
                match self._events.get_nowait():
                    case ("progress", (done, total)):
                        self._show_progress(done, total)
                    case ("done", pending):
                        self._download_done(pending)
                        terminal = True
                        break
                    case ("error", message):
                        self._download_failed(str(message))
                        terminal = True
                        break
        except queue.Empty:
            pass
        except tk.TclError:
            return  # dialog went away mid-drain

        if not terminal and self._downloading:
            try:
                self._poll_id = self.after(50, self._poll_events)
            except tk.TclError:
                pass

    def _on_progress(self, done: int, total: int | None) -> None:
        """Called on the download thread — hand off, never touch a widget."""
        if self._cancelled:
            raise UpdateError("Download cancelled.")
        self._events.put(("progress", (done, total)))

    def _show_progress(self, done: int, total: int | None) -> None:
        mb = done / 1_000_000
        if total:
            pct = min(100.0, done * 100.0 / total)
            self._progress.config(mode="determinate", value=pct)
            self._progress_var.set(f"{mb:.1f} MB of {total / 1_000_000:.1f} MB")
        else:
            self._progress.config(mode="determinate", value=0)
            self._progress_var.set(f"{mb:.1f} MB downloaded")

    def _cancel_download(self) -> None:
        self._cancelled = True
        self._progress_var.set("Cancelling…")
        self._secondary.config(state=tk.DISABLED)

    def _download_failed(self, message: str) -> None:
        self._downloading = False
        self._set_picker_enabled(True)
        self._progress.config(value=0)
        if self._cancelled:
            self._progress_row.pack_forget()
            self._progress_var.set("")
            self._primary.config(state=tk.NORMAL, text="Download & Install")
            self._secondary.config(text="Later", state=tk.NORMAL, command=self._close)
            return
        self._progress_var.set(message)
        self._primary.config(state=tk.NORMAL, text="Try Again")
        self._secondary.config(text="Close", state=tk.NORMAL, command=self._close)

    def _download_done(self, pending: PendingUpdate) -> None:
        self._downloading = False
        self._pending = pending
        self._progress_var.set("")
        self._secondary.config(state=tk.NORMAL)
        self._render()
        if self._app is not None and hasattr(self._app, "note_pending_update"):
            self._app.note_pending_update(pending)

    def _restart(self) -> None:
        """Re-exec the launcher, then shut the app down so the swap can happen."""
        launcher = updater.launcher_path()
        if launcher is None:
            self._detail_var.set("Close the app and start it again to finish installing the update.")
            self._primary.config(state=tk.DISABLED)
            return

        root = launcher.parent
        try:
            if sys.platform == "win32":
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen(
                    [str(launcher)],
                    cwd=str(root),
                    close_fds=True,
                    creationflags=flags,
                    shell=True,
                )
            else:
                subprocess.Popen(
                    ["/bin/bash", str(launcher)],
                    cwd=str(root),
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            self._detail_var.set(
                f"Could not relaunch automatically ({exc}). Close the app and "
                "start it again to finish installing the update."
            )
            return

        self._close()
        if self._app is not None and hasattr(self._app, "_on_close"):
            self._app._on_close()

    def _close(self) -> None:
        # Stop the pollers before the widgets go away. The worker threads are
        # daemons and their queue writes are harmless once nothing drains them.
        self._cancelled = True
        self._downloading = False
        self._loading_releases = False
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        if self._releases_poll_id is not None:
            try:
                self.after_cancel(self._releases_poll_id)
            except tk.TclError:
                pass
            self._releases_poll_id = None
        try:
            self.destroy()
        except tk.TclError:
            pass
