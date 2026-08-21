"""Train activity — the feed→capture→classify→label→save loop.

The pipeline, step for step: the board drops a case (``force_sort_and_move``),
the camera grabs a frame, ``image_proc`` crops it to the headstamp and applies
the primer mask, and — only when there is a checkpoint *and* torch is present
— the model predicts a label to pre-fill the field with. Saving writes the
cropped frame through ``training.dataset.save_training_image``, which owns the
WinForms-compatible filename convention.

Three rules, each easy to get wrong:

* **Torch is offered here, never demanded** (CLAUDE.md §5). Capturing and
  labelling images is exactly the workflow that doesn't need it — it's how a
  training set gets built in the first place — so declining costs only the
  predicted-label convenience, and the decline is remembered for the session.
  Training itself is gated properly, at the Train button.
* **Sort while training routes on the user's label, never the prediction.**
  The case being dropped is the one that was just labelled, so a Save chains
  into the next feed carrying that label (``sort_label``), not whatever the
  field says by then.
* **The counts column is the source of truth for what's on disk**, and any
  label found there that isn't a registered headstamp is back-filled into the
  model, so the field, the Sort grid and the Headstamps editor converge.

Layout is Qt-idiomatic, and split by workflow: a Capture column that reads
top-to-bottom as the loop itself (image → Feed → label+Save → readouts), the
image counts beside it behind a 50/50 splitter — they reflow into columns as
that splitter widens, which is what makes a 150-class model readable — and
the training launcher on its own strip at the foot. Clicking a counts row
saves-and-feeds.

The page is a two-card stack (Sort's empty-state pattern): the capture
surface, or — when the active model can't be trained here — a panel saying
why and offering the way out. The sidebar's Train button is always there
(app.py's ``_apply_mode_visibility``), so this page is what has to answer a
user who clicked it in AI Config mode or on a community model.

Everything the worker needs is snapshotted on the main thread — it never
reads a widget or the Config.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..data.models import CheckpointEnv, Model, is_openai_model, is_trainable, model_mode_label
from ..data.repository import HeadstampRepo, ModelRepo, SettingsRepo
from ..hardware import image_proc
from ..ml import classifier, local_inference
from ..training.dataset import class_counts, save_training_image
from ..training.manager import TrainingJob, TrainingManager
from .dialog_training_config import TrainingConfigDialog
from .dialog_training_progress import TrainingProgressDialog

CROP_SIZE = 320
AUTO_FEED_DELAY_MS = 50
NO_VALUE = "—"
NO_CROP_TEXT = "Click Feed to capture a case."
AI_MODE_TEXT = "AI Config mode — activate a local model on the Models page to train."
NO_HEADSTAMPS_TEXT = "No headstamps yet — type a label and save one."
NO_BROKER_TOOLTIP = "Connect to the board first"
CAPTURED_TEXT = "Captured. Pick a label, or click one in the counts list."
PREDICTED_TEXT = "Predicted label loaded — Save to keep it, or pick another."
NO_TORCH_TEXT = "Captured. PyTorch isn't installed, so nothing was predicted."
FOREIGN_MODEL_TEXT = (
    "“{name}” was downloaded from the community and is managed by its publisher, "
    "so it can't be trained here.\n\nTo build on it, export it from the Models page "
    "and import the archive back as your own model."
)

# The unavailable panel. Train is always in the sidebar now, so these are what
# a click on it has to answer: why not, and what to do instead. Same two cases
# `is_trainable` splits on — no local model at all, or someone else's.
UNAVAILABLE_TITLE_AI = "Training needs a local model"
UNAVAILABLE_TEXT_AI = (
    "Classification is running over HTTP: the AI Config server does the recognising, "
    "so there is no model on this machine to train.\n\nActivate or create a local model "
    "on the Models page to train one here."
)
UNAVAILABLE_TITLE_FOREIGN = "This model is managed by its publisher"
UNAVAILABLE_TITLE_OPENAI = "This model classifies over HTTP"
UNAVAILABLE_TEXT_OPENAI = (
    "“{name}” is an OpenAI model: an HTTP server does the recognising, so "
    "there is nothing on this machine to train.\n\nIts server settings and "
    "headstamps live on the AI Config page. To train locally, activate or "
    "create a ConvNeXt model on the Models page."
)
MODELS_JUMP_TEXT = "Open the Models page"


class TrainPage(QWidget):
    """The capture loop, the counts list, and the training launcher."""

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        self.db = win.db
        self.config = win.config
        self.bus = win.bus
        self.models = ModelRepo(self.db)
        self.headstamps = HeadstampRepo(self.db)
        self.settings = SettingsRepo(self.db)
        self.training_manager = TrainingManager(self.bus)
        self.progress_dialog: TrainingProgressDialog | None = None

        self._last_cropped: np.ndarray | None = None
        self._pending_output: tuple[int, str] | None = None
        self._feed_in_flight = False
        self._lock = threading.Lock()
        # The torch offer is made at most once per session — see `_offer_torch`.
        self._torch_prompt_shown = False
        # Test seam: the trainer script the manager spawns (None = the real one).
        self.training_script: Path | None = None

        # Index 0 is the working page, 1 the "why not" panel — see
        # `_apply_availability`, which is the only thing that switches them.
        self.stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)

        content = QWidget(self.stack)
        column = QVBoxLayout(content)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(8)
        column.addLayout(self._build_top_row())
        # A splitter, not a fixed HBox: the counts list carries 150+ classes on
        # some models and wraps into columns as it widens (see
        # _build_counts_group), so the user needs to be able to drag it wider
        # rather than being stuck with whatever share a stretch factor gave it.
        self.body_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        # Both children are group boxes; the objectName is what tells the
        # stylesheet to drop the handle to their frames (ui/theme.py).
        self.body_splitter.setObjectName("groupSplitter")
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.addWidget(self._build_capture_group())
        self.body_splitter.addWidget(self._build_counts_group())
        self.body_splitter.setStretchFactor(0, 1)
        self.body_splitter.setStretchFactor(1, 1)
        # 50/50 by default (JL): equal oversized hints scale down to an even
        # split of whatever width the page actually gets.
        self.body_splitter.setSizes([10000, 10000])
        column.addWidget(self.body_splitter, 1)
        column.addWidget(self._build_training_group())
        self.stack.addWidget(content)
        self.stack.addWidget(self._build_unavailable_panel())

        # A run ending re-enables the Train button even if the console is left
        # open; the manager posts these from its wait thread.
        for topic in ("training/done", "training/failed", "training/cancelled"):
            self.bus.subscribe(topic, lambda _payload: self._update_buttons(self.active_model()))

        self.refresh()

    # ----- construction -------------------------------------------------------

    def _build_top_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        caption = QLabel("Active model", self)
        caption.setObjectName("mutedLabel")
        row.addWidget(caption)
        self.active_label = QLabel(NO_VALUE, self)
        row.addWidget(self.active_label)
        row.addStretch(1)
        return row

    def _build_capture_group(self) -> QGroupBox:
        """One column reading as the training loop (JL): the case image, Feed
        to drop the next one, the label to give it, Save — then the readouts.
        """
        box = QGroupBox("Capture", self)
        self.capture_group = box
        outer = QHBoxLayout(box)
        column = QVBoxLayout()

        self.crop_label = QLabel(NO_CROP_TEXT, box)
        self.crop_label.setObjectName("cropPanel")
        self.crop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_label.setWordWrap(True)
        self.crop_label.setFixedSize(CROP_SIZE, CROP_SIZE)
        # Centered over the full group width (JL), which is also what lets
        # the label input below run wider than the image.
        column.addWidget(self.crop_label, 0, Qt.AlignmentFlag.AlignHCenter)

        # Centered directly under the image (JL): Feed belongs to the picture
        # it refills, not to either edge.
        self.feed_row = QHBoxLayout()
        self.feed_row.addStretch(1)
        self.feed_button = QPushButton("Feed", box)
        self.feed_button.setObjectName("action")
        self.feed_button.clicked.connect(lambda: self.feed())
        self.feed_row.addWidget(self.feed_button)
        self.feed_row.addStretch(1)
        column.addLayout(self.feed_row)

        label_row = QHBoxLayout()
        label_caption = QLabel("Label", box)
        label_caption.setObjectName("mutedLabel")
        label_row.addWidget(label_caption)
        self.label_combo = QComboBox(box)
        self.label_combo.setEditable(True)
        self.label_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        label_row.addWidget(self.label_combo, 1)
        self.save_button = QPushButton("Save image", box)
        self.save_button.clicked.connect(self.save_clicked)
        label_row.addWidget(self.save_button)
        column.addLayout(label_row)

        # Per-feed readouts: image processing covers feed +
        # capture + crop + mask; the breakdown is local_inference's own
        # per-step split, so a regression is diagnosable from the UI alone.
        readouts = QFormLayout()
        self.prediction_label = QLabel("", box)
        self.image_proc_label = QLabel(NO_VALUE, box)
        self.predict_label = QLabel(NO_VALUE, box)
        self.breakdown_label = QLabel(NO_VALUE, box)
        self.breakdown_label.setWordWrap(True)
        readouts.addRow("Predicted", self.prediction_label)
        readouts.addRow("Image processing", self.image_proc_label)
        readouts.addRow("Prediction time", self.predict_label)
        readouts.addRow("Breakdown", self.breakdown_label)
        column.addLayout(readouts)

        self.status_label = QLabel("", box)
        self.status_label.setObjectName("mutedLabel")
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)
        column.addStretch(1)
        # The column hugs the image's width; the group's leftover width stays
        # empty rather than stretching the input to the horizon.
        outer.addLayout(column, 1)
        return box

    def _build_training_group(self) -> QGroupBox:
        """The other workflow on this page: turn the images above into a model.

        Its own strip at the foot of the page, so the two buttons read as one
        cluster instead of straddling the capture controls.
        """
        box = QGroupBox("Training", self)
        self.training_group = box
        row = QHBoxLayout(box)
        hint = QLabel("Train a new checkpoint from the images above.", box)
        hint.setObjectName("mutedLabel")
        row.addWidget(hint)
        row.addStretch(1)
        # With the settings, not with Feed: routing cases while you train is a
        # training setting worn as a shortcut (JL).
        self.sort_check = QCheckBox("Sort while training", box)
        self.sort_check.setChecked(bool(self.config.sort_while_training))
        self.sort_check.setToolTip(
            "Drop each labelled case into its own bin instead of the catch-all, using the label you picked."
        )
        self.sort_check.toggled.connect(self._on_sort_while_training_toggled)
        row.addWidget(self.sort_check)
        self.settings_button = QPushButton("Training settings…", box)
        self.settings_button.clicked.connect(self.open_training_settings)
        row.addWidget(self.settings_button)
        self.train_button = QPushButton("Start training", box)
        self.train_button.setObjectName("action")
        self.train_button.clicked.connect(self.start_training)
        row.addWidget(self.train_button)
        return box

    def _build_unavailable_panel(self) -> QWidget:
        """Why Train can't run in this mode, and the one button out of it.

        Same shape as the Sort page's first-run panel (app._build_empty_state_panel):
        a title, the reason, and the action that resolves it.
        """
        panel = QWidget(self.stack)
        column = QVBoxLayout(panel)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(10)

        self.unavailable_title = QLabel("", panel)
        self.unavailable_title.setObjectName("emptyStateTitle")
        self.unavailable_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.unavailable_title)

        self.unavailable_text = QLabel("", panel)
        self.unavailable_text.setObjectName("mutedLabel")
        self.unavailable_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unavailable_text.setWordWrap(True)
        self.unavailable_text.setMaximumWidth(560)
        column.addWidget(self.unavailable_text, 0, Qt.AlignmentFlag.AlignHCenter)

        self.unavailable_button = QPushButton(MODELS_JUMP_TEXT, panel)
        self.unavailable_button.clicked.connect(self._open_models_page)
        column.addWidget(self.unavailable_button, 0, Qt.AlignmentFlag.AlignHCenter)
        return panel

    def _open_models_page(self) -> None:
        go = getattr(self._win, "go_to_activity", None)
        if go is not None:
            go("Models")

    def _build_counts_group(self) -> QGroupBox:
        box = QGroupBox("Image counts", self)
        self.counts_group = box
        box.setMinimumWidth(280)
        column = QVBoxLayout(box)
        self.counts_list = QListWidget(box)
        self.counts_list.setObjectName("countsList")
        self.counts_list.setToolTip("Click a headstamp to save the captured case under it and feed the next one.")
        # Wrapping top-to-bottom flow: fills a column alphabetically, then
        # starts the next one — reads like a directory listing, and a model
        # with 150+ classifications actually uses the width dragged into it
        # (see the splitter above) instead of scrolling one long column.
        # Adjust recomputes the column count on every resize, which is also
        # what collapses it back to a single column on a narrow window.
        self.counts_list.setFlow(QListView.Flow.TopToBottom)
        self.counts_list.setWrapping(True)
        self.counts_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.counts_list.itemClicked.connect(self._on_count_clicked)
        column.addWidget(self.counts_list, 1)
        return box

    # ----- state --------------------------------------------------------------

    def active_model(self) -> Model | None:
        model_id = self.settings.get_active_model_id()
        return self.models.get(model_id) if model_id is not None else None

    def counts(self) -> dict[str, int]:
        """Label → image count, exactly as the list is showing it."""
        out: dict[str, int] = {}
        for index in range(self.counts_list.count()):
            item = self.counts_list.item(index)
            out[str(item.data(Qt.ItemDataRole.UserRole))] = int(item.data(Qt.ItemDataRole.UserRole + 1))
        return out

    def refresh(self) -> None:
        """Re-read the active model, its headstamps and the images on disk.

        Called on ``mode/changed`` and whenever the page is shown — headstamps
        can be edited from the Models page, and images land here from a
        community import, so nothing about this is cached.
        """
        model = self.active_model()
        self._apply_availability(model)
        if model is None:
            self.active_label.setText(AI_MODE_TEXT)
            self.label_combo.clear()
            self.counts_list.clear()
            self._update_buttons(model)
            return

        self.active_label.setText(f"{model.name}  ({model_mode_label(model.model_mode)})")
        # The DB owns the headstamp list, but a filename on disk is a label the
        # user has actually used. Back-fill anything the DB is missing (a
        # pre-existing folder, an images-only import) so the field, the Sort
        # grid and the Headstamps editor agree. add_headstamp is idempotent.
        fs_counts = class_counts(paths.model_images_dir(model.id))
        known = {h.name for h in self.headstamps.list_for_model(model.id)}
        for label in fs_counts:
            if label not in known:
                self.config.add_headstamp(label)

        labels = sorted(h.name for h in self.headstamps.list_for_model(model.id))
        self._refill_labels(labels)
        counts = {label: fs_counts.get(label, 0) for label in labels}
        for label, count in fs_counts.items():
            counts.setdefault(label, count)
        self._render_counts(counts)
        self._update_buttons(model)

    def _apply_availability(self, model: Model | None) -> None:
        """Working page or explainer, decided by the same ``is_trainable`` the
        sidebar mutes Train on — so the button's ink and the page always agree."""
        if is_trainable(model):
            self.stack.setCurrentIndex(0)
            return
        if model is None:
            self.unavailable_title.setText(UNAVAILABLE_TITLE_AI)
            self.unavailable_text.setText(UNAVAILABLE_TEXT_AI)
        elif is_openai_model(model):
            self.unavailable_title.setText(UNAVAILABLE_TITLE_OPENAI)
            self.unavailable_text.setText(UNAVAILABLE_TEXT_OPENAI.format(name=model.name))
        else:
            self.unavailable_title.setText(UNAVAILABLE_TITLE_FOREIGN)
            self.unavailable_text.setText(FOREIGN_MODEL_TEXT.format(name=model.name))
        self.stack.setCurrentIndex(1)

    def is_available(self) -> bool:
        """Whether the capture surface is showing rather than the explainer."""
        return self.stack.currentIndex() == 0

    def _refill_labels(self, labels: list[str]) -> None:
        current = self.label_combo.currentText()
        blocked = self.label_combo.blockSignals(True)
        self.label_combo.clear()
        self.label_combo.addItems(labels)
        self.label_combo.setCurrentText(current if current in labels else "")
        self.label_combo.blockSignals(blocked)

    def _render_counts(self, counts: dict[str, int]) -> None:
        self.counts_list.clear()
        if not counts:
            placeholder = QListWidgetItem(NO_HEADSTAMPS_TEXT)
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.counts_list.addItem(placeholder)
            return
        for label in sorted(counts):
            item = QListWidgetItem(f"{label}   ·   {counts[label]}")
            item.setData(Qt.ItemDataRole.UserRole, label)
            item.setData(Qt.ItemDataRole.UserRole + 1, int(counts[label]))
            self.counts_list.addItem(item)

    def _update_buttons(self, model: Model | None) -> None:
        has_model = model is not None
        trainable = is_trainable(model)
        connected = getattr(self._win, "broker", None) is not None
        # Feed without a board would just re-photograph whatever is already
        # under the camera, so the button follows the connection the way the
        # Sort page's Start does. `feed()` itself still works board-less.
        self.feed_button.setEnabled(has_model and connected)
        self.feed_button.setToolTip("" if connected else NO_BROKER_TOOLTIP)
        self.save_button.setEnabled(has_model and self._last_cropped is not None)
        self.label_combo.setEnabled(has_model)
        self.sort_check.setEnabled(has_model)
        self.settings_button.setEnabled(trainable)
        # An open console counts as busy in its own right: the subprocess can
        # have exited a beat before the user closes it, and offering a second
        # run behind the first one's results reads as if nothing had happened.
        # `start_training` re-asks the manager anyway — this is only the hint.
        busy = self.progress_dialog is not None or self.training_manager.is_running
        self.train_button.setEnabled(trainable and not busy)

    def refresh_connection(self) -> None:
        """Re-evaluate the board-dependent controls. Cheap — no disk, no refill."""
        self._update_buttons(self.active_model())

    def _on_sort_while_training_toggled(self, enabled: bool) -> None:
        self.config.set_sort_while_training(bool(enabled))

    # ----- torch offer --------------------------------------------------------

    def _offer_torch(self, sort_label: str | None) -> bool:
        """Offer the PyTorch install before a Feed that would predict a label.

        Returns True when the caller should carry on feeding now.

        An *offer*, not a gate: declining must not cost the user the feed, only
        the prediction. Asked once per session, and a cancel re-enters the feed
        with the flag already set — that is what makes "no thanks" stick.

        The dialog comes from the window's gate (``win.ensure_torch``); a host
        without one — a test double — simply feeds and says why nothing was
        predicted.
        """
        if self._torch_prompt_shown:
            return True
        model = self.active_model()
        # No checkpoint → the feed predicts nothing either way, so there is
        # nothing for torch to do. Capturing images for an untrained model is
        # the normal path here, not an error.
        if not classifier.has_local_checkpoint(model):
            return True
        # An outdated torch is prompted for too — the checkpoint about to be
        # loaded is the user's own, so the gate makes it an offer.
        if local_inference.is_installed() and local_inference.meets_min_version():
            return True
        self._torch_prompt_shown = True
        ensure_torch = getattr(self._win, "ensure_torch", None)
        if ensure_torch is None:
            self.status_label.setText(NO_TORCH_TEXT)
            return True

        def resume() -> None:
            self.feed(sort_label=sort_label)

        kwargs: dict[str, Any] = {"reason": "Predicting labels needs PyTorch"}
        if _accepts_kwarg(ensure_torch, "model"):
            kwargs["model"] = model
        # A cancel has to re-enter the feed, so pass `on_cancel` when the gate
        # takes one; without it the flag alone makes the next Feed proceed.
        if _accepts_kwarg(ensure_torch, "on_cancel"):
            kwargs["on_cancel"] = resume
        return bool(ensure_torch(resume, **kwargs))

    # ----- feed ---------------------------------------------------------------

    def feed(self, sort_label: str | None = None) -> None:
        """Drop a case, capture it, crop it, and predict a label if we can."""
        if not self._offer_torch(sort_label):
            return
        with self._lock:
            if self._feed_in_flight:
                return
            self._feed_in_flight = True

        self.status_label.setText("Feeding…")
        self.prediction_label.setText("")

        # Sort while training: route the dropping case to its headstamp's run
        # slot. The label is the user's — the field when Feed is clicked, or the
        # just-saved one when a Save chains into a feed. NEVER the prediction.
        feed_slot = 0
        if self.sort_check.isChecked():
            label = (sort_label if sort_label is not None else self.label_combo.currentText()) or ""
            label = label.strip()
            if label:
                feed_slot = self.config.slot_for_headstamp(label) or 0

        model = self.active_model()
        model_path = model.model_path if (model and model.model_path) else None
        # Imported community models keep the resolution they were trained at;
        # feeding the wrong one silently degrades the prediction.
        image_size = int(model.training_config.image_size) if (model and model.training_config) else None
        broker = getattr(self._win, "broker", None)
        camera = self._win.camera
        proc_config = dict(self.config.image_proc)
        predict = bool(model_path) and Path(str(model_path)).exists() and local_inference.is_installed()

        def _work() -> dict[str, Any]:
            if broker is not None:
                try:
                    if not broker.force_sort_and_move(feed_slot):
                        return {"error": "Feed timeout"}
                except Exception as exc:
                    return {"error": f"Feed failed: {exc}"}
            started = time.perf_counter()
            frame = camera.capture_frame()
            if frame is None:
                return {"error": "Camera capture failed"}
            cropped = image_proc.crop_headstamp(frame, proc_config)
            cropped = image_proc.apply_primer_mask(
                cropped,
                proc_config.get("primer_mode", "none"),
                int(proc_config.get("primer_radius", 0)),
            )
            result: dict[str, Any] = {
                "cropped": cropped,
                "image_proc_ms": (time.perf_counter() - started) * 1000.0,
            }
            if predict and model_path:
                try:
                    started = time.perf_counter()
                    label, confidence = local_inference.classify(cropped, model_path, image_size=image_size)
                    result["predict_ms"] = (time.perf_counter() - started) * 1000.0
                    result["predict_device"] = local_inference.current_device_label()
                    result["predict_breakdown"] = dict(local_inference.last_timings)
                    result["label"] = label
                    result["confidence"] = confidence
                except Exception as exc:
                    result["classify_error"] = str(exc)
            return result

        self._win.run_worker(_work, on_done=self._on_feed_done, on_error=self._on_feed_error)

    def _on_feed_done(self, result: Any) -> None:
        with self._lock:
            self._feed_in_flight = False
        data = result if isinstance(result, dict) else {}
        error = data.get("error")
        if error:
            self.status_label.setText(str(error))
            return
        self._last_cropped = data.get("cropped")
        self._show_crop(self._last_cropped)
        self._show_timings(data)

        label = data.get("label")
        if label:
            # Pre-fill so a confident classification flows straight into Save.
            self.label_combo.setCurrentText(str(label))
            confidence = float(data.get("confidence") or 0.0)
            self.prediction_label.setText(f"{label}  ({confidence:.1f}%)")
            self.status_label.setText(PREDICTED_TEXT)
        elif "classify_error" in data:
            self.status_label.setText(f"Classify failed: {data['classify_error']}")
        elif classifier.has_local_checkpoint(self.active_model()) and not local_inference.is_installed():
            self.status_label.setText(NO_TORCH_TEXT)
        else:
            self.status_label.setText(CAPTURED_TEXT)
        self._update_buttons(self.active_model())

    def _on_feed_error(self, exc: Exception) -> None:
        with self._lock:
            self._feed_in_flight = False
        self.status_label.setText(f"Feed failed: {exc}")

    def _show_timings(self, data: dict[str, Any]) -> None:
        proc_ms = data.get("image_proc_ms")
        self.image_proc_label.setText(f"{proc_ms:.0f} ms" if isinstance(proc_ms, (int, float)) else NO_VALUE)
        predict_ms = data.get("predict_ms")
        if not isinstance(predict_ms, (int, float)):
            self.predict_label.setText(NO_VALUE)
            self.breakdown_label.setText(NO_VALUE)
            return
        device = data.get("predict_device")
        self.predict_label.setText(f"{predict_ms:.0f} ms" + (f" · {device}" if device else ""))
        # A prediction just ran locally, so the device is now known.
        self._win.refresh_device_indicator()
        breakdown = data.get("predict_breakdown") or {}
        self.breakdown_label.setText("  ".join(f"{k}:{v:.0f}" for k, v in breakdown.items()) if breakdown else NO_VALUE)

    def _show_crop(self, frame: Any) -> None:
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            self.crop_label.setText(NO_CROP_TEXT)
            return
        pixmap = QPixmap.fromImage(self._win.frame_to_image(frame))
        self.crop_label.setPixmap(
            pixmap.scaled(
                self.crop_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ----- save ---------------------------------------------------------------

    def save_clicked(self) -> None:
        label = self.label_combo.currentText().strip()
        if not label:
            self._win.notify("Missing label", "Pick or type a classification label.")
            return
        # Save-and-feed mirrors the counts-list click, so typing a brand-new
        # label keeps the rapid-labelling loop going.
        self.save_current(label, auto_feed_next=True)

    def _on_count_clicked(self, item: QListWidgetItem) -> None:
        label = item.data(Qt.ItemDataRole.UserRole)
        if not label:
            return
        if self._last_cropped is None:
            self.status_label.setText("Nothing to label yet — click Feed first.")
            return
        self.label_combo.setCurrentText(str(label))
        self.save_current(str(label), auto_feed_next=True)

    def save_current(self, label: str, *, auto_feed_next: bool = False) -> Path | None:
        """Write the captured crop under `label` and refresh the counts."""
        model = self.active_model()
        if model is None:
            self._win.notify("Pick a model first", "Activate a model on the Models page.")
            return None
        if self._last_cropped is None:
            self._win.notify("Capture first", "Click Feed before saving.")
            return None
        try:
            dest = save_training_image(self._last_cropped, paths.model_images_dir(model.id), label)
        except Exception as exc:
            self._win.notify("Save failed", str(exc))
            return None
        # A brand-new label becomes a model headstamp, so the field, the counts
        # and the Sort grid stay in step with what is on disk.
        self.config.add_headstamp(label)
        self.bus.post("status", f"Saved {dest.name}")
        self.status_label.setText(f"Saved as {label}.")
        # Dropped so a second Save can't write the same case twice.
        self._last_cropped = None
        self.crop_label.setText(NO_CROP_TEXT)
        self.prediction_label.setText("")
        self.refresh()
        if auto_feed_next:
            # The case about to drop is the one just labelled, so it sorts to
            # THIS label's slot even though the field has moved on.
            QTimer.singleShot(AUTO_FEED_DELAY_MS, self, lambda: self.feed(sort_label=label))
        return dest

    # ----- training -----------------------------------------------------------

    def open_training_settings(self) -> TrainingConfigDialog | None:
        model = self.active_model()
        if model is None:
            return None
        dialog = TrainingConfigDialog(
            model.training_config,
            on_saved=lambda _config: self.models.update(model),
            parent=self,
        )
        dialog.exec()
        return dialog

    def start_training(self) -> None:
        """Preflight, gate on torch, then hand the job to the manager."""
        model = self.active_model()
        if model is None:
            self._win.notify("Pick a model first", "Activate a model on the Models page.")
            return
        if not is_trainable(model):
            # The activity is hidden for these, so reaching here means the
            # active model changed while the page was open. Refuse rather than
            # fork someone else's model.
            self._win.notify("Community model", FOREIGN_MODEL_TEXT.format(name=model.name))
            return
        if self.training_manager.is_running:
            self._win.notify("Training in progress", "Cancel the current run first.")
            return
        image_dir = paths.model_images_dir(model.id)
        if not image_dir.exists() or not any(image_dir.iterdir()):
            self._win.notify("No images", "Save at least one training image first.")
            return
        # Torch is ~2 GB and isn't in the base venv; training is the one thing
        # on this page that genuinely can't proceed without it.
        ensure_torch = getattr(self._win, "ensure_torch", None)
        if ensure_torch is not None:
            kwargs: dict[str, Any] = {"reason": "Training needs PyTorch"}
            if _accepts_kwarg(ensure_torch, "model"):
                kwargs["model"] = model
            if not ensure_torch(self.start_training, **kwargs):
                return
        elif not local_inference.is_installed():
            self._win.notify("PyTorch required", "Training needs PyTorch, which isn't installed yet.")
            return

        paths.ensure_model_subtree(model.id)
        output = paths.model_trained_path(model.id)
        model.training_config.model_name = model.model_mode

        # Opened before the spawn so it catches the manager's `training/start`.
        self.progress_dialog = TrainingProgressDialog(
            self.bus,
            on_cancel=self.training_manager.cancel,
            on_closed=self._on_progress_closed,
            parent=self,
        )
        self.progress_dialog.show()
        try:
            self.training_manager.spawn(
                TrainingJob(image_dir=image_dir, output_model=output, config=model.training_config),
                script=self.training_script,
            )
        except Exception as exc:
            self._win.notify("Spawn failed", str(exc))
            self.progress_dialog.finished_run = True
            self.progress_dialog.close()
            self.progress_dialog = None
            return
        # Persist whatever the settings dialog may have changed.
        self.models.update(model)
        self._pending_output = (model.id, str(output))
        self._update_buttons(model)
        self._win.set_status(f"Training '{model.name}'…")

    def _on_progress_closed(self, payload: dict[str, Any] | None) -> None:
        """Adopt the checkpoint the run produced, if it produced one."""
        self.progress_dialog = None
        pending = self._pending_output
        self._pending_output = None
        if pending is None or payload is None or "best_val_acc" not in payload:
            self.refresh()
            return
        model_id, output = pending
        model = self.models.get(model_id)
        if model is not None and Path(output).exists():
            model.model_path = output
            model.last_training_date = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            # What the run was built with, straight off the completion marker.
            # Recorded here rather than read back out of the checkpoint, which
            # would need the very torch a stale machine may not have (#77).
            env = payload.get("env")
            if isinstance(env, dict):
                model.checkpoint_env = CheckpointEnv.from_dict(env)
            self.models.update(model)
        self.refresh()


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Whether `fn` takes a keyword called `name` (or **kwargs)."""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def build_train_page(win: Any) -> TrainPage:
    return TrainPage(win)
