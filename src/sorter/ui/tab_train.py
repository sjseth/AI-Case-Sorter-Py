"""Train tab — feed-capture-classify-label-train loop.

Layout:
    +--------------------------------------------------+----------+
    | Active model: <name>   [Settings…]  [Train]      |          |
    +--------------------------------------------------+ Counts   |
    | ImagePanel       | [Feed]  <label>  <conf>       | column   |
    |                  | Label: [combobox]             | (click   |
    |                  | [Save]                        | to label |
    |                  | <status / hint line>          | + feed)  |
    +--------------------------------------------------+----------+

Feed button:
  * If the serial broker is connected → broker.feed_one() / force_sort_and_move(0)
  * camera.capture_frame() → image_proc.crop_headstamp() → apply_primer_mask()
  * If the active model has a `model_path` that exists → classify_active() and
    display the predicted label + confidence
  * Holds the cropped frame in `self._last_cropped` so Save / counts-grid-click
    knows what to write.

Counts column:
  * Lists every headstamp of the active model with its image count (zeroes
    included so labels with no images yet are still clickable).
  * Clicking a row saves `self._last_cropped` under that label, increments
    the count, and triggers another Feed for the next case.

Training progress moves to a `TrainingProgressDialog` Toplevel that pops up
when Train is clicked and closes on `training/done|failed|cancelled`.
"""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .. import classifier, image_proc, local_inference, paths
from ..config import Config
from ..events import EventBus
from ..models import Model, is_trainable
from ..repository import HeadstampRepo, ModelRepo, SettingsRepo
from ..training.dataset import class_counts, save_training_image
from ..training.manager import TrainingJob, TrainingManager
from . import torch_gate
from .dialog_training_config import TrainingConfigDialog
from .dialog_training_progress import TrainingProgressDialog
from .widgets import ImagePanel


class _LabelCountCard(ttk.Frame):
    """One row in the counts column. Whole card is clickable."""

    def __init__(
        self,
        parent: tk.Misc,
        label: str,
        count: int,
        on_click,
    ) -> None:
        super().__init__(parent, style="Card.TFrame", padding=8)
        self.on_click = on_click
        self._lbl = ttk.Label(self, text=label, style="CardTitle.TLabel")
        self._lbl.pack(side=tk.LEFT)
        self._count = ttk.Label(self, text=str(count), style="CardMuted.TLabel")
        self._count.pack(side=tk.RIGHT)

        for w in (self, self._lbl, self._count):
            w.bind("<Button-1>", lambda _e: self.on_click(label))
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))

    def _hover(self, on: bool) -> None:
        self.configure(style="CardHover.TFrame" if on else "Card.TFrame")
        title_style = "CardHoverTitle.TLabel" if on else "CardTitle.TLabel"
        muted_style = "CardHoverMuted.TLabel" if on else "CardMuted.TLabel"
        self._lbl.configure(style=title_style)
        self._count.configure(style=muted_style)


class TrainTab(ttk.Frame):
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
        self.models_repo = ModelRepo(self.db)
        self.headstamps_repo = HeadstampRepo(self.db)
        self.settings = SettingsRepo(self.db)
        self.training_manager = TrainingManager(bus)
        self.training_dialog: TrainingProgressDialog | None = None
        self._last_cropped = None
        self._pending_output: tuple[int, str] | None = None
        self._feed_in_flight = False
        self._lock = threading.Lock()
        # Feed offers the torch install at most once per session — see
        # `_offer_torch_for_predictions`.
        self._torch_prompt_shown = False

        self._build_top_row()
        self._build_main_split()

        # Refresh whenever the active model changes (Models tab posts this).
        bus.subscribe("mode/changed", lambda _payload: self._refresh_active_model())
        # Also refresh whenever the user switches *into* this tab — picks up
        # headstamp edits made on the Models tab without needing a dedicated
        # event for every mutation.
        notebook = getattr(app, "notebook", None)
        if notebook is not None:
            notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed, add="+")

        self._refresh_active_model()

    def _on_tab_changed(self, _event) -> None:
        notebook = getattr(self.app, "notebook", None)
        if notebook is None:
            return
        try:
            current = notebook.select()
        except Exception:
            return
        if notebook.tab(current, "text") == "Train":
            self._refresh_active_model()

    # ----- UI build -----------------------------------------------------------

    def _build_top_row(self) -> None:
        row = ttk.Frame(self)
        row.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(row, text="Active model:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.active_var = tk.StringVar(value="(none)")
        ttk.Label(row, textvariable=self.active_var, style="Header.TLabel").pack(side=tk.LEFT, padx=(6, 12))
        ttk.Button(row, text="Train", command=self._start_training, style="Accent.TButton").pack(side=tk.RIGHT)
        ttk.Button(row, text="Settings…", command=self._open_settings).pack(side=tk.RIGHT, padx=(0, 8))

    def _build_main_split(self) -> None:
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # Left — capture panel and controls.
        left = ttk.LabelFrame(body, text="Capture")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        inner = ttk.Frame(left, padding=8)
        inner.pack(fill=tk.BOTH, expand=True)
        self.image_panel = ImagePanel(inner, width=320, height=320)
        self.image_panel.pack(side=tk.LEFT, anchor="n")

        controls = ttk.Frame(inner)
        controls.pack(side=tk.LEFT, anchor="n", padx=(12, 0), fill=tk.BOTH, expand=True)

        feed_row = ttk.Frame(controls)
        feed_row.pack(fill=tk.X)
        ttk.Button(feed_row, text="Feed", command=self._feed, style="Accent.TButton").pack(side=tk.LEFT)
        self.classify_label_var = tk.StringVar(value="")
        ttk.Label(feed_row, textvariable=self.classify_label_var, style="Accent.TLabel").pack(
            side=tk.LEFT, padx=(12, 0)
        )

        # Sort While Training — feed the labelled case to its configured run
        # slot (xf:<slot>) instead of the catch-all (xf:0). Mirrors
        # checkBox1_trainAndSort.
        sort_row = ttk.Frame(controls)
        sort_row.pack(fill=tk.X, pady=(8, 0))
        self._sort_while_training_var = tk.BooleanVar(value=self.cfg.sort_while_training)
        ttk.Checkbutton(
            sort_row,
            text="Sort While Training",
            variable=self._sort_while_training_var,
            command=self._on_toggle_sort_while_training,
        ).pack(side=tk.LEFT)

        ttk.Label(controls, text="Label:", style="Muted.TLabel").pack(
            anchor=tk.W,
            pady=(12, 2),
        )
        self.label_var = tk.StringVar()
        # Fixed widths so these don't stretch across the panel as the window
        # resizes — the wider canvas/counts column gets the extra space.
        self.label_combo = ttk.Combobox(controls, textvariable=self.label_var, width=28)
        self.label_combo.pack(anchor=tk.W)

        ttk.Button(controls, text="Save image", width=22, command=self._save_clicked).pack(anchor=tk.W, pady=(8, 0))

        # Per-Feed timing. `image_proc_ms` covers feed + capture + crop +
        # primer mask; `predict_ms` covers the local_inference.classify
        # call (zero when the active model has no .pth to predict with).
        # `predict_breakdown` is the per-step `load/preprocess/forward/postprocess`
        # split from `local_inference.last_timings` so a regression is
        # diagnosable from the UI alone.
        timings = ttk.Frame(controls)
        timings.pack(anchor=tk.W, fill=tk.X, pady=(8, 0))
        self.image_proc_ms_var = tk.StringVar(value="—")
        self.predict_ms_var = tk.StringVar(value="—")
        self.predict_breakdown_var = tk.StringVar(value="—")
        rows: tuple[tuple[str, tk.StringVar, dict], ...] = (
            ("Image processing", self.image_proc_ms_var, {}),
            ("Prediction", self.predict_ms_var, {}),
            ("Breakdown", self.predict_breakdown_var, {"wraplength": 320, "justify": tk.LEFT}),
        )
        for row, (label, var, value_kwargs) in enumerate(rows):
            ttk.Label(timings, text=f"{label}:", style="Muted.TLabel").grid(
                row=row,
                column=0,
                sticky="nw",
                padx=(0, 8),
            )
            ttk.Label(timings, textvariable=var, style="Accent.TLabel", **value_kwargs).grid(
                row=row, column=1, sticky="w"
            )

        self.status_var = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.status_var, style="Muted.TLabel", wraplength=300, justify=tk.LEFT).pack(
            anchor=tk.W,
            fill=tk.X,
            pady=(8, 0),
        )

        # Right — counts column (fixed width, full vertical). Wide enough to
        # show longer labels without truncating; the capture panel on the
        # left gets the rest.
        right = ttk.LabelFrame(body, text="Image counts", width=440)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        self._counts_wrap = ttk.Frame(right, padding=4)
        self._counts_wrap.pack(fill=tk.BOTH, expand=True)

    # ----- state --------------------------------------------------------------

    def _active_model(self) -> Model | None:
        mid = self.settings.get_active_model_id()
        if mid is None:
            return None
        return self.models_repo.get(mid)

    def _refresh_active_model(self) -> None:
        m = self._active_model()
        if m is None:
            self.active_var.set("(AI Config mode — switch to a local model on the Models tab)")
            self.label_combo["values"] = []
            self.label_var.set("")
            self._render_counts(None, {})
            return
        self.active_var.set(f"{m.name}  ({m.model_mode})")
        # Source of truth is the DB, but the on-disk filenames represent a
        # set of labels the user has actually used. If any label exists on
        # disk that isn't a registered headstamp yet (pre-existing files,
        # community-imported images, manually-copied files), back-fill it
        # so the Headstamps editor, Run-tab slot mapping, and this combo
        # all converge on the same list. add_headstamp is idempotent.
        fs_counts = class_counts(paths.model_images_dir(m.id))
        db_labels = {h.name for h in self.headstamps_repo.list_for_model(m.id)}
        for fs_label in fs_counts:
            if fs_label not in db_labels:
                self.cfg.add_headstamp(fs_label)
        labels = sorted(h.name for h in self.headstamps_repo.list_for_model(m.id))
        self.label_combo["values"] = labels
        if self.label_var.get() and self.label_var.get() not in labels:
            self.label_var.set("")
        counts = dict(fs_counts)
        for label in labels:
            counts.setdefault(label, 0)
        self._render_counts(m, counts)

    def _render_counts(self, m: Model | None, counts: dict[str, int]) -> None:
        for child in list(self._counts_wrap.winfo_children()):
            child.destroy()
        if m is None or not counts:
            ttk.Label(
                self._counts_wrap,
                text="No headstamps yet for this model.",
                style="Subtle.TLabel",
                wraplength=180,
                justify=tk.LEFT,
            ).pack(anchor=tk.NW, pady=(8, 0))
            return
        for label in sorted(counts.keys()):
            _LabelCountCard(
                self._counts_wrap,
                label,
                counts[label],
                on_click=self._on_count_click,
            ).pack(side=tk.TOP, fill=tk.X, pady=(0, 4))

    # ----- feed/capture/save workflow ----------------------------------------

    def _offer_torch_for_predictions(self, sort_label: str | None) -> bool:
        """Offer the torch install before a Feed that would predict a label.

        Returns True if the caller should carry on with the feed now.

        Unlike the Run tab this is an *offer*, not a gate. Capturing and
        labelling images works perfectly well without torch — it's how you
        build a training set in the first place — so declining must not cost
        the user the feed. Only the predicted-label convenience is lost, and
        the training run they eventually start is gated properly anyway.

        Asked at most once per session so a user who said no isn't nagged on
        every case. Cancelling re-enters the feed with the flag already set,
        which is what makes "no thanks" stick.
        """
        if self._torch_prompt_shown:
            return True
        active = self._active_model()
        # No checkpoint → Feed predicts nothing either way, so there's nothing
        # for torch to do. Capturing images for an untrained model is the
        # normal path here, not an error.
        if not classifier.has_local_checkpoint(active):
            return True
        if local_inference.is_installed():
            return True
        self._torch_prompt_shown = True
        resume = lambda: self._feed(sort_label=sort_label)  # noqa: E731
        return torch_gate.ensure_torch(
            self,
            resume,
            reason="Predicting labels needs PyTorch",
            on_cancel=resume,
        )

    def _feed(self, sort_label: str | None = None) -> None:
        if not self._offer_torch_for_predictions(sort_label):
            return
        with self._lock:
            if self._feed_in_flight:
                return
            self._feed_in_flight = True

        self.status_var.set("Feeding…")
        self.classify_label_var.set("")

        # Sort While Training: route the case being dropped to its headstamp's
        # run slot. The label is the user's choice — the dropdown selection when
        # Feed is clicked manually, or the just-saved label when Save / a counts
        # click chains into an auto-feed (`sort_label`). It is NEVER the model's
        # prediction. An unmapped/empty label falls through to the catch-all.
        feed_slot = 0
        if self._sort_while_training_var.get():
            label_for_slot = sort_label if sort_label is not None else self.label_var.get()
            label_for_slot = (label_for_slot or "").strip()
            if label_for_slot:
                feed_slot = self.cfg.slot_for_headstamp(label_for_slot) or 0

        active = self._active_model()
        model_path = active.model_path if (active and active.model_path) else None
        # Trained image size is captured in the model's training_config —
        # imported community models keep the resolution they were trained
        # at, which is often 480 for the legacy-app-default ConvNeXts.
        # Feeding the model the wrong resolution silently degrades
        # predictions, so pass the trained size through to local_inference.
        train_image_size = int(active.training_config.image_size) if active and active.training_config else None

        def _work():
            import time

            broker = getattr(self.app, "broker", None)
            if broker is not None:
                # force_sort_and_move(slot) — xf:<slot>. slot is 0 (catch-all)
                # unless Sort While Training routed the case to a run slot.
                try:
                    if not broker.force_sort_and_move(feed_slot):
                        return {"error": "Feed timeout"}
                except Exception as exc:
                    return {"error": f"Feed failed: {exc}"}
            # Image-processing block: capture + crop + primer mask. All
            # in-memory — the numpy frame never touches disk in this path.
            ip_start = time.perf_counter()
            frame = self.app.capture_frame() if hasattr(self.app, "capture_frame") else None
            if frame is None:
                return {"error": "Camera capture failed"}
            cropped = image_proc.crop_headstamp(frame, self.cfg.image_proc)
            primer = self.cfg.image_proc
            cropped = image_proc.apply_primer_mask(
                cropped,
                primer.get("primer_mode", "none"),
                int(primer.get("primer_radius", 0)),
            )
            ip_ms = (time.perf_counter() - ip_start) * 1000.0

            result: dict[str, Any] = {"cropped": cropped, "image_proc_ms": ip_ms}
            if model_path and Path(model_path).exists():
                from .. import local_inference

                try:
                    pr_start = time.perf_counter()
                    label, conf = local_inference.classify(
                        cropped,
                        model_path,
                        image_size=train_image_size,
                    )
                    result["predict_ms"] = (time.perf_counter() - pr_start) * 1000.0
                    result["predict_device"] = local_inference.current_device_label()
                    # Pass the breakdown straight through so the UI can
                    # show where the time is going.
                    result["predict_breakdown"] = dict(local_inference.last_timings)
                    result["label"] = label
                    result["confidence"] = conf
                except Exception as exc:
                    result["classify_error"] = str(exc)
            return result

        def _ok(result):
            with self._lock:
                self._feed_in_flight = False
            err = result.get("error")
            if err:
                self.status_var.set(err)
                return
            self._last_cropped = result.get("cropped")
            self.image_panel.show_bgr(self._last_cropped)
            ip_ms = result.get("image_proc_ms")
            self.image_proc_ms_var.set(f"{ip_ms:.0f} ms" if ip_ms is not None else "—")
            pr_ms = result.get("predict_ms")
            device = result.get("predict_device")
            breakdown = result.get("predict_breakdown") or {}
            if pr_ms is not None:
                suffix = f" · {device}" if device else ""
                self.predict_ms_var.set(f"{pr_ms:.0f} ms{suffix}")
                # Goes into its OWN var — `status_var` gets clobbered a few
                # lines below by the "Predicted label loaded…" message, so
                # the breakdown needs its own home to stay visible.
                if breakdown:
                    parts = [f"{k}:{v:.0f}" for k, v in breakdown.items()]
                    self.predict_breakdown_var.set("  ".join(parts))
                else:
                    self.predict_breakdown_var.set("—")
            else:
                self.predict_ms_var.set("—")
                self.predict_breakdown_var.set("—")
            label = result.get("label")
            conf = result.get("confidence")
            if label:
                # Pre-populate the combobox with the model's prediction so a
                # confident classification flows straight into Save (or a
                # counts-column click overrides it).
                self.label_var.set(label)
                self.classify_label_var.set(f"{label}  ({conf:.1f}%)")
                self.status_var.set("Predicted label loaded — Save to keep, or pick another.")
            elif "classify_error" in result:
                self.status_var.set(f"Classify failed: {result['classify_error']}")
            else:
                self.status_var.set("Captured. Pick a label or click one in the counts column.")

        def _fail(exc):
            with self._lock:
                self._feed_in_flight = False
            self.status_var.set(f"Feed failed: {exc}")

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)

    def _save_clicked(self) -> None:
        label = (self.label_var.get() or "").strip()
        if not label:
            messagebox.showwarning("Missing label", "Pick or type a classification label.", parent=self)
            return
        # Save-and-feed mirrors the counts-grid click behaviour so typing a
        # brand-new label keeps the rapid-labelling loop going.
        self._save_with_label(label, auto_feed_next=True)

    def _on_count_click(self, label: str) -> None:
        # Save (if we have a cropped frame) then immediately feed the next one.
        if self._last_cropped is None:
            self.status_var.set("Nothing to label yet — click Feed first.")
            return
        self.label_var.set(label)
        self._save_with_label(label, auto_feed_next=True)

    def _save_with_label(self, label: str, *, auto_feed_next: bool) -> None:
        m = self._active_model()
        if m is None:
            messagebox.showinfo("Pick a model first", "Activate a model on the Models tab.", parent=self)
            return
        if self._last_cropped is None:
            messagebox.showinfo("Capture first", "Click Feed before saving.", parent=self)
            return
        try:
            dest = save_training_image(
                self._last_cropped,
                paths.model_images_dir(m.id),
                label,
            )
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        # Persist a brand-new label as a model headstamp so the combobox
        # dropdown and Headstamps editor stay in sync with what's on disk.
        # add_headstamp is a no-op if the name already exists.
        self.cfg.add_headstamp(label)
        self.bus.post("status", f"Saved {dest.name}")
        self.status_var.set(f"Saved as {label}.")
        # Clear the cached cropped image so we don't accidentally double-save.
        self._last_cropped = None
        self.image_panel.show_bgr(None)
        self.classify_label_var.set("")
        self._refresh_active_model()  # bump counts and dropdown
        if auto_feed_next:
            # The case about to drop is the one we just labelled, so sort it to
            # THIS label's slot even though the dropdown has since been cleared.
            self.after(50, lambda lbl=label: self._feed(sort_label=lbl))

    # ----- training -----------------------------------------------------------

    def _on_toggle_sort_while_training(self) -> None:
        self.cfg.set_sort_while_training(self._sort_while_training_var.get())

    def _open_settings(self) -> None:
        m = self._active_model()
        if m is None:
            return

        def _on_saved(_cfg):
            self.models_repo.update(m)

        TrainingConfigDialog(self, m.training_config, on_saved=_on_saved)

    def _start_training(self) -> None:
        m = self._active_model()
        if m is None:
            messagebox.showinfo("Pick a model first", "Activate a model on the Models tab.", parent=self)
            return
        if not is_trainable(m):
            # The tab is hidden for these, so reaching here means the active
            # model changed while the tab was open. Refuse rather than fork
            # someone else's model.
            messagebox.showinfo(
                "Community model",
                f"“{m.name}” was downloaded from the community and is managed "
                "by its publisher, so it can't be trained here.\n\n"
                "To build on it, export it from the Models tab and import the "
                "archive back as your own model.",
                parent=self,
            )
            return
        if self.training_manager.is_running:
            messagebox.showinfo("Training in progress", "Cancel the current run first.", parent=self)
            return
        img_dir = paths.model_images_dir(m.id)
        if not img_dir.exists() or not any(img_dir.iterdir()):
            messagebox.showwarning("No images", "Save at least one training image first.", parent=self)
            return

        # Torch isn't shipped in the base venv (~2 GB; AI-Config-only users
        # don't need it). Prompt for the install once on first Train click,
        # then re-enter this method when it finishes.
        if not torch_gate.ensure_torch(
            self,
            self._start_training,
            reason="Training needs PyTorch",
        ):
            return

        paths.ensure_model_subtree(m.id)
        out_path = paths.model_trained_path(m.id)
        m.training_config.model_name = m.model_mode

        # Open the progress dialog before kicking off so it catches the
        # `training/start` event from the manager.
        self.training_dialog = TrainingProgressDialog(
            self,
            self.bus,
            on_cancel=self.training_manager.cancel,
            on_closed=self._on_training_dialog_closed,
        )

        try:
            self.training_manager.spawn(
                TrainingJob(
                    image_dir=img_dir,
                    output_model=out_path,
                    config=m.training_config,
                )
            )
        except Exception as exc:
            messagebox.showerror("Spawn failed", str(exc), parent=self)
            if self.training_dialog is not None:
                self.training_dialog.destroy()
                self.training_dialog = None
            return
        # Persist any TrainingConfig edits the user may have made via Settings.
        self.models_repo.update(m)
        self._pending_output = (m.id, str(out_path))

    def _on_training_dialog_closed(self, payload: dict[str, Any] | None) -> None:
        self.training_dialog = None
        pending = self._pending_output
        self._pending_output = None
        if pending is not None and payload is not None and "best_val_acc" in (payload or {}):
            mid, path = pending
            m = self.models_repo.get(mid)
            if m is not None and Path(path).exists():
                m.model_path = path
                # datetime.utcnow() is deprecated in favor of an explicit
                # timezone-aware call; the stored string is unchanged (no
                # offset in the format) since it's just a display timestamp.
                m.last_training_date = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
                self.models_repo.update(m)
                self._refresh_active_model()
