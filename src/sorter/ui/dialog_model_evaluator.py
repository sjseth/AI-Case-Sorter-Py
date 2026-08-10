"""Model evaluator — score a trained model against a folder of labelled images.

Recreates the legacy app's batch-image-tester feature using in-process
inference (``sorter.evaluator``). Launched from the Models tab's per-row
"Evaluate" button.

Two dialogs:

* :class:`ModelEvaluatorDialog` — pick a folder, optionally set up a
  folder-class -> model-class mapping, run inference, and review accuracy /
  per-class stats / a filterable result table with image preview.
* :class:`ClassMapperDialog` — map the folder's ground-truth class names onto
  the model's headstamps. Mappings persist per-model for reuse.
"""

from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import eval_report, evaluator, paths
from ..db import Database
from ..models import Model
from ..repository import CartridgeRepo, HeadstampRepo, SettingsRepo
from . import torch_gate
from .dialog_image_preview import ImagePreviewDialog
from .theme import PALETTE
from .widgets import ImagePanel, ScrollableFrame

_MATCH_LABELS = {"match": "Match", "mismatch": "Mismatch", "unknown": "—"}
_MATCH_FILTERS = ("All", "Match", "Mismatch", "No ground truth")


def _mapping_key(model_id: int) -> str:
    return f"eval_class_mapping:{model_id}"


class ModelEvaluatorDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        db: Database,
        model: Model,
        *,
        app: Any,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.model = model
        self.app = app
        self.settings = SettingsRepo(db)
        self.headstamps_repo = HeadstampRepo(db)
        cart = CartridgeRepo(db).get(model.cartridge_id)
        self._cart_name = cart.name if cart else "—"

        self.title(f"Evaluate Model — {model.name}")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.minsize(900, 600)
        self.configure(bg=PALETTE["bg_window"])

        self._mapping: dict[str, str] = self._load_mapping()
        self._results: list[dict[str, Any]] = []
        self._running = False
        self._stop_event = threading.Event()
        self._progress_topic = f"eval/progress/{id(self)}"
        # Set by `_run` before the worker thread starts; `_do_eval` only ever
        # runs after that, but the checker can't see across the
        # run_worker(...) call, so it's declared optional here.
        self._run_folder: str | None = None

        self._build_ui()
        self._refresh_history()
        self.app.bus.subscribe(self._progress_topic, self._on_progress)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())

    # ----- persistence --------------------------------------------------------

    def _load_mapping(self) -> dict[str, str]:
        raw = self.settings.get(_mapping_key(self.model.id), {})
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        return {}

    def _save_mapping(self) -> None:
        self.settings.set(_mapping_key(self.model.id), self._mapping)

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self, style="Window.TFrame", padding=(10, 10, 10, 0))
        top.pack(fill=tk.X)

        ttk.Label(
            top,
            text=f"{self.model.name}    ({self._cart_name} • {self.model.model_mode})",
            style="Title.TLabel",
        ).pack(anchor=tk.W)

        folder_row = ttk.Frame(top, style="Window.TFrame")
        folder_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(folder_row, text="Image folder", style="Subtitle.TLabel", width=12, anchor=tk.W).pack(side=tk.LEFT)
        self._folder_var = tk.StringVar(value=str(paths.model_images_dir(self.model.id)))
        ttk.Entry(folder_row, textvariable=self._folder_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Button(folder_row, text="Browse…", command=self._browse).pack(side=tk.LEFT)

        action_row = ttk.Frame(top, style="Window.TFrame")
        action_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(action_row, text="Class mapping…", command=self._open_mapper).pack(side=tk.LEFT)
        self._run_btn = ttk.Button(
            action_row,
            text="Run evaluation",
            command=self._run,
            style="Accent.TButton",
        )
        self._run_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._stop_btn = ttk.Button(
            action_row,
            text="Stop",
            command=self._stop,
            style="Danger.TButton",
        )  # packed only while running
        self._progress = ttk.Progressbar(action_row, mode="determinate", length=180)
        self._status_var = tk.StringVar(value="")
        ttk.Label(action_row, textvariable=self._status_var, style="Muted.TLabel").pack(side=tk.RIGHT)

        # ---- stats row ------------------------------------------------------
        self._stats = ttk.Frame(self, style="Window.TFrame", padding=(10, 8, 10, 0))
        self._stats.pack(fill=tk.X)
        self._stat_vars = {key: tk.StringVar(value="—") for key in ("total", "total_acc", "known_acc", "avg_conf")}
        for label, key in (
            ("Total images", "total"),
            ("Total accuracy", "total_acc"),
            ("Known accuracy", "known_acc"),
            ("Avg confidence", "avg_conf"),
        ):
            self._stat_card(self._stats, label, self._stat_vars[key])

        # ---- notebook: Results + By class + History -------------------------
        nb = ttk.Notebook(self)
        self._nb = nb
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        results_tab = ttk.Frame(nb)
        nb.add(results_tab, text="Results")
        self._build_results_tab(results_tab)

        byclass_tab = ttk.Frame(nb)
        nb.add(byclass_tab, text="By class")
        self._build_byclass_tab(byclass_tab)

        history_tab = ttk.Frame(nb)
        nb.add(history_tab, text="History")
        self._build_history_tab(history_tab)

        bottom = ttk.Frame(self, style="Window.TFrame")
        bottom.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(bottom, text="Close", command=self._close).pack(side=tk.RIGHT)

    def _stat_card(self, parent: tk.Misc, label: str, var: tk.StringVar) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=10)
        card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Label(card, text=label, style="CardSubtle.TLabel").pack(anchor=tk.W)
        ttk.Label(card, textvariable=var, style="CardSelTitle.TLabel").pack(anchor=tk.W)

    def _build_results_tab(self, parent: tk.Misc) -> None:
        filt = ttk.Frame(parent)
        filt.pack(fill=tk.X, pady=(8, 4), padx=2)
        ttk.Label(filt, text="Filter", style="Muted.TLabel").pack(side=tk.LEFT)
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._render_results())
        ttk.Entry(filt, textvariable=self._search_var, width=22).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(filt, text="Match", style="Muted.TLabel").pack(side=tk.LEFT)
        self._match_var = tk.StringVar(value=_MATCH_FILTERS[0])
        match_combo = ttk.Combobox(
            filt,
            state="readonly",
            width=18,
            textvariable=self._match_var,
            values=list(_MATCH_FILTERS),
        )
        match_combo.pack(side=tk.LEFT, padx=(6, 0))
        match_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_results())
        self._count_var = tk.StringVar(value="")
        ttk.Label(filt, textvariable=self._count_var, style="Subtle.TLabel").pack(side=tk.RIGHT)

        split = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True)

        table_wrap = ttk.Frame(split)
        split.add(table_wrap, weight=3)
        cols = ("filename", "predicted", "confidence", "original", "match")
        scroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL)
        self._tree = ttk.Treeview(
            table_wrap,
            columns=cols,
            show="headings",
            selectmode="browse",
            yscrollcommand=scroll.set,
        )
        scroll.config(command=self._tree.yview)
        for col, text, width, anchor in (
            ("filename", "Filename", 200, tk.W),
            ("predicted", "Predicted", 110, tk.W),
            ("confidence", "Conf.", 70, tk.E),
            ("original", "Ground truth", 120, tk.W),
            ("match", "Match", 80, tk.W),
        ):
            self._tree.heading(col, text=text, command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=width, anchor=anchor, stretch=(col in ("filename", "original")))
        self._tree.tag_configure("match", foreground=PALETTE["success"])
        self._tree.tag_configure("mismatch", foreground=PALETTE["error"])
        self._tree.tag_configure("unknown", foreground=PALETTE["text_muted"])
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._on_row_select)
        # Double-clicking a row opens the shared previewer (reassign / delete),
        # mirroring the model image manager's preview popup.
        self._tree.bind("<Double-1>", self._open_preview)

        preview = ttk.Frame(split)
        split.add(preview, weight=1)
        ttk.Label(preview, text="Preview", style="Muted.TLabel").pack(anchor=tk.W, padx=6, pady=(0, 2))
        self._preview = ImagePanel(preview, width=240, height=240)
        self._preview.pack(padx=6, pady=2)
        self._preview_label = tk.StringVar(value="")
        ttk.Label(
            preview, textvariable=self._preview_label, style="Subtle.TLabel", wraplength=240, justify=tk.LEFT
        ).pack(anchor=tk.W, padx=6)
        ttk.Label(
            preview,
            text="Double-click a row to reassign or delete.",
            style="Subtle.TLabel",
            wraplength=240,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=6, pady=(6, 0))

        self._sort_state: tuple[str, bool] | None = None
        self._row_by_iid: dict[str, dict[str, Any]] = {}

    def _build_byclass_tab(self, parent: tk.Misc) -> None:
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, pady=8, padx=2)
        cols = ("cls", "count", "avg", "high", "low", "mismatch")
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL)
        self._summary_tree = ttk.Treeview(
            wrap,
            columns=cols,
            show="headings",
            selectmode="browse",
            yscrollcommand=scroll.set,
        )
        scroll.config(command=self._summary_tree.yview)
        for col, text, width, anchor in (
            ("cls", "Classification", 180, tk.W),
            ("count", "# Images", 90, tk.E),
            ("avg", "Avg conf.", 90, tk.E),
            ("high", "High", 80, tk.E),
            ("low", "Low", 80, tk.E),
            ("mismatch", "Mismatched", 100, tk.E),
        ):
            self._summary_tree.heading(col, text=text)
            self._summary_tree.column(col, width=width, anchor=anchor, stretch=(col == "cls"))
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._summary_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _build_history_tab(self, parent: tk.Misc) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(8, 4), padx=2)
        ttk.Label(
            bar,
            text="Saved HTML reports for this model (newest first). "
            "Open one to view the full interactive report in your browser.",
            style="Muted.TLabel",
            wraplength=600,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT)
        ttk.Button(bar, text="Open in browser", command=self._open_selected_report, style="Accent.TButton").pack(
            side=tk.RIGHT
        )

        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True, padx=2)
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL)
        self._history_tree = ttk.Treeview(
            wrap,
            columns=("report", "created"),
            show="headings",
            selectmode="browse",
            yscrollcommand=scroll.set,
        )
        scroll.config(command=self._history_tree.yview)
        self._history_tree.heading("report", text="Report")
        self._history_tree.heading("created", text="Created")
        self._history_tree.column("report", width=320, stretch=True, anchor=tk.W)
        self._history_tree.column("created", width=170, stretch=False, anchor=tk.W)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._history_tree.bind("<Double-1>", lambda _e: self._open_selected_report())
        self._report_by_iid: dict[str, Path] = {}

    def _refresh_history(self, select_path: str | None = None) -> None:
        self._history_tree.delete(*self._history_tree.get_children())
        self._report_by_iid = {}
        report_dir = paths.model_reports_dir(self.model.id)
        reports = (
            sorted(
                (p for p in report_dir.glob("*.html") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if report_dir.exists()
            else []
        )
        for idx, p in enumerate(reports):
            iid = str(idx)
            created = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            self._history_tree.insert("", "end", iid=iid, values=(p.name, created))
            self._report_by_iid[iid] = p
        if select_path is not None:
            for iid, p in self._report_by_iid.items():
                if str(p) == str(select_path):
                    self._history_tree.selection_set(iid)
                    self._history_tree.see(iid)
                    break

    def _open_selected_report(self) -> None:
        sel = self._history_tree.selection()
        path = self._report_by_iid.get(sel[0]) if sel else None
        if path is None:  # default to the newest
            children = self._history_tree.get_children()
            if children:
                path = self._report_by_iid.get(children[0])
        if path is None:
            messagebox.showinfo("No report", "No reports to open yet.", parent=self)
            return
        try:
            # as_uri() + webbrowser are cross-platform (Windows/macOS/Linux).
            webbrowser.open(Path(path).resolve().as_uri())
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc), parent=self)

    # ----- folder / mapping ---------------------------------------------------

    def _browse(self) -> None:
        start = self._folder_var.get() or str(paths.models_dir())
        chosen = filedialog.askdirectory(
            title="Select a folder of labelled images",
            initialdir=start,
            parent=self,
        )
        if chosen:
            self._folder_var.set(chosen)

    def _open_mapper(self) -> None:
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("No folder", "Pick a valid image folder first.", parent=self)
            return
        target_classes = evaluator.folder_classes(folder)
        if not target_classes:
            messagebox.showinfo(
                "No classes",
                "No labelled images found in that folder. Filenames must look like '<class>__<id>.jpg'.",
                parent=self,
            )
            return
        model_classes = [h.name for h in self.headstamps_repo.list_for_model(self.model.id)]

        def _on_ok(mapping: dict[str, str]) -> None:
            self._mapping = mapping
            self._save_mapping()
            self.app.set_status(f"Saved {len(mapping)} class mapping(s).")

        ClassMapperDialog(
            self,
            target_classes,
            model_classes,
            dict(self._mapping),
            on_ok=_on_ok,
        )

    # ----- run ----------------------------------------------------------------

    def _run(self) -> None:
        if self._running:
            return
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("No folder", "Pick a valid image folder first.", parent=self)
            return
        if not self.model.model_path or not Path(self.model.model_path).exists():
            messagebox.showerror(
                "Not trained",
                "This model has no trained checkpoint to evaluate. Train or import it first.",
                parent=self,
            )
            return
        if not evaluator.list_images(folder):
            messagebox.showinfo("No images", "That folder has no images to evaluate.", parent=self)
            return
        # Evaluation is inference over a whole folder — offer the install here
        # rather than letting the worker raise once it's under way. On success
        # this method re-runs and the evaluation starts.
        if not torch_gate.ensure_torch(
            self,
            self._run,
            reason="Evaluating needs PyTorch",
        ):
            return

        # Snapshot everything the worker needs on the UI thread — the worker
        # must not touch Tk widgets/vars from its background thread.
        self._run_folder = folder
        self._running = True
        self._stop_event.clear()
        self._run_btn.configure(state=tk.DISABLED)
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._progress.pack(side=tk.LEFT, padx=(12, 0))
        self._progress.configure(value=0, maximum=max(1, len(evaluator.list_images(folder))))
        self._status_var.set("Starting…")
        self.app.run_worker(self._do_eval, on_done=self._on_done, on_error=self._on_error)

    def _do_eval(self) -> dict[str, Any]:
        from .. import local_inference

        if not local_inference.is_available():
            raise RuntimeError(
                "PyTorch is not installed — it's required to evaluate a local model (pip install .[ml])."
            )
        # `_run` always sets this, and already verified the checkpoint
        # exists, before starting the worker that calls here.
        if self._run_folder is None:
            raise RuntimeError("Evaluation worker started without a folder to evaluate.")
        if not self.model.model_path:
            raise RuntimeError("Evaluation worker started without a trained checkpoint.")
        image_size = int(self.model.training_config.image_size) if self.model.training_config else None
        out = evaluator.evaluate_folder(
            self._run_folder,
            model_path=self.model.model_path,
            image_size=image_size,
            mapping=dict(self._mapping),
            progress=lambda i, n, name: self.app.bus.post(self._progress_topic, (i, n, name)),
            should_stop=self._stop_event.is_set,
        )
        # Produce the self-contained HTML report (same format as the legacy
        # app). Runs on the worker thread — thumbnail generation is I/O heavy.
        if out["results"]:
            self.app.bus.post(self._progress_topic, (0, 0, "Building report…"))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = paths.model_reports_dir(self.model.id) / f"modelreport_{stamp}.html"
            try:
                eval_report.generate_report(out["results"], report_path)
                out["report_path"] = str(report_path)
            except Exception:
                import traceback

                traceback.print_exc()
        return out

    def _on_progress(self, payload: tuple[int, int, str]) -> None:
        i, n, name = payload
        try:
            if n > 0:
                self._progress.configure(maximum=n, value=i)
                self._status_var.set(f"Classifying {i}/{n}: {name}")
            else:
                self._status_var.set(name)  # status-only message (e.g. report build)
        except tk.TclError:
            return

    def _on_done(self, out: dict[str, Any]) -> None:
        self._results = out.get("results", [])
        self._finish_run()
        self._render_summary(out.get("summary", {}))
        self._render_results()
        report_path = out.get("report_path")
        self._refresh_history(select_path=report_path)
        base = "Stopped" if out.get("stopped") else "Done"
        msg = f"{base} — {len(self._results)} image(s) evaluated."
        if report_path:
            msg += "  Report saved → History tab."
        self._status_var.set(msg)

    def _on_error(self, exc: Exception) -> None:
        self._finish_run()
        self._status_var.set("Evaluation failed.")
        messagebox.showerror("Evaluation failed", str(exc), parent=self)

    def _finish_run(self) -> None:
        self._running = False
        try:
            self._run_btn.configure(state=tk.NORMAL)
            self._stop_btn.pack_forget()
            self._progress.pack_forget()
        except tk.TclError:
            pass

    def _stop(self) -> None:
        self._stop_event.set()
        self._status_var.set("Stopping…")

    # ----- rendering ----------------------------------------------------------

    def _render_summary(self, summary: dict[str, Any]) -> None:
        def _acc(value, matches, denom):
            if value is None:
                return "N/A"
            return f"{value:.1f}% ({matches}/{denom})"

        self._stat_vars["total"].set(str(summary.get("total", 0)))
        self._stat_vars["total_acc"].set(
            _acc(summary.get("total_accuracy"), summary.get("total_matches", 0), summary.get("with_original", 0))
        )
        self._stat_vars["known_acc"].set(
            _acc(summary.get("known_accuracy"), summary.get("known_matches", 0), summary.get("with_mapping", 0))
        )
        self._stat_vars["avg_conf"].set(f"{summary.get('avg_confidence', 0.0):.1f}%")

        self._summary_tree.delete(*self._summary_tree.get_children())
        for row in summary.get("per_class", []):
            self._summary_tree.insert(
                "",
                "end",
                values=(
                    row["cls"],
                    row["count"],
                    f"{row['avg']:.1f}%",
                    f"{row['high']:.1f}%",
                    f"{row['low']:.1f}%",
                    row["mismatches"],
                ),
            )

    def _filtered_results(self) -> list[dict[str, Any]]:
        needle = self._search_var.get().strip().casefold()
        match_filter = self._match_var.get()
        want = {
            "Match": "match",
            "Mismatch": "mismatch",
            "No ground truth": "unknown",
        }.get(match_filter)
        rows = []
        for r in self._results:
            if needle and needle not in r["filename"].casefold():
                continue
            if want is not None and r["match"] != want:
                continue
            rows.append(r)
        if self._sort_state is not None:
            col, descending = self._sort_state
            key = {
                "filename": lambda r: r["filename"].casefold(),
                "predicted": lambda r: r["predicted"].casefold(),
                "confidence": lambda r: r["confidence"],
                "original": lambda r: r["original"].casefold(),
                "match": lambda r: r["match"],
            }.get(col, lambda r: r["filename"])
            rows.sort(key=key, reverse=descending)
        return rows

    def _render_results(self) -> None:
        if not hasattr(self, "_tree"):
            return
        self._tree.delete(*self._tree.get_children())
        self._row_by_iid = {}
        rows = self._filtered_results()
        for idx, r in enumerate(rows):
            iid = str(idx)
            self._tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    r["filename"],
                    r["predicted"],
                    f"{r['confidence']:.1f}%",
                    r["original"] or "—",
                    _MATCH_LABELS.get(r["match"], "—"),
                ),
                tags=(r["match"],),
            )
            self._row_by_iid[iid] = r
        self._count_var.set(f"{len(rows)} of {len(self._results)} shown")

    def _sort_by(self, column: str) -> None:
        if self._sort_state and self._sort_state[0] == column:
            self._sort_state = (column, not self._sort_state[1])
        else:
            self._sort_state = (column, False)
        self._render_results()

    def _on_row_select(self, _event: tk.Event) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        row = self._row_by_iid.get(sel[0])
        if not row:
            return
        self._preview_label.set(
            f"{row['filename']}\nPredicted: {row['predicted']} ({row['confidence']:.1f}%)"
            + (f"\nGround truth: {row['original']}" if row["original"] else "")
        )
        try:
            import cv2

            img = cv2.imread(row["filepath"])
            self._preview.show_bgr(img)
        except Exception:
            self._preview.show_bgr(None)

    # ----- hybrid preview (shared with the model image manager) ----------------

    def _open_preview(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        row = self._row_by_iid.get(iid) if iid else None
        if not row:
            return
        headstamps = [h.name for h in self.headstamps_repo.list_for_model(self.model.id)]
        ImagePreviewDialog(
            self,
            row["filepath"],
            headstamps,
            row.get("raw_original") or None,
            on_changed=self._apply_image_change,
        )

    def _apply_image_change(self, change: dict[str, Any]) -> None:
        """Reflect a delete/reassign done in the previewer back into the table.

        The previewer renamed/removed the file on disk; here we update the
        in-memory results (ground truth follows the filename prefix) and
        re-score so the stats and per-class tabs stay in sync — no re-run needed.
        """
        updated = self._apply_change(self._results, change, self._mapping)
        if updated is None:
            return
        self._results = updated
        self._render_summary(evaluator.summarize(self._results))
        self._render_results()

    @staticmethod
    def _apply_change(
        results: list[dict[str, Any]],
        change: dict[str, Any],
        mapping: dict[str, str],
    ) -> list[dict[str, Any]] | None:
        """Pure result-list update for a previewer change. None = no-op.

        Reclassify re-derives ground truth from the new headstamp prefix and
        re-scores the row against the (unchanged) prediction; delete drops it.
        """
        old_path = change.get("old_path")
        action = change.get("action")
        if action == "deleted":
            return [r for r in results if r["filepath"] != old_path]
        if action == "reclassified":
            new_path = change.get("new_path")
            # The preview dialog only ever emits "reclassified" with a
            # `new_path` string (see dialog_image_preview._reclassify) — the
            # dict is typed as `dict[str, Any]` because it also carries
            # "deleted" changes, so the checker can't see that.
            if not isinstance(new_path, str):
                raise ValueError(f"'reclassified' change missing a new_path: {change!r}")
            raw = change.get("new_headstamp") or ""
            original, was_mapped = evaluator.map_classification(raw, mapping)
            out = []
            for r in results:
                if r["filepath"] == old_path:
                    r = {
                        **r,
                        "filepath": new_path,
                        "filename": Path(new_path).name,
                        "raw_original": raw,
                        "original": original,
                        "has_mapping": was_mapped,
                        "match": evaluator.match_status(r["predicted"], original),
                    }
                out.append(r)
            return out
        return None

    # ----- lifecycle ----------------------------------------------------------

    def _close(self) -> None:
        self._stop_event.set()
        try:
            self.app.bus.unsubscribe(self._progress_topic, self._on_progress)
        except Exception:
            pass
        self.destroy()


class ClassMapperDialog(tk.Toplevel):
    """Map folder ground-truth classes onto the model's headstamp classes."""

    UNMAPPED = "(Unmapped)"

    def __init__(
        self,
        parent: tk.Misc,
        target_classes: list[str],
        model_classes: list[str],
        initial: dict[str, str],
        *,
        on_ok: Callable[[dict[str, str]], None],
    ) -> None:
        super().__init__(parent)
        self.title("Map folder classes to model")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.minsize(520, 360)
        self.configure(bg=PALETTE["bg_window"])
        self._model_classes = model_classes
        self._on_ok = on_ok
        self._combos: dict[str, ttk.Combobox] = {}

        header = ttk.Frame(self, style="Window.TFrame", padding=(12, 12, 12, 4))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Folder class  →  model class", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(header, text="Auto-map all", command=self._auto_map).pack(side=tk.RIGHT)

        body = ScrollableFrame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=12)
        grid = body.body
        values = [self.UNMAPPED] + model_classes
        for row, target in enumerate(target_classes):
            ttk.Label(grid, text=target, anchor=tk.W).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Label(grid, text="→", style="Muted.TLabel").grid(row=row, column=1, padx=4)
            # Use the combobox's own value (set/get) rather than a per-row
            # StringVar — a loop-local Variable would be garbage-collected and
            # silently blank the widget.
            combo = ttk.Combobox(grid, state="readonly", width=28, values=values)
            # Preselect saved mapping, else a suggestion, else Unmapped.
            if target in initial and initial[target] in model_classes:
                combo.set(initial[target])
            else:
                suggestion = evaluator.suggest_mapping(target, model_classes)
                combo.set(suggestion if suggestion in model_classes else self.UNMAPPED)
            combo.grid(row=row, column=2, sticky="w", pady=3)
            self._combos[target] = combo
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(2, weight=1)

        btns = ttk.Frame(self, style="Window.TFrame", padding=12)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save mapping", command=self._save, style="Accent.TButton").pack(
            side=tk.RIGHT, padx=(0, 8)
        )

        self.bind("<Escape>", lambda _e: self.destroy())

    def _auto_map(self) -> None:
        mapped = 0
        for target, combo in self._combos.items():
            suggestion = evaluator.suggest_mapping(target, self._model_classes)
            if suggestion in self._model_classes:
                combo.set(suggestion)
                mapped += 1
        messagebox.showinfo(
            "Auto-map",
            f"Auto-mapped {mapped} of {len(self._combos)} classes.",
            parent=self,
        )

    def _current_mapping(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for target, combo in self._combos.items():
            value = combo.get()
            if value and value != self.UNMAPPED:
                out[target] = value
        return out

    def _save(self) -> None:
        mapping = self._current_mapping()
        unmapped = len(self._combos) - len(mapping)
        if unmapped > 0 and not messagebox.askyesno(
            "Unmapped classes",
            f"{unmapped} class(es) are unmapped and won't count toward known accuracy.\n\nSave anyway?",
            parent=self,
        ):
            return
        self._on_ok(mapping)
        self.destroy()
