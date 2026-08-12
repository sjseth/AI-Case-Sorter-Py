"""Serial config tab.

Each NumericField writes back into config.serial.init_settings under the exact
wire-protocol key the firmware expects.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..control.events import EventBus
from ..hardware import serial_broker
from ..hardware.serial_emulator import EMULATED_PORT
from .serial_console import BAUD_RATES, DEFAULT_BAUD, SerialConsole
from .widgets import NumericField, build_button_row

# (UI label, init-settings key, min, max, default). Defaults are the
# operator-tuned values; mirror DEFAULT_INIT_SETTINGS in config.py.
# Note: 'sortsteps' lives in the Sort arm panel above (next to slot count)
# rather than here.
INIT_FIELDS = [
    ("Feed homing offset", "feedhomingoffset", 0, 9999, 0),
    ("Sort homing offset", "sorthomingoffset", 0, 9999, 0),
    ("Feed speed", "feedspeed", 0, 255, 90),
    ("Sort speed", "sortspeed", 0, 255, 90),
    ("Feed cycle steps", "feedsteps", 0, 9999, 70),
    ("Slot drop delay (ms)", "slotdropdelay", 0, 9999, 300),
    ("Notification delay", "notificationdelay", 0, 9999, 160),
    ("Motor standby (s)", "automotorstandbytimeout", 0, 9999, 0),
    ("Feed motor current", "feedmotorcurrent", 0, 9999, 900),
    ("Sort motor current", "sortmotorcurrent", 0, 9999, 900),
    ("Case fan speed", "fan", 0, 255, 100),
    ("Debounce timeout (ms)", "debounceTimeout", 0, 9999, 500),
    ("Debounce pause (ms)", "debounceTime", 0, 9999, 300),
    ("Camera LED level", "cameraledlevel", 0, 255, 130),
]

AIRDROP_FIELDS = [
    ("Pre-drop delay (ms)", "airdroppredelay", 0, 1500, 50),
    ("Signal duration (ms)", "airdropdsignalduration", 0, 1500, 70),
    ("Post-drop delay (ms)", "airdroppostdelay", 0, 1500, 50),
]


class SerialTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        # Not `self.config` — that collides with ttk.Widget.config().
        self.cfg = config
        self.bus = bus
        self.app = app
        ser_cfg = config.serial
        init_settings = ser_cfg.get("init_settings", {})

        connect = ttk.LabelFrame(self, text="Connection")
        connect.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        ttk.Label(connect, text="Port").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        self.port_var = tk.StringVar(value=ser_cfg.get("port", ""))
        self.port_combo = ttk.Combobox(connect, textvariable=self.port_var, width=24)
        self.port_combo.grid(row=0, column=1, padx=6, pady=4, sticky=tk.W)
        ttk.Button(connect, text="Refresh ports", command=self.refresh_ports).grid(row=0, column=2, padx=6)

        ttk.Label(connect, text="Baud").grid(row=0, column=3, padx=6, pady=4, sticky=tk.W)
        # The same list the console's picker offers, not free numeric entry: a
        # rate outside it can't work (see BAUD_RATES). A value already saved by
        # the old spinbox still displays; it just can't be re-selected. The
        # console below carries its own picker — `_sync_baud` there and
        # `_refresh_baud` here keep the two showing one setting.
        self.baud_var = tk.IntVar(value=int(ser_cfg.get("baud", DEFAULT_BAUD)))
        ttk.Combobox(
            connect,
            textvariable=self.baud_var,
            values=[str(rate) for rate in BAUD_RATES],
            state="readonly",
            width=10,
        ).grid(row=0, column=4, padx=6, sticky=tk.W)

        ttk.Label(connect, text="Probe timeout (s)").grid(row=0, column=5, padx=6, pady=4, sticky=tk.W)
        self.probe_timeout_var = tk.DoubleVar(value=float(ser_cfg.get("handshake_timeout_s", 4.0)))
        ttk.Spinbox(
            connect,
            from_=0.5,
            to=10.0,
            increment=0.5,
            textvariable=self.probe_timeout_var,
            width=6,
        ).grid(row=0, column=6, padx=6, sticky=tk.W)

        self.init_on_startup_var = tk.BooleanVar(value=bool(ser_cfg.get("init_on_startup", False)))
        ttk.Checkbutton(
            connect,
            text="Initialize these settings on startup",
            variable=self.init_on_startup_var,
        ).grid(row=1, column=0, columnspan=3, padx=6, sticky=tk.W)

        build_button_row(
            connect,
            [
                ("Connect", self.connect_with_selected),
                ("Disconnect", self.app.disconnect_serial),
                ("Get config from board", self.fetch_board_config),
                ("Push to board", self.push_to_board),
                ("Save", self.save),
            ],
            primary="Connect",
        ).grid(row=2, column=0, columnspan=7, padx=4, pady=4, sticky=tk.W)

        # ---- Slot count + sort-arm test ------------------------------------
        sorter_box = ttk.LabelFrame(self, text="Sort arm")
        sorter_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        ttk.Label(sorter_box, text="Slot count").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        self.slot_count_var = tk.IntVar(value=int(ser_cfg.get("slot_quantity", 8)))
        ttk.Spinbox(
            sorter_box,
            from_=1,
            to=64,
            textvariable=self.slot_count_var,
            width=6,
        ).grid(row=0, column=1, padx=6, pady=4, sticky=tk.W)

        ttk.Label(sorter_box, text="Sort slot steps").grid(row=0, column=2, padx=6, pady=4, sticky=tk.W)
        # Pull the initial value from init_settings so the field tracks the
        # firmware param; saving here also writes back into init_settings.
        self.sort_steps_var = tk.IntVar(value=int(init_settings.get("sortsteps", 20)))
        ttk.Spinbox(
            sorter_box,
            from_=0,
            to=9999,
            textvariable=self.sort_steps_var,
            width=6,
        ).grid(row=0, column=3, padx=6, pady=4, sticky=tk.W)

        ttk.Label(sorter_box, text="Sort to slot").grid(row=1, column=0, padx=6, pady=4, sticky=tk.W)
        self.sort_to_var = tk.IntVar(value=0)
        self._sort_to_initialized = False
        ttk.Spinbox(
            sorter_box,
            from_=0,
            to=64,
            textvariable=self.sort_to_var,
            width=6,
        ).grid(row=1, column=1, padx=6, pady=4, sticky=tk.W)
        # Use trace_add so any change to the value — keyboard, arrow, paste —
        # fires sortto:N. Suppress the initial set during construction.
        self.sort_to_var.trace_add("write", lambda *_args: self._on_sort_to_changed())

        ttk.Button(
            sorter_box,
            text="Home sorter (sortto:0)",
            command=self._home_sorter,
        ).grid(row=1, column=2, padx=6, pady=4, sticky=tk.W)
        self._sort_to_initialized = True

        # ---- init-settings fields ----
        init_box = ttk.LabelFrame(self, text="Board init settings")
        init_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.init_widgets: dict[str, NumericField] = {}
        for idx, (label, key, lo, hi, dflt) in enumerate(INIT_FIELDS):
            value = int(init_settings.get(key, dflt))
            field = NumericField(init_box, label, from_=lo, to=hi, initial=value)
            field.grid(row=idx // 3, column=idx % 3, padx=6, pady=4, sticky=tk.W)
            self.init_widgets[key] = field

        # ---- airdrop ----
        airdrop = ttk.LabelFrame(self, text="Airdrop configuration")
        airdrop.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.airdrop_enabled_var = tk.BooleanVar(value=bool(int(init_settings.get("airdropenabled", 0))))
        ttk.Checkbutton(airdrop, text="Airdrop enabled", variable=self.airdrop_enabled_var).grid(
            row=0, column=0, padx=6, pady=4, sticky=tk.W
        )
        for idx, (label, key, lo, hi, dflt) in enumerate(AIRDROP_FIELDS):
            value = int(init_settings.get(key, dflt))
            field = NumericField(airdrop, label, from_=lo, to=hi, initial=value)
            field.grid(row=0, column=idx + 1, padx=6, pady=4, sticky=tk.W)
            self.init_widgets[key] = field

        # ---- monitor & debug ----
        # The same widget the detached window uses, minus its baud selector —
        # the Connection panel above already owns that setting.
        monitor = ttk.LabelFrame(self, text="Serial monitor / debug")
        monitor.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.console = SerialConsole(
            monitor,
            app=self.app,
            height=10,
            detach_command=self.app.open_serial_monitor,
            on_baud_changed=self._refresh_baud,
        )
        self.console.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.refresh_ports()

    # ----- handlers -----------------------------------------------------------

    def connect_with_selected(self) -> None:
        """Save the form first so config matches what's on screen, then connect
        to the port currently shown in the dropdown."""
        port = (self.port_var.get() or "").strip()
        if not port:
            messagebox.showerror("No port selected", "Pick a port from the dropdown (or click Refresh ports).")
            return
        self.save()
        self.app.connect_serial(port=port)

    def _refresh_baud(self) -> None:
        """Follow a speed picked in the console below."""
        try:
            self.baud_var.set(int(self.cfg.serial.get("baud", DEFAULT_BAUD)))
        except (TypeError, ValueError, tk.TclError):
            pass

    def refresh_ports(self) -> None:
        ports = serial_broker.list_serial_ports() + [EMULATED_PORT]
        self.port_combo["values"] = ports
        if self.port_var.get() not in ports and ports:
            self.port_var.set(ports[0])

    def save(self) -> None:
        self.cfg.serial["port"] = self.port_var.get()
        self.cfg.serial["baud"] = int(self.baud_var.get())
        self.cfg.serial["handshake_timeout_s"] = float(self.probe_timeout_var.get())
        self.cfg.serial["slot_quantity"] = int(self.slot_count_var.get())
        self.cfg.serial["init_on_startup"] = bool(self.init_on_startup_var.get())
        init_settings = dict(self.cfg.serial.get("init_settings", {}))
        for key, field in self.init_widgets.items():
            init_settings[key] = int(field.get())
        init_settings["airdropenabled"] = 1 if self.airdrop_enabled_var.get() else 0
        try:
            init_settings["sortsteps"] = int(self.sort_steps_var.get())
        except (tk.TclError, ValueError):
            pass
        self.cfg.serial["init_settings"] = init_settings
        self.cfg.save()
        self.app.set_status("Serial settings saved.")

    # ----- Sort arm test helpers --------------------------------------------

    def _on_sort_to_changed(self) -> None:
        if not getattr(self, "_sort_to_initialized", False):
            return
        try:
            slot = int(self.sort_to_var.get())
        except (tk.TclError, ValueError):
            return
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            self.app.set_status(f"Sort to {slot}: not connected.")
            return
        broker.move_sorter_to_slot(slot)
        self.app.set_status(f"sortto:{slot}")

    def _home_sorter(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        broker.move_sorter_to_slot(0)
        # Reset the spinbox to 0 so subsequent changes start from a known state.
        # Bypass the trace by toggling the init flag.
        self._sort_to_initialized = False
        self.sort_to_var.set(0)
        self._sort_to_initialized = True
        self.app.set_status("Homed sorter (sortto:0).")

    def push_to_board(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        self.save()
        self.app.set_status("Pushing init settings to board…")
        self.app.run_worker(
            lambda: broker.update_init_settings(self.cfg.serial["init_settings"]),
            on_done=lambda _r: self.app.set_status("Init settings pushed."),
            on_error=lambda err: messagebox.showerror("Serial error", str(err)),
        )

    def fetch_board_config(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        self.app.set_status("Requesting config from board…")
        self.app.run_worker(
            broker.get_config,
            on_done=self._apply_board_config,
            on_error=lambda err: messagebox.showerror("Serial error", str(err)),
        )

    def _apply_board_config(self, payload) -> None:
        if not payload:
            self.app.set_status("Board returned no config.")
            return
        applied = 0
        for key, value in payload.items():
            if key in self.init_widgets:
                try:
                    self.init_widgets[key].set(int(value))
                    applied += 1
                except (TypeError, ValueError):
                    pass
            elif key == "sortsteps":
                # sortsteps lives in the Sort arm panel, not init_widgets.
                try:
                    self.sort_steps_var.set(int(value))
                    applied += 1
                except (TypeError, ValueError, tk.TclError):
                    pass
        self.app.set_status(f"Loaded {applied} value(s) from board.")
