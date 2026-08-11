"""Community tab — browse and download published models.

Community Models layout:
  Toolbar:   [Share a Model]              Community Info: <username link>
  Filters:   [Cartridge ▾] [Type ▾] [search...] [Search]
  List:      <vertically stacked CommunityModelCard>s in a plain frame
             (the tab is already inside the per-tab ScrollableFrame from
             app._add_scrolled — a second one here would render a
             redundant inner scrollbar)

`Share a Model` is rendered disabled until the upload flow is in scope.
"""

from __future__ import annotations

import tempfile
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from ..community.community_api import CartridgeInfo, CommunityApi, ModelInfo
from ..control.events import EventBus
from ..data.config import Config
from ..data.model_io import import_model
from ..data.repository import ModelRepo

_TYPE_VALUES = ("All", "ModelOnly", "ModelAndImages", "ImagesOnly")


def _format_size(n: int) -> str:
    if n <= 0:
        return "—"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.2f} {unit}"
        val /= 1024
    return f"{val:.2f} TB"


def _format_date(iso: str) -> str:
    """ISO-ish → 'M/D/YYYY' to match the legacy look. Falls back to raw."""
    from datetime import datetime

    if not iso:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(iso[: len(fmt) + 6 if "%S" in fmt else len(fmt)], fmt).strftime("%-m/%-d/%Y")
        except (ValueError, OSError):
            continue
    return iso


class CommunityModelCard(ttk.Frame):
    """Wide card: name, info grid, description, right-aligned action button."""

    def __init__(
        self,
        parent: tk.Misc,
        info: ModelInfo,
        *,
        installed_state: str,  # 'download' | 'update' | 'installed'
        on_action: Callable[[ModelInfo], None],
    ) -> None:
        super().__init__(parent, style="Card.TFrame", padding=14)
        # Not `self.info` — that collides with ttk.Widget.info() (place_info
        # bound-method lookup), a real latent bug the type checker caught.
        self.model_info = info
        self.installed_state = installed_state
        self.on_action = on_action

        # Header: model name
        ttk.Label(
            self,
            text=info.model_name,
            style="CardTitle.TLabel",
        ).pack(side=tk.TOP, anchor="w")

        # Info grid (3 columns × 3 rows)
        grid = ttk.Frame(self, style="CardRow.TFrame")
        grid.pack(side=tk.TOP, fill=tk.X, pady=(8, 6))

        def _cell(row: int, col: int, label: str, value: str) -> None:
            f = ttk.Frame(grid, style="CardRow.TFrame")
            f.grid(row=row, column=col, sticky="w", padx=(0, 32), pady=2)
            ttk.Label(f, text=f"{label}:", style="CardSubtle.TLabel").pack(side=tk.LEFT)
            ttk.Label(f, text=value, style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(6, 0))

        _cell(0, 0, "Cartridge", info.cartridge_name or "—")
        _cell(0, 1, "File Size", _format_size(info.download_size))
        _cell(0, 2, "Publish Date", _format_date(info.publish_date))
        _cell(1, 0, "Model Type", info.export_mode or "—")
        _cell(1, 1, "Headstamp Count", str(info.headstamp_count))
        _cell(1, 2, "Author", info.author or "—")
        _cell(2, 0, "Image Count", str(info.image_count))
        _cell(2, 1, "Version", f"v{info.model_version}")

        # Description
        if info.model_description:
            ttk.Label(
                self,
                text=info.model_description,
                style="CardSubtle.TLabel",
                wraplength=900,
                justify=tk.LEFT,
            ).pack(side=tk.TOP, anchor="w", pady=(2, 8), fill=tk.X)

        # Action button (right-aligned)
        action_row = ttk.Frame(self, style="CardRow.TFrame")
        action_row.pack(side=tk.TOP, fill=tk.X)
        ttk.Frame(action_row, style="CardRow.TFrame").pack(side=tk.LEFT, fill=tk.X, expand=True)
        if installed_state == "installed":
            btn = ttk.Button(action_row, text="Already Installed", state=tk.DISABLED)
        elif installed_state == "update":
            # Blue, not the download green: this replaces a model already in
            # the library rather than adding one.
            btn = ttk.Button(
                action_row, text="Update Model", style="Update.TButton", command=lambda: self.on_action(self.model_info)
            )
        else:
            btn = ttk.Button(
                action_row,
                text="Download Model",
                style="Accent.TButton",
                command=lambda: self.on_action(self.model_info),
            )
        btn.pack(side=tk.RIGHT)


class CommunityTab(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        config: Config,
        bus: EventBus,
        app: Any,
    ) -> None:
        super().__init__(parent)
        # Not `self.config` — that collides with ttk.Widget.config().
        self.cfg = config
        self.bus = bus
        self.app = app
        self.db = app.db
        self._models: list[ModelInfo] = []
        self._cartridges: list[CartridgeInfo] = []

        self._build_toolbar()
        self._build_filter_bar()
        self._build_status_label()
        self._build_models_list()

        # Populate filters once the tab is laid out.
        self.after(120, self._initial_load)

        # Mirror download/import status into the in-tab label. Posts come
        # from worker threads via the bus, so we have to drain through the
        # event loop instead of writing the StringVar directly — see
        # _post_progress.
        self.bus.subscribe(
            "community/status",
            lambda msg: self.status_var.set(str(msg)),
        )

    # ----- status helpers -----------------------------------------------------

    def _post_progress(self, message: str) -> None:
        """Surface a status line on the main app's bottom bar AND the
        in-tab label. Bus-routed so it's safe to call from the download
        worker thread."""
        self.bus.post("status", message)
        self.bus.post("community/status", message)

    def _api(self) -> CommunityApi:
        return CommunityApi(auth=self.app.auth)

    # ----- UI build -----------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(8, 0))
        # Share is gated on the user having the Contribute role on the
        # community server. Start hidden; `_load_community_info` re-shows it
        # after the role check
        # comes back so non-contributors never see the button at all.
        self.share_button = ttk.Button(
            bar,
            text="Share a Model",
            command=self._open_share_dialog,
        )
        self.share_button.pack(side=tk.LEFT)
        self.share_button.pack_forget()
        # Resolved community profile name, used as the Author on shared models.
        self._community_username = ""

        right = ttk.Frame(bar)
        right.pack(side=tk.RIGHT)
        ttk.Label(right, text="Community Info:", style="Muted.TLabel").pack(side=tk.LEFT)
        # Resolved asynchronously — identity() may do a silent token read.
        self.community_info_var = tk.StringVar(value="…")
        ttk.Label(right, textvariable=self.community_info_var, style="Accent.TLabel").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(right, text="Sign out", command=self._sign_out).pack(side=tk.LEFT, padx=(16, 0))
        self._load_community_info()

    def _load_community_info(self) -> None:
        """Resolve the signed-in user's name + email and Contribute role
        off the UI thread. identity() can do a silent token read and
        get_user_metadata() always hits the network, so they run in a
        worker; results marshalled back via the bus-backed run_worker
        callbacks.
        """

        def _work() -> tuple[str, bool, str]:
            name, email = self.app.auth.identity()
            try:
                meta = self._api().get_user_metadata()
            except Exception:
                meta = None
            # Fall back to the community profile handle when the B2C token
            # carries no name claim, so the label still shows something
            # human-readable alongside the email.
            profile = (meta.profile_name if meta else "") or ""
            if not name and profile:
                name = profile
            display = self._format_identity(name, email)
            can_contribute = bool(meta and meta.can_contribute())
            # Author for shared models: the community profile name, else the
            # display name, else the email local part.
            username = profile or name or (email.split("@")[0] if email else "")
            return display, can_contribute, username

        def _ok(payload: tuple[str, bool, str]) -> None:
            display, can_contribute, username = payload
            self.community_info_var.set(display)
            self._community_username = username
            self._set_share_visible(can_contribute)

        def _fail(_exc: Exception) -> None:
            self.community_info_var.set("(unknown)")
            self._set_share_visible(False)

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _set_share_visible(self, visible: bool) -> None:
        """Toggle the Share button. The toolbar has only two children
        (this button on the left, the info+sign-out frame on the right),
        so re-packing with side=LEFT puts it back in place."""
        if visible:
            self.share_button.pack(side=tk.LEFT)
        else:
            self.share_button.pack_forget()

    @staticmethod
    def _format_identity(name: str | None, email: str | None) -> str:
        name = (name or "").strip()
        email = (email or "").strip()
        if name and email:
            return f"{name} ({email})"
        return name or email or "(unknown)"

    def _build_filter_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(8, 4))

        self.cart_var = tk.StringVar(value="All")
        self.cart_combo = ttk.Combobox(bar, state="readonly", width=14, textvariable=self.cart_var)
        self.cart_combo.pack(side=tk.LEFT)
        self.cart_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh())

        self.type_var = tk.StringVar(value="All")
        self.type_combo = ttk.Combobox(
            bar,
            state="readonly",
            width=18,
            textvariable=self.type_var,
            values=list(_TYPE_VALUES),
        )
        self.type_combo.current(0)
        self.type_combo.pack(side=tk.LEFT, padx=(8, 0))
        self.type_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh())

        self.search_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.search_var, width=36)
        entry.pack(side=tk.LEFT, padx=(8, 0))
        entry.bind("<Return>", lambda _e: self._refresh())

        ttk.Button(bar, text="Search", style="Accent.TButton", command=self._refresh).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bar, text="Refresh filters", command=self._populate_cartridge_filter).pack(side=tk.LEFT, padx=(8, 0))

    def _build_status_label(self) -> None:
        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel").pack(fill=tk.X, padx=10, pady=(2, 4))

    def _build_models_list(self) -> None:
        # The whole tab is already hosted in a ScrollableFrame (see
        # app._add_scrolled), so the model list lives in a plain frame —
        # otherwise we'd render a redundant inner scrollbar alongside the
        # tab's outer one.
        self._list_body = ttk.Frame(self)
        self._list_body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ----- data flow ----------------------------------------------------------

    def _initial_load(self) -> None:
        self._populate_cartridge_filter()
        self._refresh()

    def _populate_cartridge_filter(self) -> None:
        def _work():
            return self._api().get_available_cartridges()

        def _ok(carts: list[CartridgeInfo]):
            self._cartridges = carts
            names = ["All"] + [c.name for c in carts]
            self.cart_combo["values"] = names

        def _fail(exc: Exception):
            self.status_var.set(f"Could not load cartridges: {exc}")

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _refresh(self) -> None:
        cart = "" if self.cart_var.get() == "All" else self.cart_var.get()
        type_ = "" if self.type_var.get() == "All" else self.type_var.get()
        search = self.search_var.get().strip()
        self.status_var.set("Loading…")
        for child in list(self._list_body.winfo_children()):
            child.destroy()

        def _work():
            return self._api().get_models(search=search, model_type=type_, cartridge=cart)

        def _ok(models: list[ModelInfo]):
            self._models = list(models)
            if not models:
                self.status_var.set("No community models found for these filters.")
                return
            self.status_var.set(f"{len(models)} model(s) available")
            for info in models:
                state = self._installed_state(info)
                card = CommunityModelCard(
                    self._list_body,
                    info,
                    installed_state=state,
                    on_action=self._download,
                )
                card.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))

        def _fail(exc: Exception):
            self.status_var.set(f"Failed: {exc}")

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _installed_state(self, info: ModelInfo) -> str:
        if not info.model_uid:
            return "download"
        local = ModelRepo(self.db).find_by_community_uid(info.model_uid)
        if local is None:
            return "download"
        if info.model_version > local.model_version:
            return "update"
        return "installed"

    # ----- download -----------------------------------------------------------

    def _download(self, info: ModelInfo) -> None:
        name = info.model_name or info.model_uid or "model"
        # An update refreshes the installed model in place (import_model
        # matches on the community UID), so the library doesn't gain a
        # second copy and the user's slot assignments and sorting templates
        # carry over. Remembered here, on the UI thread, for the messaging.
        is_update = self._installed_state(info) == "update"
        if not messagebox.askyesno(
            "Download model — security notice",
            f'"{name}" is a community-published model. Models are loaded with '
            "PyTorch and can execute code embedded in the file, so only download "
            "models from authors you trust.\n\n"
            + ("Download and install this update?" if is_update else "Download and import this model?"),
            icon="warning",
            default="no",
            parent=self,
        ):
            return
        self._post_progress(f"Downloading {name}…")

        def _work():
            api = self._api()
            payload = api.request_download(info.model_uid)
            url = payload.get("FullUrl") or payload.get("fullUrl")
            if not url:
                raise RuntimeError("Server did not return a download URL")
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = Path(tmpdir) / f"{info.model_uid}.zip"

                # Throttle download progress to whole-percent changes so we
                # don't flood the bus (download_to fires the callback per
                # 64 KB chunk — ~16 calls per MB).
                last_pct: list[int] = [-1]

                def _dl_progress(done: int, total: int | None) -> None:
                    if total and total > 0:
                        pct = int(done * 100 / total)
                        if pct != last_pct[0]:
                            last_pct[0] = pct
                            mb_done = done / (1024 * 1024)
                            mb_total = total / (1024 * 1024)
                            self._post_progress(f"Downloading {name}: {pct}% ({mb_done:.1f} / {mb_total:.1f} MB)")
                    else:
                        mb_done = done / (1024 * 1024)
                        # No Content-Length: report bytes-done every ~1 MB.
                        if int(mb_done) != last_pct[0]:
                            last_pct[0] = int(mb_done)
                            self._post_progress(f"Downloading {name}: {mb_done:.1f} MB")

                api.download_to(
                    url,
                    zip_path,
                    expected_total=info.download_size or None,
                    progress=_dl_progress,
                )

                self._post_progress(f"Importing {name}…")

                last_import_pct: list[int] = [-1]

                def _import_progress(step: int, total: int) -> None:
                    if total <= 0:
                        return
                    pct = int(step * 100 / total)
                    if pct != last_import_pct[0]:
                        last_import_pct[0] = pct
                        self._post_progress(f"Importing {name}: {pct}% ({step} / {total} files)")

                # community_download=True marks the install as belonging to
                # its publisher, which is what keeps the Train tab off it.
                result = import_model(
                    zip_path,
                    db=self.db,
                    community_download=True,
                    progress=_import_progress,
                )
                self._record_installed_version(result[1], info)
                return result

        def _ok(result):
            _cart_id, mid = result
            self._post_progress(f"Updated {name} to v{info.model_version}." if is_update else f"Imported {name}.")
            self._notify_import(mid, updated=is_update)
            models_tab = getattr(self.app, "models_tab", None)
            if models_tab is not None:
                models_tab.refresh()
            self._refresh()

        def _fail(exc: Exception):
            self._post_progress(f"Download failed: {exc}")
            messagebox.showerror("Download failed", str(exc), parent=self)

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _record_installed_version(self, model_id: int, info: ModelInfo) -> None:
        """Stamp the server's version onto the freshly-installed model.

        The card's Download / Update / Already Installed state compares the
        server's `model_version` against the local row. The archive's own
        manifest is what import writes, and it can lag what the catalogue
        advertises — which would leave a just-installed model still showing
        "Update Model". The bytes came from this catalogue entry, so its
        version is the authoritative one to record.

        Runs on the download worker thread; DB access is serialized by the
        Database lock.
        """
        repo = ModelRepo(self.db)
        model = repo.get(model_id)
        if model is None or int(info.model_version) <= int(model.model_version):
            return
        model.model_version = int(info.model_version)
        repo.update(model)

    def _notify_import(self, model_id: int, *, updated: bool = False) -> None:
        """Post-import dialog. Community models with the feedback loop enabled
        get the parity notice (threshold + how to opt out); others get the
        plain confirmation."""
        if updated:
            messagebox.showinfo(
                "Update complete",
                "The installed model was updated in place — your slot assignments and sorting templates were kept.",
                parent=self,
            )
            return
        model = ModelRepo(self.db).get(model_id)
        if model is not None and model.feedback_loop_enabled and model.community_model_uid:
            messagebox.showinfo(
                "Feedback loop enabled",
                "Feedback Loop has been enabled for this model, which may include "
                f"automatic upload of images below the {model.feedback_loop_confidence_floor}% "
                "confidence threshold for the model owner to review.\n\n"
                "To change the upload mode or opt out, open the model in the Models "
                "tab, click Edit, and adjust the Community Feedback Loop settings.",
                parent=self,
            )
            return
        messagebox.showinfo("Download complete", "Model imported.", parent=self)

    def _open_share_dialog(self) -> None:
        from .dialog_share_model import ShareModelDialog

        if not ModelRepo(self.db).list():
            messagebox.showinfo(
                "No models to share",
                "Create and train a local model on the Models tab first.",
                parent=self,
            )
            return
        ShareModelDialog(self, app=self.app, username=self._community_username)

    def _sign_out(self) -> None:
        if not messagebox.askyesno("Sign out", "Sign out of the community?", parent=self):
            return
        try:
            self.app.auth.logout()
        except Exception as exc:
            messagebox.showerror("Sign-out failed", str(exc), parent=self)
            return
        self.app._unmount_community_tab()
