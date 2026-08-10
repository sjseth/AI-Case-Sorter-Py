"""Image processing tab — Hough configuration + primer mask + live test.

Note: the line-scan crop strategy is currently hidden in the UI but still
present in sorter.hardware.image_proc (LineScanParams, linescan_crop). To bring it
back, restore the strategy radio + line-scan parameters section here and
uncomment the dispatch in sorter.hardware.image_proc.crop_headstamp.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..control.events import EventBus
from ..hardware import image_proc
from .widgets import ImagePanel, NumericField, build_button_row


class ImageProcTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        # Not `self.config` — that collides with ttk.Widget.config().
        self.cfg = config
        self.bus = bus
        self.app = app
        ip = config.image_proc

        # ----- HoughCircles configuration ------------------------------------
        hough = ttk.LabelFrame(self, text="Configuration")
        hough.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        h = ip.get("hough", {})

        self.hough_dp = NumericField(
            hough,
            "Accumulator scale (dp)",
            from_=1,
            to=10,
            increment=0.5,
            initial=float(h.get("dp", 2.0)),
            is_float=True,
        )
        self.hough_min_dist = NumericField(
            hough,
            "Min center separation (px)",
            from_=1,
            to=4000,
            initial=int(h.get("min_dist", 500)),
        )
        self.hough_p1 = NumericField(
            hough,
            "Edge strength (param1)",
            from_=1,
            to=500,
            initial=int(h.get("param1", 100)),
        )
        self.hough_p2 = NumericField(
            hough,
            "Detection threshold (param2)",
            from_=1,
            to=500,
            initial=int(h.get("param2", 60)),
        )
        self.hough_min_r = NumericField(
            hough,
            "Min case radius (px)",
            from_=1,
            to=4000,
            initial=int(h.get("min_radius", 150)),
        )
        self.hough_max_r = NumericField(
            hough,
            "Max case radius (px)",
            from_=1,
            to=4000,
            initial=int(h.get("max_radius", 250)),
        )
        for idx, w in enumerate(
            (
                self.hough_dp,
                self.hough_min_dist,
                self.hough_p1,
                self.hough_p2,
                self.hough_min_r,
                self.hough_max_r,
            )
        ):
            w.grid(row=idx // 3, column=idx % 3, padx=6, pady=6, sticky=tk.W)

        # ----- Primer mask ---------------------------------------------------
        primer = ttk.LabelFrame(self, text="Primer mask")
        primer.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.primer_mode_var = tk.StringVar(value=ip.get("primer_mode", "hide"))
        ttk.Radiobutton(
            primer,
            text="None",
            variable=self.primer_mode_var,
            value="none",
        ).pack(side=tk.LEFT, padx=8, pady=4)
        ttk.Radiobutton(
            primer,
            text="Keep primer area only",
            variable=self.primer_mode_var,
            value="use",
        ).pack(side=tk.LEFT, padx=8, pady=4)
        ttk.Radiobutton(
            primer,
            text="Hide primer",
            variable=self.primer_mode_var,
            value="hide",
        ).pack(side=tk.LEFT, padx=8, pady=4)
        self.primer_radius = NumericField(
            primer,
            "Primer radius (px)",
            from_=1,
            to=240,
            initial=int(ip.get("primer_radius", 135)),
        )
        self.primer_radius.pack(side=tk.LEFT, padx=12, pady=4)

        # ----- Camera LED brightness (debounced live send) -------------------
        led_box = ttk.LabelFrame(self, text="Camera LED brightness")
        led_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        init_settings = config.serial.get("init_settings", {})
        initial_led = int(init_settings.get("cameraledlevel", 130))

        # IMPORTANT: state used by the Scale command callback must exist before
        # we call self.led_scale.set(...) — setting the scale triggers the
        # command, which would otherwise hit attributes that don't exist yet.
        self.led_value_var = tk.StringVar(value=str(initial_led))
        self._led_pending_after_id: str | None = None
        self._led_last_sent: int | None = None

        self.led_scale = ttk.Scale(
            led_box,
            from_=1,
            to=255,
            orient=tk.HORIZONTAL,
            length=220,
            command=self._on_led_changed,
        )
        self.led_scale.set(initial_led)
        self.led_scale.pack(side=tk.TOP, anchor=tk.W, padx=8, pady=(8, 2))

        row = ttk.Frame(led_box)
        row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 8))
        ttk.Label(row, text="Value").pack(side=tk.LEFT)
        ttk.Label(row, textvariable=self.led_value_var, style="Accent.TLabel").pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="(sends cameraledlevel:N once idle for 500 ms)", style="Subtle.TLabel").pack(
            side=tk.LEFT, padx=10
        )

        # ----- Actions + previews -------------------------------------------
        build_button_row(
            self,
            [
                ("Save", self.save),
                ("Capture Image", self.test_on_frame),
            ],
            primary="Capture Image",
        ).pack(side=tk.TOP, anchor=tk.W, padx=8, pady=4)

        preview_row = ttk.Frame(self)
        preview_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)
        # Don't name these `before`/`after` — `after` is a tk.Widget method
        # used for scheduling (we rely on it for the LED debounce below).
        self.before_panel = ImagePanel(preview_row, width=320, height=240)
        self.before_panel.pack(side=tk.LEFT, padx=8)
        ttk.Label(preview_row, text="→").pack(side=tk.LEFT)
        self.after_panel = ImagePanel(preview_row, width=320, height=320)
        self.after_panel.pack(side=tk.LEFT, padx=8)

    # ----- handlers ----------------------------------------------------------

    def save(self) -> None:
        # Strategy is locked to "hough" while the line-scan UI is hidden.
        self.cfg.image_proc["strategy"] = "hough"
        self.cfg.image_proc["primer_mode"] = self.primer_mode_var.get()
        self.cfg.image_proc["primer_radius"] = int(self.primer_radius.get())
        self.cfg.image_proc["hough"] = {
            "dp": float(self.hough_dp.get()),
            "min_dist": int(self.hough_min_dist.get()),
            "param1": float(self.hough_p1.get()),
            "param2": float(self.hough_p2.get()),
            "min_radius": int(self.hough_min_r.get()),
            "max_radius": int(self.hough_max_r.get()),
        }
        # Preserve line-scan params in config even though the UI doesn't expose
        # them, so they survive a save+reload if we ever bring the UI back.
        self.cfg.save()
        self.app.set_status("Image-processing settings saved.")

    def test_on_frame(self) -> None:
        frame = self.app.capture_frame()
        if frame is None:
            self.app.set_status("No camera frame available.")
            return
        self.save()
        cfg = self.cfg.image_proc
        # Overlay the detected circle on the source preview so the operator
        # can see exactly what got picked while tuning the parameters.
        detection = image_proc.hough_detect(frame, image_proc.HoughParams.from_dict(cfg.get("hough", {})))
        preview = image_proc.overlay_detection(frame, detection)
        if detection is None:
            self.app.set_status("No circle detected within radius bounds.")
        else:
            cx, cy, r = detection
            self.app.set_status(f"Detected circle: r={r:.0f} px at ({cx:.0f}, {cy:.0f}).")
        cropped = image_proc.crop_headstamp(frame, cfg)
        cropped = image_proc.apply_primer_mask(cropped, self.primer_mode_var.get(), int(self.primer_radius.get()))
        self.before_panel.show_bgr(preview)
        self.after_panel.show_bgr(cropped)

    # ----- Camera LED brightness debounce -----------------------------------

    def _on_led_changed(self, value: str) -> None:
        try:
            val = int(round(float(value)))
        except (TypeError, ValueError):
            return
        val = max(1, min(255, val))
        self.led_value_var.set(str(val))
        if self._led_pending_after_id is not None:
            try:
                self.after_cancel(self._led_pending_after_id)
            except (tk.TclError, ValueError):
                pass
        self._led_pending_after_id = self.after(500, lambda v=val: self._apply_led(v))

    def _apply_led(self, value: int) -> None:
        self._led_pending_after_id = None
        if self._led_last_sent == value:
            return
        self._led_last_sent = value
        # Persist immediately so a restart picks up the same brightness.
        init_settings = dict(self.cfg.serial.get("init_settings", {}))
        init_settings["cameraledlevel"] = value
        self.cfg.serial["init_settings"] = init_settings
        self.cfg.save()
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            self.app.set_status(f"Camera LED level = {value} (saved; not connected).")
            return
        try:
            broker.send_command(f"cameraledlevel:{value}")
            self.app.set_status(f"Camera LED level → {value}.")
            # Give the LED + camera time to settle (one frame at 20 fps + buffer),
            # then re-run the capture so the operator sees the new brightness.
            self.after(200, self.test_on_frame)
        except Exception as exc:
            self.app.set_status(f"LED send failed: {exc}")
