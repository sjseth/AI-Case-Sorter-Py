"""Settings -> Camera page: device/resolution pick, live-preview swap.

The probe is two-phase: a fast, non-blocking name-only listing at build time
(``camera_names``) and a full ``list_cameras_with_metadata`` probe only when
the user clicks Detect, since that call opens every device and can block
seconds. The page never probes or swaps the live camera on its own — opening
a settings page must not grab hardware out from under a run, so Detect and
Apply are the only things that touch a device.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..hardware.camera import Camera, camera_names, list_cameras_with_metadata

PREVIEW_INITIAL_TEXT = "No frame"
NO_FEED_TEXT = "No camera feed — press Detect / refresh or check the device."


def _format_camera_choice(cam: dict[str, Any]) -> str:
    """Just the name: the list position is not the device index, and showing
    a "(0)" next to a status line reading "device 4" only ever misled (JL).
    The real index rides in the item's data."""
    return cam.get("name") or f"Camera {cam['index']}"


def _format_resolution(wh: tuple[int, int]) -> str:
    return f"{wh[0]} x {wh[1]}"


class CameraSection(QWidget):
    """Device + resolution pick, live preview swap. State lives on ``self``."""

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        self._detected: list[dict[str, Any]] = []

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        form = QFormLayout()
        self.device_combo = QComboBox(self)
        self.device_combo.setMaximumWidth(320)
        self.resolution_combo = QComboBox(self)
        self.resolution_combo.setMaximumWidth(200)
        form.addRow("Camera", self.device_combo)
        form.addRow("Resolution", self.resolution_combo)
        column.addLayout(form)

        buttons = QHBoxLayout()
        self.detect_button = QPushButton("Detect / refresh", self)
        self.detect_button.clicked.connect(self.detect_devices)
        self.apply_button = QPushButton("Apply", self)
        self.apply_button.setObjectName("action")
        self.apply_button.clicked.connect(self.apply_camera)
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.apply_button)
        buttons.addStretch(1)
        column.addLayout(buttons)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("mutedLabel")
        column.addWidget(self.status_label)
        self.current_label = QLabel("", self)
        self.current_label.setObjectName("mutedLabel")
        column.addWidget(self.current_label)

        # Live feed, so Apply isn't blind — same as the Tk camera tab. Ignored
        # policy + tiny minimum: the scaled pixmap must not drive the layout
        # (the Sort-preview window-growth bug, same fix).
        self.preview_label = QLabel(PREVIEW_INITIAL_TEXT, self)
        self.preview_label.setObjectName("cropPanel")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview_label.setMinimumSize(1, 1)
        column.addWidget(self.preview_label, 1)

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._preview_timer.start(100)

        self._populate_quick()
        self._refresh_current_label()
        # Connected last: the population above must not read as a user pick.
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)

    def _refresh_preview(self) -> None:
        if not self.isVisible():
            return  # don't decode frames for a page nobody is looking at
        frame = self._win.camera.latest_frame()
        if frame is None:
            # Nothing has ever been grabbed off this device — say what to do
            # about it instead of leaving an empty panel (JL).
            if self.preview_label.text() != NO_FEED_TEXT:
                self.preview_label.setText(NO_FEED_TEXT)
            return
        pixmap = QPixmap.fromImage(self._win.frame_to_image(frame))
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ----- population ----------------------------------------------------

    def _populate_quick(self) -> None:
        """Name-only enumeration — doesn't open any device, safe at build time."""
        names = camera_names()
        cam_cfg = self._win.config.camera
        if names:
            entries = [{"index": idx, "name": name, "resolutions": []} for idx, name in sorted(names.items())]
        else:
            idx = int(cam_cfg.get("device_index", 0))
            entries = [{"index": idx, "name": f"Camera {idx}", "resolutions": []}]
        self._detected = entries
        self._fill_device_combo(select_index=int(cam_cfg.get("device_index", 0)))
        self.status_label.setText("Click Detect / refresh to probe supported resolutions.")

    def _fill_device_combo(self, *, select_index: int) -> None:
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for cam in self._detected:
            self.device_combo.addItem(_format_camera_choice(cam), cam)
        if self._detected:
            match = next((i for i, cam in enumerate(self._detected) if cam["index"] == select_index), 0)
            self.device_combo.setCurrentIndex(match)
        self.device_combo.blockSignals(False)
        self._fill_resolution_combo()

    def _fill_resolution_combo(self) -> None:
        cam = self.device_combo.currentData()
        cam_cfg = self._win.config.camera
        saved = (int(cam_cfg.get("width", 0)), int(cam_cfg.get("height", 0)))
        resolutions: list[tuple[int, int]] = (cam or {}).get("resolutions") or []

        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        if resolutions:
            for wh in resolutions:
                self.resolution_combo.addItem(_format_resolution(wh), wh)
            # resolutions is sorted ascending by pixel count (list_cameras_with_metadata's
            # contract), so the last entry is the largest supported.
            if saved in resolutions and cam is not None and cam["index"] == int(cam_cfg.get("device_index", 0)):
                self.resolution_combo.setCurrentIndex(resolutions.index(saved))
            else:
                self.resolution_combo.setCurrentIndex(len(resolutions) - 1)
        elif saved[0] and saved[1]:
            # No probed resolutions yet — keep whatever is saved so the combo isn't empty.
            self.resolution_combo.addItem(_format_resolution(saved), saved)
            self.resolution_combo.setCurrentIndex(0)
        self.resolution_combo.blockSignals(False)

    def _on_device_changed(self, _index: int) -> None:
        self._fill_resolution_combo()

    # ----- detect ----------------------------------------------------------

    def detect_devices(self) -> None:
        self.detect_button.setEnabled(False)
        self.status_label.setText("Detecting cameras… (opens each device)")
        self._win.run_worker(
            list_cameras_with_metadata,
            on_done=self._on_detect_done,
            on_error=self._on_detect_error,
        )

    def _on_detect_done(self, cameras: list[dict[str, Any]]) -> None:
        self.detect_button.setEnabled(True)
        if not cameras:
            self.status_label.setText("No cameras detected.")
            return
        self._detected = cameras
        self._fill_device_combo(select_index=int(self._win.config.camera.get("device_index", 0)))
        cam = self.device_combo.currentData()
        chosen_name = cam.get("name", "?") if cam else "?"
        self.status_label.setText(f"Detected {len(cameras)} camera(s). Selected {chosen_name}.")

    def _on_detect_error(self, exc: Exception) -> None:
        self.detect_button.setEnabled(True)
        self.status_label.setText(f"Detect failed: {exc}")

    # ----- apply -------------------------------------------------------------

    def apply_camera(self) -> None:
        """Swap the live camera in place — mirrors ``ui.app.MainWindow.restart_camera``."""
        cam = self.device_combo.currentData()
        if cam is None:
            self._win.set_status("No camera selected.")
            return
        wh = self.resolution_combo.currentData()
        cam_cfg = self._win.config.camera
        width = int(wh[0]) if wh else int(cam_cfg.get("width", 640))
        height = int(wh[1]) if wh else int(cam_cfg.get("height", 480))

        self._win.camera.stop()
        new_camera = Camera(device_index=int(cam["index"]), width=width, height=height)
        started = new_camera.start_preview()
        # Swap unconditionally, same as restart_camera: a failed device still
        # replaces the old (now-stopped) one rather than leaving a dead handle.
        self._win.camera = new_camera
        if self._win.run_controller is not None:
            self._win.run_controller.camera = new_camera

        cam_cfg["device_index"] = int(cam["index"])
        cam_cfg["device_chosen"] = True
        cam_cfg["width"] = new_camera.width
        cam_cfg["height"] = new_camera.height
        self._win.config.save()

        if started:
            self._win._set_camera_indicator(
                f"Camera: connected ({new_camera.width}x{new_camera.height})", connected=True
            )
            self._win.set_status(f"Using {cam.get('name', '?')} @ {new_camera.width}x{new_camera.height}.")
        else:
            self._win._set_camera_indicator("Camera: failed to start", connected=False)
            self._win.set_status("Camera failed to start. Check the device index.")
        self._refresh_current_label()

    def _refresh_current_label(self) -> None:
        cam = self._win.camera
        self.current_label.setText(f"Current: device {cam.device_index} @ {cam.width}x{cam.height}")


def build_camera_section(win: Any) -> CameraSection:
    return CameraSection(win)
