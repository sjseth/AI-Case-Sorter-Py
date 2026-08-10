"""Modal dialog for creating or editing a Model.

Minimal field set this round — name, cartridge, model mode, primer-mask
settings. Advanced training hyperparameters live in a separate dialog
(``dialog_training_config``).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from ..data.db import Database
from ..data.models import (
    FEEDBACK_UPLOAD_MODES,
    SUPPORTED_MODEL_MODES,
    AIModelConfig,
    ImageProcessingConfig,
    Model,
    TrainingConfig,
)
from ..data.repository import CartridgeRepo, ModelRepo

# Upload-mode value (persisted) <-> display label (shown in the combobox).
_FEEDBACK_MODE_LABELS = {
    "Instant": "Instant",
    "OnRunComplete": "On Run Complete",
    "Manual": "Manual",
}
_FEEDBACK_MODE_BY_LABEL = {label: value for value, label in _FEEDBACK_MODE_LABELS.items()}


def _is_community_model(model: Model | None) -> bool:
    return bool(model is not None and (model.community_model_uid or model.model_type == "CommunityManaged"))


class ModelEditorDialog(tk.Toplevel):
    """Returns the saved Model id via the ``on_saved`` callback."""

    def __init__(
        self,
        parent: tk.Misc,
        db: Database,
        *,
        existing: Model | None = None,
        on_saved: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.existing = existing
        self.on_saved = on_saved
        self.cartridge_repo = CartridgeRepo(db)
        self.model_repo = ModelRepo(db)

        self.title("Edit Model" if existing else "New Model")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(frm, text="Name:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=(existing.name if existing else ""))
        ttk.Entry(frm, textvariable=self.name_var, width=32).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(frm, text="Cartridge:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.cartridge_combo = ttk.Combobox(frm, state="readonly", width=30)
        self.cartridge_combo.grid(row=row, column=1, sticky="ew", pady=4)
        self._cartridges = self.cartridge_repo.list()
        self.cartridge_combo["values"] = [c.name for c in self._cartridges]
        if existing:
            for i, c in enumerate(self._cartridges):
                if c.id == existing.cartridge_id:
                    self.cartridge_combo.current(i)
                    break
        elif self._cartridges:
            self.cartridge_combo.current(0)

        row += 1
        ttk.Label(frm, text="Model type:", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.mode_combo = ttk.Combobox(frm, state="readonly", width=30, values=list(SUPPORTED_MODEL_MODES))
        if existing and existing.model_mode in SUPPORTED_MODEL_MODES:
            self.mode_combo.set(existing.model_mode)
        else:
            self.mode_combo.current(0)  # convnext_tiny
        self.mode_combo.grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        self.hide_primer_var = tk.BooleanVar(value=bool(existing.hide_primer) if existing else True)
        ttk.Checkbutton(frm, text="Hide primer in cropped image", variable=self.hide_primer_var).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=4,
        )

        row += 1
        ttk.Label(frm, text="Primer mask size (px):", anchor=tk.W).grid(row=row, column=0, sticky="w", pady=4)
        self.primer_size_var = tk.IntVar(value=int(existing.primer_mask_size) if existing else 135)
        ttk.Spinbox(frm, from_=0, to=512, textvariable=self.primer_size_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4
        )

        # Community Feedback Loop — only for community models. The publisher's
        # confidence floor is shown read-only; the user may opt out or change
        # the upload mode but never the threshold.
        self._is_community = _is_community_model(existing)
        # `_is_community` implies `existing is not None` (see
        # `_is_community_model`), but re-checking here narrows the type for
        # the checker too, instead of asserting it blindly.
        if self._is_community and existing is not None:
            row += 1
            self._build_feedback_section(frm, existing).grid(
                row=row,
                column=0,
                columnspan=2,
                sticky="ew",
                pady=(10, 2),
            )

        frm.columnconfigure(1, weight=1)

        # Buttons
        btn = ttk.Frame(self, padding=(12, 0, 12, 12))
        btn.pack(fill=tk.X)
        ttk.Button(btn, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn, text="Save", command=self._save, style="Accent.TButton").pack(side=tk.RIGHT)

        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _build_feedback_section(self, parent: tk.Misc, model: Model) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="Community Feedback Loop", padding=8)

        # Opt-out lives here (and only here): unchecking stops uploads.
        self.fb_enabled_var = tk.BooleanVar(value=bool(model.feedback_loop_enabled))
        ttk.Checkbutton(
            box,
            text="Participate in feedback loop",
            variable=self.fb_enabled_var,
            command=self._on_fb_toggle,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        ttk.Label(box, text="Upload mode:", anchor=tk.W).grid(
            row=1,
            column=0,
            sticky="w",
            pady=2,
        )
        current_label = _FEEDBACK_MODE_LABELS.get(
            model.feedback_loop_upload_mode,
            _FEEDBACK_MODE_LABELS["Instant"],
        )
        self.fb_mode_var = tk.StringVar(value=current_label)
        self.fb_mode_combo = ttk.Combobox(
            box,
            state="readonly",
            width=22,
            textvariable=self.fb_mode_var,
            values=[_FEEDBACK_MODE_LABELS[m] for m in FEEDBACK_UPLOAD_MODES],
        )
        self.fb_mode_combo.grid(row=1, column=1, sticky="w", pady=2)

        # Confidence floor is publisher-controlled: shown, never editable.
        ttk.Label(box, text="Confidence floor:", anchor=tk.W).grid(
            row=2,
            column=0,
            sticky="w",
            pady=2,
        )
        ttk.Label(
            box,
            text=f"{int(model.feedback_loop_confidence_floor)}%  (set by publisher)",
            style="Muted.TLabel",
        ).grid(row=2, column=1, sticky="w", pady=2)

        box.columnconfigure(1, weight=1)
        self._on_fb_toggle()  # sync the combobox enabled state with the checkbox
        return box

    def _on_fb_toggle(self) -> None:
        self.fb_mode_combo.configure(state="readonly" if self.fb_enabled_var.get() else "disabled")

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("Missing name", "Please enter a model name.", parent=self)
            return
        idx = self.cartridge_combo.current()
        if idx < 0:
            messagebox.showwarning("Missing cartridge", "Pick a cartridge first.", parent=self)
            return
        cartridge = self._cartridges[idx]
        mode = self.mode_combo.get()
        if mode not in SUPPORTED_MODEL_MODES:
            messagebox.showerror("Invalid model type", f"Unknown model: {mode}", parent=self)
            return

        try:
            if self.existing is None:
                model = Model(
                    name=name,
                    cartridge_id=cartridge.id,
                    model_mode=mode,
                    hide_primer=self.hide_primer_var.get(),
                    primer_mask_size=int(self.primer_size_var.get()),
                    image_processing=ImageProcessingConfig(),
                    training_config=TrainingConfig(model_name=mode),
                    ai_model_config=AIModelConfig(),
                )
                saved = self.model_repo.create(model)
                saved_id = saved.id
            else:
                self.existing.name = name
                self.existing.cartridge_id = cartridge.id
                self.existing.model_mode = mode
                self.existing.hide_primer = self.hide_primer_var.get()
                self.existing.primer_mask_size = int(self.primer_size_var.get())
                self.existing.training_config.model_name = mode
                if self._is_community:
                    # Editable: opt-out flag + upload mode. Never the floor.
                    self.existing.feedback_loop_enabled = bool(self.fb_enabled_var.get())
                    self.existing.feedback_loop_upload_mode = _FEEDBACK_MODE_BY_LABEL.get(
                        self.fb_mode_var.get(),
                        self.existing.feedback_loop_upload_mode,
                    )
                self.model_repo.update(self.existing)
                saved_id = self.existing.id
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return

        if self.on_saved is not None:
            self.on_saved(saved_id)
        self.destroy()
