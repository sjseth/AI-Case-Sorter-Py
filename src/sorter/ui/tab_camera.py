"""Camera tab: friendly device-name dropdown, resolution dropdown, preview.

Two-phase population:
 - On construction, populate the dropdown using the fast OS-level name
   enumeration (camera_names). This doesn't open any cameras, so it doesn't
   fight with the live preview. The resolution dropdown is left with just
   the saved resolution (or empty on fresh installs).
 - When the user clicks Detect, do the full probe — open each device, query
   supported resolutions. Because cv2 typically can't open a device that's
   already in use, this temporarily pauses the live preview and restarts
   it when done.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..control.events import EventBus
from ..hardware import camera as camera_mod
from .widgets import ImagePanel, build_button_row

# Startup resolution preference: highest of these the camera supports,
# falling back to the max resolution it reports if neither is available.
PREFERRED_RESOLUTIONS: list[tuple[int, int]] = [(1920, 1080), (1280, 720)]


def _pick_best_resolution(resolutions: list[tuple[int, int]]) -> tuple[int, int] | None:
    if not resolutions:
        return None
    available = set(resolutions)
    for pref in PREFERRED_RESOLUTIONS:
        if pref in available:
            return pref
    # resolutions is sorted ascending by pixel count.
    return resolutions[-1]


def _format_camera_choice(cam: dict) -> str:
    name = cam.get("name") or f"Camera {cam['index']}"
    return f"({cam['index']}) {name}"


def _format_resolution(wh: tuple[int, int]) -> str:
    return f"{wh[0]} x {wh[1]}"


def _parse_resolution(label: str) -> tuple[int, int] | None:
    try:
        w_str, h_str = label.split("x")
        return int(w_str.strip()), int(h_str.strip())
    except (ValueError, AttributeError):
        return None


class CameraTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        # Not `self.config` — that collides with ttk.Widget.config().
        self.cfg = config
        self.bus = bus
        self.app = app
        self._detected: list[dict] = []  # filled by quick or full enumeration

        cam_cfg = config.camera

        controls = ttk.LabelFrame(self, text="Device")
        controls.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        ttk.Label(controls, text="Camera").grid(row=0, column=0, padx=6, pady=6, sticky=tk.W)
        self.camera_var = tk.StringVar()
        self.camera_combo = ttk.Combobox(
            controls,
            textvariable=self.camera_var,
            width=40,
            state="readonly",
        )
        self.camera_combo.grid(row=0, column=1, padx=6, pady=6, sticky=tk.W)
        self.camera_combo.bind("<<ComboboxSelected>>", self._on_camera_selected)

        ttk.Label(controls, text="Resolution").grid(row=1, column=0, padx=6, pady=6, sticky=tk.W)
        self.resolution_var = tk.StringVar()
        self.resolution_combo = ttk.Combobox(
            controls,
            textvariable=self.resolution_var,
            width=20,
            state="readonly",
        )
        self.resolution_combo.grid(row=1, column=1, padx=6, pady=6, sticky=tk.W)

        build_button_row(
            controls,
            [
                ("Detect", self.detect_devices),
                ("Apply / Start preview", self.apply_and_start),
                ("Stop preview", self.stop_preview),
            ],
            primary="Apply / Start preview",
        ).grid(
            row=2,
            column=0,
            columnspan=2,
            padx=6,
            pady=4,
            sticky=tk.W,
        )

        self.status_label = ttk.Label(controls, text="", style="Muted.TLabel")
        self.status_label.grid(row=3, column=0, columnspan=2, padx=6, sticky=tk.W)

        self.preview = ImagePanel(self, width=640, height=480)
        self.preview.pack(side=tk.TOP, padx=8, pady=8)

        # Quick startup: name-only enumeration. No opening cameras, no
        # disruption to the running preview. Resolutions stay unknown until
        # the full probe below completes.
        self._populate_from_names(cam_cfg)

        # Full probe on startup: detect cameras + supported resolutions and
        # pick the best one (1920x1080 > 1280x720 > max supported), then
        # start the preview at that resolution. The app doesn't start the
        # camera itself — this is what brings the preview up.
        self._auto_detect_on_startup()

    # ----- selection logic ---------------------------------------------------

    def _pick_default_camera(self, cameras: list[dict]) -> dict | None:
        """Saved device (if explicitly chosen) → Webcam-named → first."""
        if not cameras:
            return None
        chosen: dict | None = None
        if bool(self.cfg.camera.get("device_chosen", False)):
            saved_idx = int(self.cfg.camera.get("device_index", 0))
            chosen = next((c for c in cameras if c["index"] == saved_idx), None)
        if chosen is None:
            chosen = next(
                (c for c in cameras if "webcam" in (c.get("name") or "").lower()),
                None,
            )
        if chosen is None:
            chosen = cameras[0]
        return chosen

    # ----- quick (name-only) population -------------------------------------

    def _populate_from_names(self, cam_cfg: dict) -> None:
        names = camera_mod.camera_names()
        cameras = [{"index": idx, "name": name, "resolutions": []} for idx, name in sorted(names.items())]

        saved_w = int(cam_cfg.get("width", 0))
        saved_h = int(cam_cfg.get("height", 0))

        if not cameras:
            # No /sys, no pygrabber. Fall back to a single placeholder so
            # the dropdown isn't empty.
            idx = int(cam_cfg.get("device_index", 0))
            self.camera_combo["values"] = [f"({idx}) Camera {idx}"]
            self.camera_var.set(f"({idx}) Camera {idx}")
        else:
            self._detected = cameras
            self.camera_combo["values"] = [_format_camera_choice(c) for c in cameras]
            chosen = self._pick_default_camera(cameras)
            if chosen is not None:
                self.camera_var.set(_format_camera_choice(chosen))
                # If the chosen device differs from what the preview is
                # currently showing, swap to it. Don't persist — that
                # happens only when the user clicks Apply.
                if chosen["index"] != int(cam_cfg.get("device_index", 0)):
                    self.app.restart_camera(device_index=chosen["index"])

        # Resolution dropdown shows the saved size (if any) until Detect is
        # clicked and we have a real supported list.
        if saved_w and saved_h:
            self.resolution_combo["values"] = [_format_resolution((saved_w, saved_h))]
            self.resolution_var.set(_format_resolution((saved_w, saved_h)))
        else:
            self.resolution_combo["values"] = []
            self.resolution_var.set("")

        self.status_label.config(text="Click Detect to probe supported resolutions (briefly pauses preview).")

    # ----- Startup auto-detect (full probe; starts the preview) -------------

    def _auto_detect_on_startup(self) -> None:
        self.status_label.config(text="Detecting cameras…")
        # _populate_from_names may have already started a preview (e.g. if
        # the saved device index differs from the default) — release it so
        # the probe can open each device.
        self.app.stop_camera()
        self.app.run_worker(
            camera_mod.list_cameras_with_metadata,
            on_done=self._startup_detect_done,
            on_error=self._startup_detect_failed,
        )

    def _startup_detect_done(self, cameras: list[dict]) -> None:
        if not cameras:
            self._detected = []
            self.status_label.config(text="No cameras detected.")
            self.app.start_camera()
            return

        self._detected = cameras
        self.camera_combo["values"] = [_format_camera_choice(c) for c in cameras]
        chosen = self._pick_default_camera(cameras)
        if chosen is None:
            chosen = cameras[0]
        self.camera_var.set(_format_camera_choice(chosen))
        self._populate_resolutions(chosen)

        best = _pick_best_resolution(chosen.get("resolutions") or [])
        if best is not None:
            self.resolution_var.set(_format_resolution(best))

        self.status_label.config(
            text=f"Detected {len(cameras)} camera(s). Selected {chosen.get('name', '?')} @ {self.resolution_var.get()}."
        )

        wh = _parse_resolution(self.resolution_var.get())
        self.app.restart_camera(
            device_index=chosen["index"],
            width=wh[0] if wh else None,
            height=wh[1] if wh else None,
        )

    def _startup_detect_failed(self, exc: Exception) -> None:
        self.status_label.config(text=f"Detect failed: {exc}")
        self.app.start_camera()

    # ----- Detect (full probe; pauses preview) ------------------------------

    def detect_devices(self) -> None:
        self.status_label.config(text="Detecting cameras… (preview paused)")
        # Release the live camera so the probe can open each device.
        self.app.stop_camera()
        self.app.run_worker(
            camera_mod.list_cameras_with_metadata,
            on_done=self._detect_done,
            on_error=self._detect_failed,
        )

    def _detect_done(self, cameras: list[dict]) -> None:
        if not cameras:
            self._detected = []
            self.camera_combo["values"] = []
            self.resolution_combo["values"] = []
            self.status_label.config(text="No cameras detected.")
            # Best effort: restart whatever preview was running.
            self.app.restart_camera()
            return

        self._detected = cameras
        self.camera_combo["values"] = [_format_camera_choice(c) for c in cameras]
        chosen = self._pick_default_camera(cameras)
        if chosen is None:
            chosen = cameras[0]
        self.camera_var.set(_format_camera_choice(chosen))
        self._populate_resolutions(chosen)

        self.status_label.config(text=f"Detected {len(cameras)} camera(s). Selected {chosen.get('name', '?')}.")

        # Restart preview with the chosen device + resolution. We don't
        # persist — Apply commits.
        wh = _parse_resolution(self.resolution_var.get())
        self.app.restart_camera(
            device_index=chosen["index"],
            width=wh[0] if wh else None,
            height=wh[1] if wh else None,
        )

    def _detect_failed(self, exc: Exception) -> None:
        self.status_label.config(text=f"Detect failed: {exc}")
        # Restart preview from saved config so the user isn't left with a
        # black panel.
        self.app.restart_camera()

    # ----- camera dropdown handling -----------------------------------------

    def _on_camera_selected(self, _event=None) -> None:
        cam = self._current_selected_camera()
        if not cam:
            return
        self._populate_resolutions(cam)
        wh = _parse_resolution(self.resolution_var.get())
        self.app.restart_camera(
            device_index=cam["index"],
            width=wh[0] if wh else None,
            height=wh[1] if wh else None,
        )

    def _current_selected_camera(self) -> dict | None:
        label = self.camera_var.get()
        for cam in self._detected:
            if _format_camera_choice(cam) == label:
                return cam
        return None

    def _populate_resolutions(self, cam: dict) -> None:
        resolutions = cam.get("resolutions") or []
        if not resolutions:
            # No resolution data yet (quick name-only enumeration). Keep
            # whatever the user has saved so the dropdown isn't empty.
            saved_w = int(self.cfg.camera.get("width", 0))
            saved_h = int(self.cfg.camera.get("height", 0))
            if saved_w and saved_h:
                self.resolution_combo["values"] = [_format_resolution((saved_w, saved_h))]
                self.resolution_var.set(_format_resolution((saved_w, saved_h)))
            else:
                self.resolution_combo["values"] = []
                self.resolution_var.set("")
            return

        labels = [_format_resolution(wh) for wh in resolutions]
        self.resolution_combo["values"] = labels

        # Default selection: saved resolution if this camera supports it,
        # otherwise the highest. resolutions is sorted ascending by pixel
        # count, so resolutions[-1] is the largest.
        saved_w = int(self.cfg.camera.get("width", 0))
        saved_h = int(self.cfg.camera.get("height", 0))
        if (saved_w, saved_h) in resolutions and cam["index"] == int(self.cfg.camera.get("device_index", 0)):
            self.resolution_var.set(_format_resolution((saved_w, saved_h)))
        else:
            self.resolution_var.set(_format_resolution(resolutions[-1]))

    # ----- Apply / Stop ------------------------------------------------------

    def apply_and_start(self) -> None:
        cam = self._current_selected_camera()
        if cam is None:
            self.app.restart_camera()
            return

        wh = _parse_resolution(self.resolution_var.get())
        self.cfg.camera["device_index"] = int(cam["index"])
        self.cfg.camera["device_chosen"] = True
        if wh is not None:
            self.cfg.camera["width"] = wh[0]
            self.cfg.camera["height"] = wh[1]
        self.cfg.save()
        self.app.restart_camera(
            device_index=int(cam["index"]),
            width=wh[0] if wh else None,
            height=wh[1] if wh else None,
        )
        self.status_label.config(
            text=f"Using {cam.get('name', '?')}" + (f" @ {self.resolution_var.get()}" if wh else "") + "."
        )

    def stop_preview(self) -> None:
        self.app.stop_camera()
        self.status_label.config(text="Preview stopped.")

    # ----- Live preview hook -------------------------------------------------

    def refresh_preview(self, frame_bgr) -> None:
        self.preview.show_bgr(frame_bgr)
