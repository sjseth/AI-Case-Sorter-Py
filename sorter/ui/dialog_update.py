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
from .theme import PALETTE

# Tagged union for the worker->main-thread queue below: the "kind" string
# picks which payload shape goes with it, so _poll_events can narrow by
# literal instead of casting.
_DownloadEvent = (
    tuple[Literal["progress"], tuple[int, int | None]]
    | tuple[Literal["done"], PendingUpdate]
    | tuple[Literal["error"], str]
)


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
        self.geometry("620x520")
        self.minsize(520, 420)
        self.configure(bg=PALETTE["bg_surface"])
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._info = info
        self._app = app
        self._pending = pending if pending is not None else updater.pending_update()
        self._cancelled = False
        self._downloading = False
        self._events: queue.Queue[_DownloadEvent] = queue.Queue()
        self._poll_id: str | None = None

        # Buttons first with side=BOTTOM so the resizable notes area above
        # can't squeeze them off-screen (same ordering trick as the PyTorch
        # install dialog).
        self._btns = ttk.Frame(self, padding=(12, 0, 12, 12))
        self._btns.pack(side=tk.BOTTOM, fill=tk.X)

        wrap = ttk.Frame(self, padding=12)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._build_header(wrap)
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

    # ----- rendering ----------------------------------------------------------

    def _set_notes(self, text: str) -> None:
        self._notes.config(state=tk.NORMAL)
        self._notes.delete("1.0", tk.END)
        self._notes.insert("1.0", text)
        self._notes.config(state=tk.DISABLED)

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
            self._progress_row.pack_forget()
            self._primary.config(text="Restart Now", state=tk.NORMAL)
            self._secondary.config(text="Later", command=self._close)
            return

        info = self._info
        if info is None:
            self._title_var.set("You're up to date")
            self._version_var.set(f"Version {current}")
            self._detail_var.set("No newer release is available.")
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
        if self._downloading or self._info is None:
            return
        self._downloading = True
        self._cancelled = False
        self._primary.config(state=tk.DISABLED, text="Downloading…")
        self._secondary.config(text="Cancel", command=self._cancel_download)
        self._progress_row.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        self._progress.config(value=0)
        self._progress_var.set("Starting download…")

        info = self._info

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
        # Stop the poller before the widgets go away. The download thread is a
        # daemon and its queue writes are harmless once nothing drains them.
        self._cancelled = True
        self._downloading = False
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        try:
            self.destroy()
        except tk.TclError:
            pass
