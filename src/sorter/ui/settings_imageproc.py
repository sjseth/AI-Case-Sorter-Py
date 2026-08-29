"""Settings -> Image Proc page: Hough tuning, primer mask, LED, before/after preview.

Pipeline reference: ``sorter/hardware/image_proc.py``.

Every control persists to ``Config`` on change — no separate Save step — and
reprocesses the last-captured frame against it. The line-scan strategy stays
dormant and UI-hidden (``crop_headstamp`` always picks Hough).

The reason this page listens on the bus: crop and primer
settings belong to the model (case diameter and primer size are cartridge
properties), and the model row has carried them since the WinForms port —
``Model.use_primer_mask``/``hide_primer``/``primer_mask_size`` and
``Model.image_processing``. Nothing read them — the UI tuned the single global
``config.image_proc`` and switching models left it untouched. This page reads
and writes the active model's values, mirroring them into ``config.image_proc``,
which stays the live copy ``run_controller`` reads. See the ``ImageProcSection``
class docstring for how a pristine model row inherits the global.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..data.models import ImageProcessingConfig, Model
from ..data.repository import ModelRepo
from ..hardware.image_proc import (
    CASE_MIN_DISC_BRIGHTNESS,
    HoughParams,
    apply_primer_mask,
    crop_headstamp,
    disc_brightness,
    hough_detect,
    overlay_detection,
)

PRIMER_MODES: tuple[tuple[str, str], ...] = (
    ("None", "none"),
    ("Keep primer area only", "use"),
    ("Hide primer", "hide"),
)
LED_DEBOUNCE_MS = 500
LED_RECAPTURE_DELAY_MS = 200

# A model row still holding these has never had the page's settings written to
# it, so it inherits whatever the global currently holds instead of resetting it.
_NEW_MODEL = Model()
_UNSET_PRIMER = (_NEW_MODEL.use_primer_mask, _NEW_MODEL.hide_primer, _NEW_MODEL.primer_mask_size)
_UNSET_HOUGH = dict(ImageProcessingConfig().hough)


def primer_mode_of(model: Model) -> str:
    """The page's tri-state primer mode from the model's two legacy booleans."""
    if model.use_primer_mask:
        return "use"
    return "hide" if model.hide_primer else "none"


def _write_primer_to(model: Model, mode: str, radius: int) -> None:
    model.use_primer_mask = mode == "use"
    model.hide_primer = mode == "hide"
    model.primer_mask_size = radius
    # The legacy booleans above are what the model editor and the WinForms
    # manifest read; image_processing is the same values in the newer shape.
    model.image_processing.primer_mode = mode
    model.image_processing.primer_radius = radius


class ImageProcSection(QWidget):
    """Hough + primer + LED controls, and a before/after preview. State on ``self``.

    Crop and primer settings follow the active model. ``sync_from_active_model``
    is the one direction of travel: model row → ``config.image_proc`` → widgets,
    run on ``mode/changed``, on show (the model editor edits the same primer
    fields and posts nothing) and at construction. A model still holding the
    dataclass defaults inherits the global rather than resetting it, so an
    install that tuned the global before this page existed keeps its tuning.

    LED brightness is genuinely global — it is a board setting
    (``serial.init_settings.cameraledlevel``), not a model one.
    """

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        self._raw_frame: np.ndarray | None = None
        self._led_pending_value: int | None = None
        self._led_last_sent: int | None = None
        # Set while widgets are being loaded from config, so the change handlers
        # don't write what they just read back out again.
        self._syncing = False

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        column.addWidget(self._build_hough_group())
        column.addWidget(self._build_primer_group())
        column.addWidget(self._build_led_group())
        column.addLayout(self._build_actions_row())
        column.addLayout(self._build_preview_row(), 1)

        self._led_timer = QTimer(self)
        self._led_timer.setSingleShot(True)
        self._led_timer.timeout.connect(self._apply_led)

        win.bus.subscribe("mode/changed", lambda _payload: self.sync_from_active_model())
        self.sync_from_active_model()

    # ----- model scope -------------------------------------------------------

    def _active_model(self) -> Model | None:
        """The active model, re-read every time — never a snapshot to write back."""
        model_id = self._win.config.settings.get_active_model_id()
        if model_id is None:  # AI Config mode: the global settings are the scope
            return None
        return ModelRepo(self._win.config.db).get(model_id)

    def sync_from_active_model(self) -> None:
        """Load the active model's settings into the live config, then the widgets."""
        model = self._active_model()
        if model is not None:
            cfg = self._win.config.image_proc
            before = (
                cfg.get("primer_mode"),
                cfg.get("primer_radius"),
                cfg.get("case_min_brightness"),
                dict(cfg.get("hough") or {}),
            )
            if (model.use_primer_mask, model.hide_primer, model.primer_mask_size) != _UNSET_PRIMER:
                cfg["primer_mode"] = primer_mode_of(model)
                cfg["primer_radius"] = int(model.primer_mask_size)
            if model.image_processing.hough != _UNSET_HOUGH:
                cfg["hough"] = {**(cfg.get("hough") or {}), **model.image_processing.hough}
            if model.image_processing.case_min_brightness != ImageProcessingConfig().case_min_brightness:
                cfg["case_min_brightness"] = int(model.image_processing.case_min_brightness)
            after = (
                cfg.get("primer_mode"),
                cfg.get("primer_radius"),
                cfg.get("case_min_brightness"),
                dict(cfg.get("hough") or {}),
            )
            if after != before:
                self._win.config.save()
        self.refresh_from_config()

    def refresh_from_config(self) -> None:
        """Re-read every control from ``config.image_proc`` without writing back."""
        cfg = self._win.config.image_proc
        hough = cfg.get("hough") or {}
        self._syncing = True
        try:
            self.dp_spin.setValue(float(hough.get("dp", 2.0)))
            self.min_dist_spin.setValue(int(hough.get("min_dist", 500)))
            self.param1_spin.setValue(int(hough.get("param1", 100)))
            self.param2_spin.setValue(int(hough.get("param2", 60)))
            self.min_radius_spin.setValue(int(hough.get("min_radius", 150)))
            self.max_radius_spin.setValue(int(hough.get("max_radius", 250)))
            self.case_floor_spin.setValue(int(cfg.get("case_min_brightness", CASE_MIN_DISC_BRIGHTNESS)))
            index = self.primer_mode_combo.findData(cfg.get("primer_mode", "hide"))
            self.primer_mode_combo.setCurrentIndex(index if index >= 0 else 2)
            self.primer_radius_spin.setValue(int(cfg.get("primer_radius", 135)))
        finally:
            self._syncing = False
        self._reprocess()

    def _store_on_active_model(self) -> None:
        """Mirror what the page just wrote to the config onto the active model."""
        model = self._active_model()
        if model is None:
            return
        cfg = self._win.config.image_proc
        _write_primer_to(model, str(cfg.get("primer_mode", "hide")), int(cfg.get("primer_radius", 135)))
        model.image_processing.strategy = str(cfg.get("strategy", "hough"))
        model.image_processing.hough = dict(cfg.get("hough") or {})
        model.image_processing.case_min_brightness = int(cfg.get("case_min_brightness", CASE_MIN_DISC_BRIGHTNESS))
        with self._win.config.db.transaction():
            ModelRepo(self._win.config.db).update(model)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.sync_from_active_model()

    # ----- Hough group -----------------------------------------------------

    def _build_hough_group(self) -> QGroupBox:
        box = QGroupBox("Configuration", self)
        grid = QGridLayout(box)
        h = self._win.config.image_proc.get("hough", {})

        self.dp_spin = QDoubleSpinBox(box)
        self.dp_spin.setRange(1, 10)
        self.dp_spin.setSingleStep(0.5)
        self.dp_spin.setValue(float(h.get("dp", 2.0)))

        self.min_dist_spin = QSpinBox(box)
        self.min_dist_spin.setRange(1, 4000)
        self.min_dist_spin.setValue(int(h.get("min_dist", 500)))

        self.param1_spin = QSpinBox(box)
        self.param1_spin.setRange(1, 500)
        self.param1_spin.setValue(int(h.get("param1", 100)))

        self.param2_spin = QSpinBox(box)
        self.param2_spin.setRange(1, 500)
        self.param2_spin.setValue(int(h.get("param2", 60)))

        self.min_radius_spin = QSpinBox(box)
        self.min_radius_spin.setRange(1, 4000)
        self.min_radius_spin.setValue(int(h.get("min_radius", 150)))

        self.max_radius_spin = QSpinBox(box)
        self.max_radius_spin.setRange(1, 4000)
        self.max_radius_spin.setValue(int(h.get("max_radius", 250)))

        self.case_floor_spin = QSpinBox(box)
        self.case_floor_spin.setRange(0, 255)
        self.case_floor_spin.setValue(
            int(self._win.config.image_proc.get("case_min_brightness", CASE_MIN_DISC_BRIGHTNESS))
        )
        self.case_floor_spin.setToolTip(
            "End-of-run empty-nest check: a detected disc darker than this does not\n"
            "count as a case. Capture the EMPTY nest, read its brightness in the\n"
            "status line, set this above it — then capture a case and confirm it\n"
            "reads clear of the floor."
        )

        fields = (
            ("Accumulator scale (dp)", self.dp_spin),
            ("Min center separation (px)", self.min_dist_spin),
            ("Edge strength (param1)", self.param1_spin),
            ("Detection threshold (param2)", self.param2_spin),
            ("Min case radius (px)", self.min_radius_spin),
            ("Max case radius (px)", self.max_radius_spin),
            ("Case brightness floor", self.case_floor_spin),
        )
        for idx, (label, widget) in enumerate(fields):
            grid.addWidget(QLabel(label, box), idx // 3, (idx % 3) * 2)
            grid.addWidget(widget, idx // 3, (idx % 3) * 2 + 1)
            widget.valueChanged.connect(self._on_hough_changed)
        return box

    def _on_hough_changed(self, _value: Any = None) -> None:
        if self._syncing:
            return
        cfg = self._win.config.image_proc
        cfg["strategy"] = "hough"
        cfg["hough"] = {
            "dp": float(self.dp_spin.value()),
            "min_dist": int(self.min_dist_spin.value()),
            "param1": float(self.param1_spin.value()),
            "param2": float(self.param2_spin.value()),
            "min_radius": int(self.min_radius_spin.value()),
            "max_radius": int(self.max_radius_spin.value()),
        }
        cfg["case_min_brightness"] = int(self.case_floor_spin.value())
        self._win.config.save()
        self._store_on_active_model()
        self._reprocess()

    # ----- Primer group ------------------------------------------------------

    def _build_primer_group(self) -> QGroupBox:
        box = QGroupBox("Primer mask", self)
        row = QHBoxLayout(box)
        ip = self._win.config.image_proc

        self.primer_mode_combo = QComboBox(box)
        for label, value in PRIMER_MODES:
            self.primer_mode_combo.addItem(label, value)
        saved_mode = ip.get("primer_mode", "hide")
        match = next((i for i, (_l, v) in enumerate(PRIMER_MODES) if v == saved_mode), 2)
        self.primer_mode_combo.setCurrentIndex(match)
        row.addWidget(self.primer_mode_combo)

        row.addWidget(QLabel("Primer radius (px)", box))
        self.primer_radius_spin = QSpinBox(box)
        self.primer_radius_spin.setRange(1, 240)
        self.primer_radius_spin.setValue(int(ip.get("primer_radius", 135)))
        row.addWidget(self.primer_radius_spin)
        row.addStretch(1)

        self.primer_mode_combo.currentIndexChanged.connect(self._on_primer_changed)
        self.primer_radius_spin.valueChanged.connect(self._on_primer_changed)
        return box

    def _on_primer_changed(self, _value: Any = None) -> None:
        if self._syncing:
            return
        cfg = self._win.config.image_proc
        cfg["primer_mode"] = self.primer_mode_combo.currentData()
        cfg["primer_radius"] = int(self.primer_radius_spin.value())
        self._win.config.save()
        self._store_on_active_model()
        self._reprocess()

    # ----- LED group -----------------------------------------------------------

    def _build_led_group(self) -> QGroupBox:
        box = QGroupBox("Camera LED brightness", self)
        column = QVBoxLayout(box)
        init_settings = self._win.config.serial.get("init_settings", {})
        initial = int(init_settings.get("cameraledlevel", 130))

        self.led_slider = QSlider(Qt.Orientation.Horizontal, box)
        self.led_slider.setRange(1, 255)
        self.led_slider.setValue(initial)
        column.addWidget(self.led_slider)

        row = QFormLayout()
        self.led_value_label = QLabel(str(initial), box)
        row.addRow("Value", self.led_value_label)
        column.addLayout(row)
        column.addWidget(QLabel(f"(sends cameraledlevel:N once idle for {LED_DEBOUNCE_MS} ms)", box))

        self.led_slider.valueChanged.connect(self._on_led_changed)
        return box

    def _on_led_changed(self, value: int) -> None:
        self.led_value_label.setText(str(value))
        self._led_pending_value = int(value)
        self._led_timer.stop()
        self._led_timer.start(LED_DEBOUNCE_MS)

    def _apply_led(self) -> None:
        value = self._led_pending_value
        if value is None or value == self._led_last_sent:
            return
        self._led_last_sent = value
        # Persist immediately so a restart picks up the same brightness.
        init_settings = dict(self._win.config.serial.get("init_settings", {}))
        init_settings["cameraledlevel"] = value
        self._win.config.serial["init_settings"] = init_settings
        self._win.config.save()

        broker = self._win.broker
        if broker is None or not getattr(broker, "is_connected", False):
            self._win.set_status(f"Camera LED level = {value} (saved; not connected).")
            return
        try:
            broker.send_command(f"cameraledlevel:{value}")
            self._win.set_status(f"Camera LED level → {value}.")
            # Give the LED + camera a moment to settle, then show the new brightness.
            QTimer.singleShot(LED_RECAPTURE_DELAY_MS, self, self.capture)
        except Exception as exc:
            self._win.set_status(f"LED send failed: {exc}")

    # ----- actions + preview -----------------------------------------------

    def _build_actions_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.capture_button = QPushButton("Capture", self)
        self.capture_button.setObjectName("action")
        self.capture_button.clicked.connect(self.capture)
        row.addWidget(self.capture_button)
        row.addStretch(1)
        return row

    def _build_preview_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.raw_preview_label = self._preview_label()
        row.addWidget(self.raw_preview_label, 1)
        arrow = QLabel("→", self)
        row.addWidget(arrow)
        self.processed_preview_label = self._preview_label()
        row.addWidget(self.processed_preview_label, 1)
        return row

    def _preview_label(self) -> QLabel:
        label = QLabel("No frame", self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Ignored + tiny minimum, same reasoning as the Sort preview: a fixed
        # size hint from the pixmap would ratchet the layout on every frame.
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        label.setMinimumSize(1, 1)
        label.setStyleSheet("background-color: #000000; color: #808080;")
        return label

    def capture(self) -> None:
        self._win.run_worker(
            self._win.camera.capture_frame,
            on_done=self._on_captured,
            on_error=self._on_capture_error,
        )

    def _on_captured(self, frame: np.ndarray | None) -> None:
        if frame is None:
            self._win.set_status("No camera frame available.")
            return
        self._raw_frame = frame
        self._reprocess()

    def _on_capture_error(self, exc: Exception) -> None:
        self._win.set_status(f"Capture failed: {exc}")

    def _reprocess(self) -> None:
        """Re-run the pipeline against the last captured frame — no re-capture."""
        if self._raw_frame is None:
            return
        cfg = self._win.config.image_proc
        params = HoughParams.from_dict(cfg.get("hough", {}))
        detection = hough_detect(self._raw_frame, params)
        preview = overlay_detection(self._raw_frame, detection)
        if detection is None:
            self._win.set_status("No circle detected within radius bounds.")
        else:
            cx, cy, r = detection
            # The disc brightness is what the end-of-brass flush compares to
            # the case floor — showing it here is how the floor gets tuned.
            brightness = disc_brightness(self._raw_frame, detection)
            floor = int(cfg.get("case_min_brightness", CASE_MIN_DISC_BRIGHTNESS))
            self._win.set_status(
                f"Detected circle: r={r:.0f} px at ({cx:.0f}, {cy:.0f}). "
                f"Disc brightness {brightness:.0f} (case floor {floor})."
            )

        cropped = crop_headstamp(self._raw_frame, cfg)
        cropped = apply_primer_mask(cropped, self.primer_mode_combo.currentData(), int(self.primer_radius_spin.value()))
        self._show(self.raw_preview_label, preview)
        self._show(self.processed_preview_label, cropped)

    def _show(self, label: QLabel, frame: np.ndarray) -> None:
        pixmap = QPixmap.fromImage(self._win.frame_to_image(frame))
        label.setPixmap(
            pixmap.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )


def build_imageproc_section(win: Any) -> ImageProcSection:
    return ImageProcSection(win)
