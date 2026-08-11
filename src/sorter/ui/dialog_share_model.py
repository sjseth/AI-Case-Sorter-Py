"""Share-a-Model dialog — export a local model and upload it to the community.

Collapses the legacy pick → upload-info → upload-status flow into one dialog:

  * pick a local model,
  * fill the share metadata (name, description, community cartridge, what to
    include, optional feedback loop),
  * Share → export the ZIP (+ standalone manifest) and run the upload sequence
    (FileUploadRequest → PUT zip → CompleteUpload → ManifestUploadRequest →
    PUT manifest), reporting progress, then stamp the returned community UID /
    version onto the local model.
"""

from __future__ import annotations

import shutil
import tempfile
import tkinter as tk
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from uuid import uuid4

from .. import paths
from ..community.community_api import CartridgeInfo, CommunityApi
from ..data.model_io import ExportMode, export_for_share
from ..data.repository import CartridgeRepo, HeadstampRepo, ModelRepo

# Display label <-> ExportMode. ManifestOnly is intentionally omitted — there
# is nothing useful to share without either a model or images.
_EXPORT_LABELS = {
    "Model and images": ExportMode.MODEL_AND_IMAGES,
    "Model only": ExportMode.MODEL_ONLY,
    "Images only": ExportMode.IMAGES_ONLY,
}

# Server-side ModelExportMode is a numeric enum. Send the matching integer so
# the endpoint binds it (ModelOnly=0, ModelAndImages=1, ImagesOnly=2,
# ManifestOnly=3).
_EXPORT_MODE_INT = {
    ExportMode.MODEL_ONLY: 0,
    ExportMode.MODEL_AND_IMAGES: 1,
    ExportMode.IMAGES_ONLY: 2,
}


def _image_count(images_dir: Path) -> int:
    if not images_dir.exists():
        return 0
    return sum(1 for p in images_dir.iterdir() if p.is_file())


class ShareModelDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, *, app: Any, username: str = "") -> None:
        super().__init__(parent)
        self.app = app
        self.db = app.db
        self.bus = app.bus
        self.username = username or ""
        self.model_repo = ModelRepo(self.db)
        self.cartridge_repo = CartridgeRepo(self.db)
        self.headstamp_repo = HeadstampRepo(self.db)

        self._models = self.model_repo.list()
        self._community_cartridges: list[CartridgeInfo] = []
        self._in_flight = False

        self.title("Share a Model")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(frm, text="Model:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.model_combo = ttk.Combobox(frm, state="readonly", width=34, values=[m.name for m in self._models])
        self.model_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self.model_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_model_selected())
        if self._models:
            self.model_combo.current(0)

        row += 1
        ttk.Label(frm, text="Share name:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.name_var, width=36).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(frm, text="Description:", anchor=tk.NW).grid(row=row, column=0, sticky="nw", pady=4)
        self.desc_text = tk.Text(frm, width=36, height=3, wrap=tk.WORD)
        self.desc_text.grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(frm, text="Cartridge:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.cartridge_combo = ttk.Combobox(frm, state="readonly", width=34)
        self.cartridge_combo.grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(frm, text="Include:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.export_combo = ttk.Combobox(frm, state="readonly", width=34, values=list(_EXPORT_LABELS.keys()))
        self.export_combo.current(0)
        self.export_combo.grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        fb_box = ttk.LabelFrame(frm, text="Community Feedback Loop", padding=8)
        fb_box.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        self.fb_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            fb_box,
            text="Enable feedback loop",
            variable=self.fb_enabled_var,
            command=self._on_fb_toggle,
        ).pack(anchor=tk.W)
        floor_row = ttk.Frame(fb_box)
        floor_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(floor_row, text="Confidence floor", style="Muted.TLabel").pack(side=tk.LEFT)
        self.fb_floor_var = tk.IntVar(value=95)
        self.fb_floor_spin = ttk.Spinbox(
            floor_row,
            from_=0,
            to=100,
            width=6,
            textvariable=self.fb_floor_var,
            state="disabled",
        )
        self.fb_floor_spin.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(floor_row, text="%", style="Muted.TLabel").pack(side=tk.LEFT, padx=(2, 0))

        frm.columnconfigure(1, weight=1)

        self.status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.status_var, style="Muted.TLabel", wraplength=360, justify=tk.LEFT).grid(
            row=row + 1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(6, 0),
        )

        btn = ttk.Frame(self, padding=(12, 0, 12, 12))
        btn.pack(fill=tk.X)
        self.cancel_btn = ttk.Button(btn, text="Close", command=self._on_close)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.share_btn = ttk.Button(btn, text="Share", command=self._share, style="Accent.TButton")
        self.share_btn.pack(side=tk.RIGHT)

        self.bus.subscribe("share/progress", self._on_progress)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _e: self._on_close())

        self._on_model_selected()
        self._load_community_cartridges()

    # ----- helpers ------------------------------------------------------------

    def _selected_model(self):
        idx = self.model_combo.current()
        if idx < 0 or idx >= len(self._models):
            return None
        return self._models[idx]

    def _local_cartridge_name(self, model) -> str:
        cart = self.cartridge_repo.get(model.cartridge_id) if model else None
        return cart.name if cart else ""

    def _on_model_selected(self) -> None:
        model = self._selected_model()
        if model is None:
            return
        self.name_var.set(model.name)
        self._sync_cartridge_default(model)

    def _sync_cartridge_default(self, model) -> None:
        """Preselect the community cartridge matching the model's local one."""
        target = self._local_cartridge_name(model).casefold()
        for i, c in enumerate(self._community_cartridges):
            if c.name.casefold() == target:
                self.cartridge_combo.current(i)
                return

    def _on_fb_toggle(self) -> None:
        self.fb_floor_spin.config(state="normal" if self.fb_enabled_var.get() else "disabled")

    def _load_community_cartridges(self) -> None:
        def _work():
            return CommunityApi(auth=self.app.auth).get_available_cartridges()

        def _ok(carts: list[CartridgeInfo]):
            self._community_cartridges = list(carts)
            self.cartridge_combo["values"] = [c.name for c in carts]
            if carts:
                self.cartridge_combo.current(0)
                model = self._selected_model()
                if model is not None:
                    self._sync_cartridge_default(model)

        def _fail(exc: Exception):
            self.status_var.set(f"Could not load cartridges: {exc}")

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _on_progress(self, message: str) -> None:
        try:
            self.status_var.set(str(message))
        except tk.TclError:
            pass

    # ----- share --------------------------------------------------------------

    def _share(self) -> None:
        if self._in_flight:
            return
        model = self._selected_model()
        if model is None:
            messagebox.showinfo("Pick a model", "Select a model to share.", parent=self)
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("Missing name", "Enter a share name.", parent=self)
            return
        mode = _EXPORT_LABELS[self.export_combo.get()]
        wants_model = mode in (ExportMode.MODEL_ONLY, ExportMode.MODEL_AND_IMAGES)
        model_file = model.model_path if (model.model_path and Path(model.model_path).exists()) else None
        if wants_model and model_file is None:
            messagebox.showwarning(
                "No trained model",
                'This model has no trained file yet. Choose "Images only" or train the model first.',
                parent=self,
            )
            return

        cart_idx = self.cartridge_combo.current()
        if 0 <= cart_idx < len(self._community_cartridges):
            cart = self._community_cartridges[cart_idx]
            cart_id, cart_name = cart.id, cart.name
        else:
            cart_id, cart_name = 0, self._local_cartridge_name(model)

        desc = self.desc_text.get("1.0", tk.END).strip()
        fb_enabled = bool(self.fb_enabled_var.get())
        fb_floor = int(self.fb_floor_var.get()) if fb_enabled else 0
        hs_names = [h.name for h in self.headstamp_repo.list_for_model(model.id)]
        images_dir = paths.model_images_dir(model.id)
        image_count = _image_count(images_dir)
        # Reuse an existing community UID (re-publish/update) or mint a new one.
        # Standard GUID *with* dashes.
        uid = model.community_model_uid or str(uuid4())
        version = model.model_version + 1 if model.community_model_uid else model.model_version

        self._set_in_flight(True)
        self.status_var.set("Packaging…")

        def _work():
            api = CommunityApi(auth=self.app.auth)
            # Export into the app's own temp folder, not the OS temp dir, which
            # on Windows can be locked or cleaned mid-write. Created fresh and
            # cleaned up afterwards regardless of success.
            scratch_root = paths.export_temp_dir()
            scratch_root.mkdir(parents=True, exist_ok=True)
            tmpdir = tempfile.mkdtemp(dir=str(scratch_root))
            try:
                zip_out = Path(tmpdir) / f"{uid}.zip"

                def _exp_prog(step: int, total: int) -> None:
                    self.bus.post("share/progress", f"Packaging {step}/{total} files…")

                zip_path, manifest_path = export_for_share(
                    zip_out,
                    model,
                    cart_name,
                    hs_names,
                    mode=mode,
                    model_file=model_file,
                    images_dir=images_dir,
                    community_uid=uid,
                    feedback_enabled=fb_enabled,
                    feedback_floor=fb_floor,
                    progress=_exp_prog,
                )
                model_info = {
                    "ModelName": name,
                    "PublishDate": datetime.now(UTC).isoformat(),
                    "CartridgeId": cart_id,
                    "CartridgeName": cart_name,
                    "HeadstampCount": len(hs_names),
                    "ImageCount": image_count,
                    "ModelDescription": desc,
                    "Author": self.username,
                    "ModelExportMode": _EXPORT_MODE_INT.get(mode, 1),
                    "FeedbackLoopEnabled": fb_enabled,
                    "FeedbackLoopConfidenceFloor": fb_floor,
                    "ModelVersion": version,
                    "CommunityModelUID": uid,
                }

                last_pct = [-1]

                def _up_prog(sent: int, total: int) -> None:
                    pct = int(sent * 100 / total) if total else 0
                    if pct != last_pct[0]:
                        last_pct[0] = pct
                        mb_done = sent / (1024 * 1024)
                        mb_total = total / (1024 * 1024)
                        self.bus.post(
                            "share/progress",
                            f"Uploading {pct}% ({mb_done:.1f} / {mb_total:.1f} MB)",
                        )

                final_uid = api.share_model(
                    zip_path=zip_path,
                    manifest_path=manifest_path,
                    model_info=model_info,
                    progress=_up_prog,
                )
                return final_uid or uid, version
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        def _ok(result):
            final_uid, final_version = result
            # Stamp the community identity onto the local model so it shows as
            # installed/up-to-date in the Community browser.
            model.community_model_uid = final_uid
            model.model_version = final_version
            try:
                self.model_repo.update(model)
            except Exception:
                pass
            self._set_in_flight(False)
            self.status_var.set("Shared successfully.")
            messagebox.showinfo(
                "Model shared",
                f"“{name}” has been uploaded to the community.",
                parent=self,
            )
            self._on_close()

        def _fail(exc: Exception):
            self._set_in_flight(False)
            self.status_var.set(f"Share failed: {exc}")
            messagebox.showerror("Share failed", str(exc), parent=self)

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _set_in_flight(self, busy: bool) -> None:
        self._in_flight = busy
        state = "disabled" if busy else "normal"
        try:
            self.share_btn.config(state=state)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        if self._in_flight:
            if not messagebox.askyesno(
                "Upload in progress",
                "An upload is still running. Close anyway?",
                parent=self,
            ):
                return
        try:
            self.bus.unsubscribe("share/progress", self._on_progress)
        except Exception:
            pass
        self.destroy()
