"""Modal for editing the per-model TrainingConfig (27 fields).

Fields not used by ConvNeXt-only training (Inception/DeepLearning bits) are
omitted.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

from ..models import TrainingConfig


class TrainingConfigDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        config: TrainingConfig,
        *,
        on_saved: Callable[[TrainingConfig], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Training settings")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self._original = config
        self._on_saved = on_saved

        # Build variables once so save can re-read them.
        self.vars: dict[str, tk.Variable] = {
            "epochs": tk.IntVar(value=config.epochs),
            "batch_size": tk.IntVar(value=config.batch_size),
            "learning_rate": tk.DoubleVar(value=config.learning_rate),
            "weight_decay": tk.DoubleVar(value=config.weight_decay),
            "dropout_rate": tk.DoubleVar(value=config.dropout_rate),
            "val_split": tk.DoubleVar(value=config.val_split),
            "image_size": tk.IntVar(value=config.image_size),
            "max_workers": tk.IntVar(value=config.max_workers),
            "train_all": tk.BooleanVar(value=config.train_all),
            "freeze_backbone": tk.BooleanVar(value=config.freeze_backbone),
            "use_focal_loss": tk.BooleanVar(value=config.use_focal_loss),
            "focal_gamma": tk.DoubleVar(value=config.focal_gamma),
            "stochastic_depth_prob": tk.DoubleVar(value=config.stochastic_depth_prob),
            "use_swa": tk.BooleanVar(value=config.use_swa),
            "swa_start": tk.DoubleVar(value=config.swa_start),
            "swa_mode": tk.StringVar(value=config.swa_mode),
            "swa_acc_threshold": tk.DoubleVar(value=config.swa_acc_threshold),
            "swa_patience": tk.IntVar(value=config.swa_patience),
            "swa_min_epoch": tk.IntVar(value=config.swa_min_epoch),
        }

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        # (label, settings key, Spinbox kwargs) — every row is a Spinbox, so
        # there is no widget-kind column to carry. Explicitly `Any`-valued:
        # each row's kwargs mixes ints/floats/format strings, which the
        # Spinbox constructor's per-keyword overloads don't unify — the
        # dict is genuinely heterogeneous by design, not a typing gap.
        rows: list[tuple[str, str, dict[str, Any]]] = [
            ("Epochs", "epochs", {"from_": 1, "to": 200}),
            ("Batch size", "batch_size", {"from_": 1, "to": 1024}),
            ("Learning rate", "learning_rate", {"from_": 1e-6, "to": 1.0, "increment": 1e-5, "format": "%.6f"}),
            ("Weight decay", "weight_decay", {"from_": 0.0, "to": 1.0, "increment": 1e-5, "format": "%.6f"}),
            ("Dropout", "dropout_rate", {"from_": 0.0, "to": 1.0, "increment": 0.05, "format": "%.2f"}),
            ("Validation split", "val_split", {"from_": 0.0, "to": 0.99, "increment": 0.05, "format": "%.2f"}),
            ("Image size", "image_size", {"from_": 64, "to": 512}),
            ("Max workers (-1 = auto)", "max_workers", {"from_": -1, "to": 64}),
            (
                "Stochastic depth prob (-1 = default)",
                "stochastic_depth_prob",
                {"from_": -1.0, "to": 0.5, "increment": 0.05, "format": "%.2f"},
            ),
        ]
        for i, (lbl, key, kwargs) in enumerate(rows):
            ttk.Label(frm, text=lbl).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            ttk.Spinbox(frm, textvariable=self.vars[key], width=12, **kwargs).grid(
                row=i,
                column=1,
                sticky="w",
                padx=4,
                pady=2,
            )

        flags_row = len(rows)
        ttk.Checkbutton(frm, text="Train on full dataset (override val split)", variable=self.vars["train_all"]).grid(
            row=flags_row,
            column=0,
            columnspan=2,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Checkbutton(
            frm, text="Freeze backbone (only train classifier)", variable=self.vars["freeze_backbone"]
        ).grid(
            row=flags_row + 1,
            column=0,
            columnspan=2,
            sticky="w",
            padx=4,
            pady=2,
        )

        # Focal Loss
        ttk.Separator(frm, orient=tk.HORIZONTAL).grid(
            row=flags_row + 2,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=8,
        )
        ttk.Checkbutton(frm, text="Use focal loss", variable=self.vars["use_focal_loss"]).grid(
            row=flags_row + 3,
            column=0,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Spinbox(
            frm, from_=0.1, to=10.0, increment=0.1, format="%.1f", width=12, textvariable=self.vars["focal_gamma"]
        ).grid(
            row=flags_row + 3,
            column=1,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(frm, text="(focal gamma)").grid(row=flags_row + 4, column=1, sticky="w", padx=4)

        # SWA
        ttk.Separator(frm, orient=tk.HORIZONTAL).grid(
            row=flags_row + 5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=8,
        )
        ttk.Checkbutton(frm, text="Use stochastic weight averaging (SWA)", variable=self.vars["use_swa"]).grid(
            row=flags_row + 6,
            column=0,
            columnspan=2,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(frm, text="SWA start (fraction of epochs)").grid(row=flags_row + 7, column=0, sticky="w", padx=4)
        ttk.Spinbox(
            frm, from_=0.0, to=1.0, increment=0.05, format="%.2f", width=12, textvariable=self.vars["swa_start"]
        ).grid(
            row=flags_row + 7,
            column=1,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(frm, text="SWA mode").grid(row=flags_row + 8, column=0, sticky="w", padx=4)
        ttk.Combobox(
            frm, values=["scheduled", "adaptive"], state="readonly", textvariable=self.vars["swa_mode"], width=12
        ).grid(
            row=flags_row + 8,
            column=1,
            sticky="w",
            padx=4,
            pady=2,
        )

        btns = ttk.Frame(self, padding=(12, 0, 12, 12))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save", command=self._save, style="Accent.TButton").pack(side=tk.RIGHT, padx=(0, 8))

        self.bind("<Escape>", lambda _e: self.destroy())

    def _save(self) -> None:
        cfg = self._original
        try:
            cfg.epochs = int(self.vars["epochs"].get())
            cfg.batch_size = int(self.vars["batch_size"].get())
            cfg.learning_rate = float(self.vars["learning_rate"].get())
            cfg.weight_decay = float(self.vars["weight_decay"].get())
            cfg.dropout_rate = float(self.vars["dropout_rate"].get())
            cfg.val_split = float(self.vars["val_split"].get())
            cfg.image_size = int(self.vars["image_size"].get())
            cfg.max_workers = int(self.vars["max_workers"].get())
            cfg.train_all = bool(self.vars["train_all"].get())
            cfg.freeze_backbone = bool(self.vars["freeze_backbone"].get())
            cfg.use_focal_loss = bool(self.vars["use_focal_loss"].get())
            cfg.focal_gamma = float(self.vars["focal_gamma"].get())
            cfg.stochastic_depth_prob = float(self.vars["stochastic_depth_prob"].get())
            cfg.use_swa = bool(self.vars["use_swa"].get())
            cfg.swa_start = float(self.vars["swa_start"].get())
            cfg.swa_mode = str(self.vars["swa_mode"].get())
            cfg.swa_acc_threshold = float(self.vars["swa_acc_threshold"].get())
            cfg.swa_patience = int(self.vars["swa_patience"].get())
            cfg.swa_min_epoch = int(self.vars["swa_min_epoch"].get())
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Invalid value", str(exc), parent=self)
            return
        if self._on_saved is not None:
            self._on_saved(cfg)
        self.destroy()
