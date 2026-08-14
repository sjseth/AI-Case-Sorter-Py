"""Model evaluator — score a trained model against a folder of labelled images.

Pick a folder of ``{label}__{ticks}.jpg`` images, optionally map the folder's
class names onto the model's headstamps, classify every image, and review
accuracy, per-class stats and a filterable result table. Every run writes the
legacy-format interactive HTML report into the model's ``reports/`` directory;
the History tab is that directory, listed newest-first — no DB mirror, the
folder is the history.

⚠️ The report interpolates result rows into a ``<script>`` block
(``ml/eval_report.py``), so it is only safe to open for locally-evaluated,
trusted image folders.

Evaluation runs on a ``QThread`` (not the window's ``run_worker``): the dialog
takes either the main window or a bare ``Config``, so it can't depend on a bus
being there. Progress and the result cross back as queued signals, i.e. on the
main thread. ``notify``/``confirm``/``ask_folder``/``open_url``/``ensure_torch``
are attributes, not direct calls, so a test drives the whole flow with nothing
modal opening.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..data.models import Model
from ..data.repository import CartridgeRepo, HeadstampRepo, SettingsRepo
from ..ml import eval_report, evaluator
from . import formatting
from .dialog_image_preview import ImagePreviewDialog, bgr_to_pixmap

MATCH_LABELS = {"match": "Match", "mismatch": "Mismatch", "unknown": "—"}
# Row color per match status, as palette roles.
MATCH_ROLES = {"match": "success", "mismatch": "error", "unknown": "text_muted"}
_FALLBACK_COLORS = {"success": "#22c55e", "error": "#ef4444", "text_muted": "#9a9a9a"}
MATCH_FILTERS = ("All", "Match", "Mismatch", "No ground truth")
_MATCH_FOR_FILTER = {"Match": "match", "Mismatch": "mismatch", "No ground truth": "unknown"}
RESULT_COLUMNS = ("Filename", "Predicted", "Conf.", "Ground truth", "Match")
SUMMARY_COLUMNS = ("Classification", "# Images", "Avg conf.", "High", "Low", "Mismatched")
HISTORY_COLUMNS = ("Report", "Created")
UNMAPPED = "(Unmapped)"
TORCH_REASON = "Evaluating needs PyTorch"
PREVIEW_SIZE = 240


def mapping_key(model_id: int) -> str:
    """Settings key holding this model's folder-class -> model-class mapping."""
    return f"eval_class_mapping:{model_id}"


class _SortableItem(QTreeWidgetItem):
    """Row that sorts on typed values rather than its displayed text."""

    def __init__(self, texts: Sequence[str], sort_values: Sequence[Any], payload: Any = None) -> None:
        super().__init__(list(texts))
        self.sort_values = list(sort_values)
        self.payload = payload

    def __lt__(self, other: Any) -> bool:
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        try:
            return bool(self.sort_values[column] < other.sort_values[column])
        except (AttributeError, IndexError, TypeError):
            return super().__lt__(other)


class _EvalWorker(QThread):
    """Runs one folder evaluation and writes its report, off the UI thread."""

    progress = Signal(int, int, str)  # index, total, filename ("" total = status-only message)
    done = Signal(object)  # the evaluate_folder dict, plus "report_path" when one was written
    failed = Signal(object)

    def __init__(
        self,
        parent: QWidget | None,
        *,
        folder: str,
        model_path: str,
        image_size: int | None,
        mapping: dict[str, str],
        reports_dir: Path,
        classify_fn: Any,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(parent)
        self._folder = folder
        self._model_path = model_path
        self._image_size = image_size
        self._mapping = mapping
        self._reports_dir = reports_dir
        self._classify_fn = classify_fn
        self._stop_event = stop_event

    def run(self) -> None:
        try:
            out = evaluator.evaluate_folder(
                self._folder,
                model_path=self._model_path,
                image_size=self._image_size,
                mapping=self._mapping,
                classify_fn=self._classify_fn,
                progress=self.progress.emit,
                should_stop=self._stop_event.is_set,
            )
            if out["results"]:
                # Thumbnail generation is I/O heavy; it belongs on this thread.
                self.progress.emit(0, 0, "Building report…")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                report_path = self._reports_dir / f"modelreport_{stamp}.html"
                try:
                    eval_report.generate_report(out["results"], report_path)
                    out["report_path"] = str(report_path)
                except Exception:
                    traceback.print_exc()
            self.done.emit(out)
        except Exception as exc:  # surfaced through `failed`, never raised on this thread
            traceback.print_exc()
            self.failed.emit(exc)


class ModelEvaluatorDialog(QDialog):
    """Folder -> mapping -> run -> results/report/history, for one model."""

    def __init__(self, parent: QWidget | None, source: Any, model: Model) -> None:
        super().__init__(parent)
        self.model = model
        self._source = source
        self.db = source.db
        self.settings = SettingsRepo(self.db)
        self.headstamps = HeadstampRepo(self.db)
        cartridge = CartridgeRepo(self.db).get(model.cartridge_id)
        self._cartridge_name = cartridge.name if cartridge else "—"
        self._status_hook: Callable[[str], None] | None = getattr(source, "set_status", None)

        self.mapping: dict[str, str] = self._load_mapping()
        self.results: list[dict[str, Any]] = []
        self._visible: list[dict[str, Any]] = []
        self._stop_event = threading.Event()
        self._worker: _EvalWorker | None = None

        # Replaceable hooks — a test never opens a native dialog, and the
        # PyTorch gate comes from the window when there is one (increment 7);
        # without it we fall back to the presence probe, refusing rather than
        # letting the worker raise mid-folder.
        self.notify: Callable[[str, str], None] = self._warn
        self.confirm: Callable[[str, str], bool] = self._ask
        self.ask_folder: Callable[[str, str], str | None] = self._ask_folder
        self.open_url: Callable[[str], bool] = self._open_url
        self.ensure_torch: Callable[..., bool] | None = getattr(source, "ensure_torch", None)
        # Injected straight into `evaluate_folder`; None means local inference.
        self.classify_fn: Any = None

        self.setWindowTitle(f"Evaluate model — {model.name}")
        self.resize(940, 640)
        self._build_ui()
        self.refresh_history()

    # ----- persistence --------------------------------------------------------

    def _load_mapping(self) -> dict[str, str]:
        raw = self.settings.get(mapping_key(self.model.id), {})
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        return {}

    def _save_mapping(self) -> None:
        self.settings.set(mapping_key(self.model.id), self.mapping)

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(8)

        title = QLabel(f"{self.model.name}    ({self._cartridge_name} • {self.model.model_mode})", self)
        title.setObjectName("slotTitle")
        column.addWidget(title)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Image folder", self))
        self.folder_edit = QLineEdit(str(paths.model_images_dir(self.model.id)), self)
        folder_row.addWidget(self.folder_edit, 1)
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.clicked.connect(self.browse)
        folder_row.addWidget(self.browse_button)
        column.addLayout(folder_row)

        action_row = QHBoxLayout()
        self.mapping_button = QPushButton("Class mapping…", self)
        self.mapping_button.clicked.connect(self.open_mapper)
        action_row.addWidget(self.mapping_button)
        self.run_button = QPushButton("Run evaluation", self)
        self.run_button.setObjectName("action")
        self.run_button.clicked.connect(self.run_evaluation)
        action_row.addWidget(self.run_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setObjectName("danger")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setVisible(False)
        action_row.addWidget(self.cancel_button)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setVisible(False)
        action_row.addWidget(self.progress_bar)
        action_row.addStretch(1)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("mutedLabel")
        action_row.addWidget(self.status_label)
        column.addLayout(action_row)

        column.addLayout(self._build_stats_row())

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_results_tab(), "Results")
        self.tabs.addTab(self._build_byclass_tab(), "By class")
        self.tabs.addTab(self._build_history_tab(), "History")
        column.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def _build_stats_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.stat_labels: dict[str, QLabel] = {}
        for key, caption in (
            ("total", "Total images"),
            ("total_acc", "Total accuracy"),
            ("known_acc", "Known accuracy"),
            ("avg_conf", "Avg confidence"),
        ):
            # `slotCard` is the palette's card surface; reused rather than a
            # new QSS role so this dialog needs no theme change to look right.
            card = QFrame(self)
            card.setObjectName("slotCard")
            card_column = QVBoxLayout(card)
            card_column.setContentsMargins(10, 8, 10, 8)
            caption_label = QLabel(caption, card)
            caption_label.setObjectName("slotNames")
            card_column.addWidget(caption_label)
            value_label = QLabel("—", card)
            value_label.setObjectName("slotTitle")
            card_column.addWidget(value_label)
            self.stat_labels[key] = value_label
            row.addWidget(card, 1)
        return row

    def _make_table(self, columns: Sequence[str]) -> QTreeWidget:
        tree = QTreeWidget(self)
        tree.setObjectName("modelTable")  # the palette's table role
        tree.setColumnCount(len(columns))
        tree.setHeaderLabels(list(columns))
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(False)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tree.header().setStretchLastSection(True)
        return tree

    def _build_results_tab(self) -> QWidget:
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(4, 8, 4, 4)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter", page))
        self.filter_edit = QLineEdit(page)
        self.filter_edit.setPlaceholderText("Filename contains…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _text: self.render_results())
        filter_row.addWidget(self.filter_edit, 1)
        filter_row.addWidget(QLabel("Match", page))
        self.match_combo = QComboBox(page)
        self.match_combo.addItems(list(MATCH_FILTERS))
        self.match_combo.currentTextChanged.connect(lambda _text: self.render_results())
        filter_row.addWidget(self.match_combo)
        self.count_label = QLabel("", page)
        self.count_label.setObjectName("mutedLabel")
        filter_row.addWidget(self.count_label)
        column.addLayout(filter_row)

        split = QSplitter(Qt.Orientation.Horizontal, page)
        self.result_tree = self._make_table(RESULT_COLUMNS)
        self.result_tree.setSortingEnabled(True)
        # Qt's default sort indicator is column 0 *descending*, and re-enabling
        # sorting after a fill applies it; ascending filename is the evaluator's
        # own order, which is what the table should show before any header click.
        self.result_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.result_tree.currentItemChanged.connect(lambda current, _prev: self._show_preview(current))
        self.result_tree.itemDoubleClicked.connect(lambda item, _col: self._open_preview(item))
        split.addWidget(self.result_tree)

        preview = QWidget(split)
        preview_column = QVBoxLayout(preview)
        preview_column.setContentsMargins(8, 0, 0, 0)
        self.preview_label = QLabel(preview)
        self.preview_label.setObjectName("imagePreview")
        self.preview_label.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_column.addWidget(self.preview_label)
        self.preview_caption = QLabel("", preview)
        self.preview_caption.setObjectName("mutedLabel")
        self.preview_caption.setWordWrap(True)
        preview_column.addWidget(self.preview_caption)
        hint = QLabel("Double-click a row to reclassify or delete the image.", preview)
        hint.setObjectName("dialogHint")
        hint.setWordWrap(True)
        preview_column.addWidget(hint)
        preview_column.addStretch(1)
        split.addWidget(preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        column.addWidget(split, 1)
        return page

    def _build_byclass_tab(self) -> QWidget:
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(4, 8, 4, 4)
        self.summary_tree = self._make_table(SUMMARY_COLUMNS)
        self.summary_tree.setSortingEnabled(True)
        self.summary_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        column.addWidget(self.summary_tree, 1)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(4, 8, 4, 4)
        bar = QHBoxLayout()
        note = QLabel(
            "Saved HTML reports for this model (newest first). Open one to view "
            "the full interactive report in your browser.",
            page,
        )
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        bar.addWidget(note, 1)
        self.open_report_button = QPushButton("Open in browser", page)
        self.open_report_button.setObjectName("action")
        self.open_report_button.clicked.connect(self.open_selected_report)
        bar.addWidget(self.open_report_button)
        column.addLayout(bar)
        self.history_tree = self._make_table(HISTORY_COLUMNS)
        self.history_tree.itemDoubleClicked.connect(lambda _item, _col: self.open_selected_report())
        column.addWidget(self.history_tree, 1)
        return page

    # ----- default hooks ------------------------------------------------------

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _ask_folder(self, title: str, start: str) -> str | None:
        return QFileDialog.getExistingDirectory(self, title, start) or None

    def _open_url(self, path: str) -> bool:
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        if self._status_hook is not None:
            self._status_hook(message)

    # ----- folder / mapping ---------------------------------------------------

    def browse(self) -> None:
        start = self.folder_edit.text().strip() or str(paths.models_dir())
        chosen = self.ask_folder("Select a folder of labelled images", start)
        if chosen:
            self.folder_edit.setText(chosen)

    def model_class_names(self) -> list[str]:
        return [h.name for h in self.headstamps.list_for_model(self.model.id)]

    def build_mapper(self) -> ClassMapperDialog | None:
        """The mapping dialog for the current folder, or None (and a notify)."""
        folder = self.folder_edit.text().strip()
        if not folder or not Path(folder).is_dir():
            self.notify("No folder", "Pick a valid image folder first.")
            return None
        target_classes = evaluator.folder_classes(folder)
        if not target_classes:
            self.notify(
                "No classes",
                "No labelled images found in that folder. Filenames must look like '<class>__<id>.jpg'.",
            )
            return None
        return ClassMapperDialog(self, target_classes, self.model_class_names(), dict(self.mapping))

    def open_mapper(self) -> None:
        dialog = self.build_mapper()
        if dialog is None:
            return
        if dialog.exec():
            self.apply_mapping(dialog.mapping())

    def apply_mapping(self, mapping: dict[str, str]) -> None:
        self.mapping = dict(mapping)
        self._save_mapping()
        self._set_status(f"Saved {len(self.mapping)} class mapping(s).")

    # ----- run ----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def run_evaluation(self) -> None:
        """Preflight, then start the worker."""
        if self.running:
            return
        folder = self.folder_edit.text().strip()
        if not folder or not Path(folder).is_dir():
            self.notify("No folder", "Pick a valid image folder first.")
            return
        if not self.model.model_path or not Path(self.model.model_path).exists():
            self.notify(
                "Not trained",
                "This model has no trained checkpoint to evaluate. Train or import it first.",
            )
            return
        images = evaluator.list_images(folder)
        if not images:
            self.notify("No images", "That folder has no images to evaluate.")
            return
        # Evaluation is inference over a whole folder — offer the install here
        # rather than letting the worker raise once it is under way. The gate
        # re-enters this method on success.
        if not self._ensure_torch():
            return

        self._stop_event = threading.Event()
        self.run_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(images))
        self.progress_bar.setValue(0)
        self._set_status("Starting…")
        image_size = int(self.model.training_config.image_size) if self.model.training_config else None
        worker = _EvalWorker(
            self,
            folder=folder,
            model_path=self.model.model_path,
            image_size=image_size,
            mapping=dict(self.mapping),
            reports_dir=paths.model_reports_dir(self.model.id),
            classify_fn=self.classify_fn,
            stop_event=self._stop_event,
        )
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_error)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._worker = worker
        worker.start()

    def _ensure_torch(self) -> bool:
        if self.ensure_torch is not None:
            return bool(self.ensure_torch(self, self.run_evaluation, reason=TORCH_REASON))
        from ..ml import local_inference

        # `is_installed` is the free find_spec probe — never `is_available`,
        # which imports torch and runs the device benchmark on this thread.
        if local_inference.is_installed():
            return True
        self.notify(
            "PyTorch required",
            "Evaluating a local model needs PyTorch. Install it (pip install .[ml]) and try again.",
        )
        return False

    def cancel(self) -> None:
        self._stop_event.set()
        self.status_label.setText("Stopping…")

    def _on_progress(self, index: int, total: int, name: str) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(index)
            self.status_label.setText(f"Classifying {index}/{total}: {name}")
        else:
            self.status_label.setText(name)  # status-only message (e.g. report build)

    def _on_done(self, out: dict[str, Any]) -> None:
        self.results = out.get("results", [])
        self._finish_run()
        self.render_summary(out.get("summary", {}))
        self.render_results()
        report_path = out.get("report_path")
        self.refresh_history(select_path=report_path)
        base = "Stopped" if out.get("stopped") else "Done"
        message = f"{base} — {len(self.results)} image(s) evaluated."
        if report_path:
            message += "  Report saved → History tab."
        self._set_status(message)

    def _on_error(self, exc: Exception) -> None:
        self._finish_run()
        self.status_label.setText("Evaluation failed.")
        self.notify("Evaluation failed", str(exc))

    def _finish_run(self) -> None:
        self.run_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.progress_bar.setVisible(False)

    def _on_worker_finished(self, worker: _EvalWorker) -> None:
        # Drop the reference before deleteLater() — a later run must not wait
        # on an already-deleted C++ object.
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    # ----- rendering ----------------------------------------------------------

    def render_summary(self, summary: dict[str, Any]) -> None:
        def accuracy(value: Any, matches: Any, denominator: Any) -> str:
            if value is None:
                return "N/A"
            return f"{value:.1f}% ({matches}/{denominator})"

        self.stat_labels["total"].setText(str(summary.get("total", 0)))
        self.stat_labels["total_acc"].setText(
            accuracy(summary.get("total_accuracy"), summary.get("total_matches", 0), summary.get("with_original", 0))
        )
        self.stat_labels["known_acc"].setText(
            accuracy(summary.get("known_accuracy"), summary.get("known_matches", 0), summary.get("with_mapping", 0))
        )
        self.stat_labels["avg_conf"].setText(f"{summary.get('avg_confidence', 0.0):.1f}%")

        self.summary_tree.setSortingEnabled(False)
        self.summary_tree.clear()
        for row in summary.get("per_class", []):
            item = _SortableItem(
                (
                    row["cls"],
                    str(row["count"]),
                    f"{row['avg']:.1f}%",
                    f"{row['high']:.1f}%",
                    f"{row['low']:.1f}%",
                    str(row["mismatches"]),
                ),
                (row["cls"].casefold(), row["count"], row["avg"], row["high"], row["low"], row["mismatches"]),
            )
            for column in range(1, len(SUMMARY_COLUMNS)):
                item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.summary_tree.addTopLevelItem(item)
        self.summary_tree.setSortingEnabled(True)

    def filtered_results(self) -> list[dict[str, Any]]:
        needle = self.filter_edit.text().strip().casefold()
        wanted = _MATCH_FOR_FILTER.get(self.match_combo.currentText())
        rows = []
        for row in self.results:
            if needle and needle not in row["filename"].casefold():
                continue
            if wanted is not None and row["match"] != wanted:
                continue
            rows.append(row)
        return rows

    def render_results(self) -> None:
        self.result_tree.setSortingEnabled(False)
        self.result_tree.clear()
        self._visible = self.filtered_results()
        for row in self._visible:
            item = _SortableItem(
                (
                    row["filename"],
                    row["predicted"],
                    f"{row['confidence']:.1f}%",
                    row["original"] or "—",
                    MATCH_LABELS.get(row["match"], "—"),
                ),
                (
                    row["filename"].casefold(),
                    row["predicted"].casefold(),
                    row["confidence"],
                    row["original"].casefold(),
                    row["match"],
                ),
                payload=row,
            )
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            brush = QBrush(QColor(self.role_color(MATCH_ROLES.get(row["match"], "text_muted"))))
            for column in range(len(RESULT_COLUMNS)):
                item.setForeground(column, brush)
            self.result_tree.addTopLevelItem(item)
        self.result_tree.setSortingEnabled(True)
        self.count_label.setText(f"{len(self._visible)} of {len(self.results)} shown")

    def role_color(self, role: str) -> str:
        """A palette color, from the window when there is one."""
        palette = getattr(self._source, "palette_colors", None) or {}
        return str(palette.get(role) or _FALLBACK_COLORS[role])

    def apply_palette(self) -> None:
        """Re-read colors after a theme switch — the row colors are baked in.

        Same contract as the serial monitor's ``apply_palette``: the stylesheet
        can't reach a QTreeWidgetItem's brush, so the host asks for this by name.
        """
        self.render_results()

    def result_rows_shown(self) -> list[dict[str, Any]]:
        """The rows in the table, in the order the table shows them."""
        items = [self.result_tree.topLevelItem(i) for i in range(self.result_tree.topLevelItemCount())]
        return [item.payload for item in items if isinstance(item, _SortableItem) and item.payload is not None]

    # ----- preview -------------------------------------------------------------

    def _show_preview(self, item: Any) -> None:
        row = item.payload if isinstance(item, _SortableItem) else None
        if row is None:
            return
        caption = f"{row['filename']}\nPredicted: {row['predicted']} ({row['confidence']:.1f}%)"
        if row["original"]:
            caption += f"\nGround truth: {row['original']}"
        self.preview_caption.setText(caption)
        frame = None
        try:
            import cv2

            frame = cv2.imread(row["filepath"])
        except Exception:
            frame = None
        self.preview_label.setPixmap(bgr_to_pixmap(frame, PREVIEW_SIZE))

    def _open_preview(self, item: Any) -> None:
        row = item.payload if isinstance(item, _SortableItem) else None
        if row is None:
            return
        shown = self.result_rows_shown()
        image_paths = [r["filepath"] for r in shown]
        try:
            index = image_paths.index(row["filepath"])
        except ValueError:
            index = 0
        dialog = ImagePreviewDialog(
            self,
            image_paths,
            index,
            self.model_class_names(),
            on_changed=self.apply_image_change,
        )
        dialog.exec()

    def apply_image_change(self, change: dict[str, Any]) -> None:
        """Fold a preview reclassify/delete back into the results and re-score.

        The file already moved on disk; ground truth follows the filename, so
        the row is re-derived and the stats re-summarized without a re-run.
        """
        updated = self.updated_results(self.results, change, self.mapping)
        if updated is None:
            return
        self.results = updated
        self.render_summary(evaluator.summarize(self.results))
        self.render_results()

    @staticmethod
    def updated_results(
        results: list[dict[str, Any]],
        change: dict[str, Any],
        mapping: dict[str, str],
    ) -> list[dict[str, Any]] | None:
        """Pure result-list update for a previewer change. None = no-op."""
        old_path = change.get("old_path")
        action = change.get("action")
        if action == "deleted":
            return [r for r in results if r["filepath"] != old_path]
        if action != "reclassified":
            return None
        new_path = change.get("new_path")
        if not isinstance(new_path, str):
            raise ValueError(f"'reclassified' change missing a new_path: {change!r}")
        raw = change.get("new_headstamp") or ""
        original, was_mapped = evaluator.map_classification(raw, mapping)
        out = []
        for row in results:
            if row["filepath"] == old_path:
                row = {
                    **row,
                    "filepath": new_path,
                    "filename": Path(new_path).name,
                    "raw_original": raw,
                    "original": original,
                    "has_mapping": was_mapped,
                    "match": evaluator.match_status(row["predicted"], original),
                }
            out.append(row)
        return out

    # ----- history --------------------------------------------------------------

    def report_paths(self) -> list[Path]:
        """Saved reports for this model, newest first — the folder is the history."""
        report_dir = paths.model_reports_dir(self.model.id)
        if not report_dir.exists():
            return []
        return sorted(
            (p for p in report_dir.glob("*.html") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def refresh_history(self, select_path: str | None = None) -> None:
        self.history_tree.clear()
        for path in self.report_paths():
            mtime = path.stat().st_mtime
            # Display in the OS's regional format; sort on the raw mtime — a
            # locale-formatted string doesn't sort chronologically in general.
            created = formatting.format_datetime(datetime.fromtimestamp(mtime))
            item = _SortableItem((path.name, created), (path.name.casefold(), mtime), payload=path)
            self.history_tree.addTopLevelItem(item)
            if select_path is not None and str(path) == str(select_path):
                self.history_tree.setCurrentItem(item)

    def selected_report(self) -> Path | None:
        item = self.history_tree.currentItem()
        if not isinstance(item, _SortableItem) or item.payload is None:
            item = self.history_tree.topLevelItem(0)  # default to the newest
        if isinstance(item, _SortableItem) and isinstance(item.payload, Path):
            return item.payload
        return None

    def open_selected_report(self) -> None:
        path = self.selected_report()
        if path is None:
            self.notify("No report", "No reports to open yet.")
            return
        if not self.open_url(str(path.resolve())):
            self.notify("Open failed", str(path))

    # ----- lifecycle -------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        self._stop_event.set()
        worker = self._worker
        if worker is not None:
            worker.wait(2000)
        super().closeEvent(event)


class ClassMapperDialog(QDialog):
    """Map the folder's ground-truth class names onto the model's headstamps."""

    def __init__(
        self,
        parent: QWidget | None,
        target_classes: Sequence[str],
        model_classes: Sequence[str],
        initial: dict[str, str],
    ) -> None:
        super().__init__(parent)
        self._model_classes = list(model_classes)
        self.combos: dict[str, QComboBox] = {}
        self.notify: Callable[[str, str], None] = self._inform
        self.confirm: Callable[[str, str], bool] = self._ask

        self.setWindowTitle("Map folder classes to model")
        self.resize(560, 420)
        column = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel("Folder class  →  model class", self))
        header.addStretch(1)
        self.auto_button = QPushButton("Auto-map all", self)
        self.auto_button.clicked.connect(self.auto_map)
        header.addWidget(self.auto_button)
        column.addLayout(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget(scroll)
        grid = QGridLayout(host)
        grid.setContentsMargins(4, 4, 4, 4)
        values = [UNMAPPED, *self._model_classes]
        for row, target in enumerate(target_classes):
            grid.addWidget(QLabel(target, host), row, 0)
            arrow = QLabel("→", host)
            arrow.setObjectName("mutedLabel")
            grid.addWidget(arrow, row, 1)
            combo = QComboBox(host)
            combo.addItems(values)
            # Saved mapping wins over a suggestion; a suggestion over Unmapped.
            saved = initial.get(target)
            if saved in self._model_classes:
                combo.setCurrentText(saved)
            else:
                suggestion = evaluator.suggest_mapping(target, self._model_classes)
                combo.setCurrentText(suggestion if suggestion in self._model_classes else UNMAPPED)
            grid.addWidget(combo, row, 2)
            self.combos[target] = combo
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        scroll.setWidget(host)
        column.addWidget(scroll, 1)

        buttons = QDialogButtonBox(self)
        save = buttons.addButton("Save mapping", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("action")
        save.clicked.connect(self.save)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def _inform(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def auto_map(self) -> None:
        mapped = 0
        for target, combo in self.combos.items():
            suggestion = evaluator.suggest_mapping(target, self._model_classes)
            if suggestion in self._model_classes:
                combo.setCurrentText(suggestion)
                mapped += 1
        self.notify("Auto-map", f"Auto-mapped {mapped} of {len(self.combos)} classes.")

    def mapping(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for target, combo in self.combos.items():
            value = combo.currentText()
            if value and value != UNMAPPED:
                out[target] = value
        return out

    def save(self) -> None:
        unmapped = len(self.combos) - len(self.mapping())
        if unmapped > 0 and not self.confirm(
            "Unmapped classes",
            f"{unmapped} class(es) are unmapped and won't count toward known accuracy.\n\nSave anyway?",
        ):
            return
        self.accept()


def build_model_evaluator(parent: QWidget | None, source: Any, model: Model) -> ModelEvaluatorDialog:
    """Factory mirroring the other dialogs' ``build_*`` entry points."""
    return ModelEvaluatorDialog(parent, source, model)
