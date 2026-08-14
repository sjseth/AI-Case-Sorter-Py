"""Settings -> Serial page: connect/disconnect, board init settings, sort-arm
test, airdrop config.

The protocol reference is ``hardware/serial_broker.py``. This module owns the
whole connect/disconnect/refresh flow itself rather than reusing
``QtMainWindow.connect_serial``/``refresh_ports``, since those read from
widgets that don't exist on this page.

The traffic log is deliberately not duplicated here — the serial panel
(``qtui/serial_monitor.py``) already renders ``serial/rx``/``serial/tx``/
``serial/note``, and a second log on this page would be an out-of-sync copy
of its ring buffer. "Open monitor ↗" reveals that panel instead; the baud
picker is kept in step with the panel's own.

As on the other settings pages (``settings_camera.py``,
``settings_imageproc.py``): every field persists to ``Config`` on change, no
separate Save button. Board-config values fetched via "Get config from board"
persist immediately for the same reason.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..hardware import serial_broker
from ..hardware.serial_emulator import EMULATED_PORT, EmulatorBroker

BAUD_CHOICES = (9600, 19200, 38400, 57600, 115200)

# (UI label, init-settings key, min, max, default). Mirrors tab_serial.INIT_FIELDS —
# keep the two lists in lock-step; renaming a key here without renaming it there
# breaks board round-tripping between the two UIs.
INIT_FIELDS: tuple[tuple[str, str, int, int, int], ...] = (
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
)

AIRDROP_FIELDS: tuple[tuple[str, str, int, int, int], ...] = (
    ("Pre-drop delay (ms)", "airdroppredelay", 0, 1500, 50),
    ("Signal duration (ms)", "airdropdsignalduration", 0, 1500, 70),
    ("Post-drop delay (ms)", "airdroppostdelay", 0, 1500, 50),
)


class SerialSection(QWidget):
    """Connection + board init settings + sort-arm test + airdrop. State on ``self``."""

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        self._sort_to_ready = False
        self._init_widgets: dict[str, QSpinBox] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(self._build_connection_group())
        column.addWidget(self._build_sort_arm_group())
        column.addWidget(self._build_init_group())
        column.addWidget(self._build_airdrop_group())
        column.addStretch(1)

        self.refresh_ports()
        self._update_connection_state()
        # Reactive to any broker change, not just ones this page caused — the
        # window can also connect via auto-connect at startup.
        win.bus.subscribe("serial/state", lambda _payload: self._update_connection_state())
        # The monitor's IDE-style picker announces its changes here; without
        # this, a baud change over there left this combo stale (JL).
        win.bus.subscribe("serial/baud", lambda _payload: self._sync_baud_from_config())

    # ----- connection ----------------------------------------------------------

    def _build_connection_group(self) -> QGroupBox:
        box = QGroupBox("Connection", self)
        column = QVBoxLayout(box)

        ser_cfg = self._win.config.serial
        form = QFormLayout()
        self.port_combo = QComboBox(box)
        self.port_combo.setMaximumWidth(240)
        self.baud_combo = QComboBox(box)
        self.baud_combo.setMaximumWidth(240)
        self.baud_combo.addItems([str(b) for b in BAUD_CHOICES])
        saved_baud = str(int(ser_cfg.get("baud", 9600)))
        if saved_baud not in [str(b) for b in BAUD_CHOICES]:
            self.baud_combo.addItem(saved_baud)
        self.baud_combo.setCurrentText(saved_baud)
        # `activated` = user action only, so the serial/baud sync below can
        # setCurrentText without feeding back into this handler.
        self.baud_combo.activated.connect(self._on_baud_activated)
        self.probe_timeout_spin = QDoubleSpinBox(box)
        self.probe_timeout_spin.setRange(0.5, 10.0)
        self.probe_timeout_spin.setSingleStep(0.5)
        self.probe_timeout_spin.setValue(float(ser_cfg.get("handshake_timeout_s", 4.0)))
        self.probe_timeout_spin.valueChanged.connect(self._on_probe_timeout_changed)
        form.addRow("Port", self.port_combo)
        form.addRow("Baud", self.baud_combo)
        form.addRow("Probe timeout (s)", self.probe_timeout_spin)
        column.addLayout(form)

        self.init_on_startup_check = QCheckBox("Initialize these settings on startup", box)
        self.init_on_startup_check.setChecked(bool(ser_cfg.get("init_on_startup", False)))
        self.init_on_startup_check.toggled.connect(self._on_init_on_startup_toggled)
        column.addWidget(self.init_on_startup_check)

        buttons = QHBoxLayout()
        self.connect_button = QPushButton("Connect", box)
        self.connect_button.setObjectName("action")
        self.connect_button.clicked.connect(self.connect_port)
        self.disconnect_button = QPushButton("Disconnect", box)
        self.disconnect_button.setObjectName("danger")
        self.disconnect_button.clicked.connect(self.disconnect_port)
        refresh = QPushButton("Refresh ports", box)
        refresh.clicked.connect(self.refresh_ports)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(refresh)
        # Tk parity (its Serial tab had "Open monitor ↗"): the traffic log
        # lives in the bottom dock, and this page should say so — a user
        # configuring serial is exactly the one who wants to watch it (JL).
        self.monitor_button = QPushButton("Open serial monitor ↗", box)
        self.monitor_button.clicked.connect(self._open_monitor)
        buttons.addWidget(self.monitor_button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        return box

    def _open_monitor(self) -> None:
        open_monitor = getattr(self._win, "open_serial_monitor", None)
        if open_monitor is not None:
            open_monitor()

    def refresh_ports(self) -> None:
        """Emulated first, then USB/ACM/COM adapters, then the rest.

        Same ordering as ``app.py``'s (superseded) ``refresh_ports`` — a real
        board lives on a USB/ACM adapter, and Linux otherwise buries it under
        32 legacy ``/dev/ttyS*`` UARTs.
        """
        selected = self.port_combo.currentText() or (self._win.config.serial.get("port") or "").strip()
        detected = serial_broker.list_serial_ports()
        likely = [p for p in detected if "USB" in p or "ACM" in p or "COM" in p.upper()]
        ports = [EMULATED_PORT, *likely, *(p for p in detected if p not in likely)]
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if selected in ports:
            self.port_combo.setCurrentText(selected)

    def connect_port(self) -> None:
        """Open the port shown in the combo. Emulated is instant; a real port
        opens on a worker since ``try_open`` waits out the board's handshake.

        Named ``connect_port``, not ``connect`` — ``QObject.connect`` is a
        staticmethod ty flags any instance-method override of as an LSP
        violation.
        """
        win = self._win
        self._stop_current_broker()
        port = self.port_combo.currentText().strip()
        if not port:
            win.set_status("No port selected.")
            win._set_serial_indicator("Serial: no port selected", connected=False)
            return

        if port == EMULATED_PORT:
            broker: Any = EmulatorBroker()
            broker.try_open()
            self._finish_connect(broker, port)
            return

        baud = int(self.baud_combo.currentText() or win.config.serial.get("baud", 9600))
        timeout = float(self.probe_timeout_spin.value())
        broker = serial_broker.SerialBroker(
            port=port, baud=baud, require_serial_ready=True, handshake_timeout_s=timeout
        )
        # Attach before the handshake: a failed open should still leave a
        # trace in the serial dock (mirrors the auto-connect probe).
        win._attach_serial_listeners(broker)
        self.connect_button.setEnabled(False)
        win.set_status(f"Connecting to {port}…")

        def _open() -> bool:
            if not broker.try_open():
                return False
            broker.start()
            return True

        def _done(opened: bool) -> None:
            self.connect_button.setEnabled(True)
            if not opened:
                win.set_status(f"Failed to open {port}.")
                win._set_serial_indicator(f"Serial: failed to open {port}", connected=False)
                return
            self._finish_connect(broker, port)

        win.run_worker(_open, on_done=_done, on_error=self._on_connect_error)

    def _on_connect_error(self, exc: Exception) -> None:
        self.connect_button.setEnabled(True)
        self._win.set_status(f"Connect error: {exc}")

    def _finish_connect(self, broker: Any, port: str) -> None:
        # _after_connect owns the init-on-startup push (shared with auto-connect).
        self._win._after_connect(broker, port, source="manual")
        self._update_connection_state()

    def disconnect_port(self) -> None:
        win = self._win
        self._stop_current_broker()
        win._set_serial_indicator("Serial: disconnected", connected=False)
        win.set_status("Serial disconnected.")
        self._update_connection_state()

    def _stop_current_broker(self) -> None:
        win = self._win
        if win.broker is None:
            return
        try:
            if win.run_controller is not None:
                win.run_controller.stop()
            win.broker.stop()
        except Exception:
            pass
        win.broker = None
        win.run_controller = None
        update_buttons = getattr(win, "_update_run_buttons", None)
        if update_buttons is not None:
            update_buttons()

    def _on_baud_activated(self, index: int) -> None:
        """User picked a speed: persist it, tell the monitor's picker, and —
        matching its IDE-style behavior — re-open a live connection at the
        new speed rather than silently running at the old one."""
        try:
            baud = int(self.baud_combo.itemText(index))
        except (TypeError, ValueError):
            return
        if int(self._win.config.serial.get("baud", 9600) or 9600) == baud:
            return
        self._win.config.serial["baud"] = baud
        self._win.config.save()
        self._win.bus.post("serial/baud", baud)
        broker = self._win.broker
        if broker is not None and getattr(broker, "port", None):
            self._win.connect_serial(port=broker.port)

    def _sync_baud_from_config(self) -> None:
        """The config row is the truth for both baud surfaces (JL: the two
        pickers must not drift); follow it whenever anything announces it."""
        saved = str(int(self._win.config.serial.get("baud", 9600) or 9600))
        if self.baud_combo.currentText() != saved:
            if self.baud_combo.findText(saved) < 0:
                self.baud_combo.addItem(saved)
            self.baud_combo.setCurrentText(saved)

    def _update_connection_state(self) -> None:
        """Board-only actions need a live broker; everything else is offline-editable."""
        self._sync_baud_from_config()
        connected = self._win.broker is not None
        for widget in (
            self.disconnect_button,
            self.get_config_button,
            self.push_button,
            self.home_button,
            self.sort_to_spin,
        ):
            widget.setEnabled(connected)

    # ----- sort arm --------------------------------------------------------------

    def _build_sort_arm_group(self) -> QGroupBox:
        box = QGroupBox("Sort arm", self)
        grid = QGridLayout(box)
        ser_cfg = self._win.config.serial
        init_settings = ser_cfg.get("init_settings", {})

        grid.addWidget(QLabel("Slot count", box), 0, 0)
        self.slot_count_spin = QSpinBox(box)
        self.slot_count_spin.setRange(1, 64)
        self.slot_count_spin.setValue(int(ser_cfg.get("slot_quantity", 8)))
        self.slot_count_spin.valueChanged.connect(self._on_slot_count_changed)
        grid.addWidget(self.slot_count_spin, 0, 1)

        grid.addWidget(QLabel("Sort slot steps", box), 0, 2)
        self.sort_steps_spin = QSpinBox(box)
        self.sort_steps_spin.setRange(0, 9999)
        self.sort_steps_spin.setValue(int(init_settings.get("sortsteps", 20)))
        self.sort_steps_spin.valueChanged.connect(lambda v: self._on_init_value_changed("sortsteps", v))
        grid.addWidget(self.sort_steps_spin, 0, 3)

        grid.addWidget(QLabel("Sort to slot", box), 1, 0)
        self.sort_to_spin = QSpinBox(box)
        self.sort_to_spin.setRange(0, 64)
        self.sort_to_spin.setValue(0)
        grid.addWidget(self.sort_to_spin, 1, 1)
        # Connected last, like Tk's trace_add: the initial setValue above must
        # not read as the user requesting a move.
        self.sort_to_spin.valueChanged.connect(self._on_sort_to_changed)
        self._sort_to_ready = True

        self.home_button = QPushButton("Home sorter (sortto:0)", box)
        self.home_button.clicked.connect(self._home_sorter)
        grid.addWidget(self.home_button, 1, 2, 1, 2)
        return box

    def _on_slot_count_changed(self, value: int) -> None:
        self._win.config.serial["slot_quantity"] = int(value)
        self._win.config.save()

    def _on_sort_to_changed(self, value: int) -> None:
        """Every change — keyboard, arrows, or a fetched value — tests the move.

        Not persisted: this is a transient test control, not a saved setting
        (Tk's ``sort_to_var`` isn't in its tab's ``save()`` either).
        """
        if not self._sort_to_ready:
            return
        win = self._win
        broker = win.broker
        if broker is None or not getattr(broker, "is_connected", False):
            win.set_status(f"Sort to {value}: not connected.")
            return
        broker.move_sorter_to_slot(int(value))
        win.set_status(f"sortto:{value}")

    def _home_sorter(self) -> None:
        win = self._win
        broker = win.broker
        if broker is None or not getattr(broker, "is_connected", False):
            win.notify("Not connected", "Connect to the board first.")
            return
        broker.move_sorter_to_slot(0)
        # Bypass the valueChanged handler so resetting the spinbox doesn't
        # send a second, redundant sortto:0.
        self._sort_to_ready = False
        self.sort_to_spin.setValue(0)
        self._sort_to_ready = True
        win.set_status("Homed sorter (sortto:0).")

    # ----- board init settings --------------------------------------------------

    def _build_init_group(self) -> QGroupBox:
        box = QGroupBox("Board init settings", self)
        grid = QGridLayout(box)
        init_settings = self._win.config.serial.get("init_settings", {})

        for idx, (label, key, lo, hi, default) in enumerate(INIT_FIELDS):
            spin = QSpinBox(box)
            spin.setRange(lo, hi)
            spin.setValue(int(init_settings.get(key, default)))
            spin.valueChanged.connect(lambda value, k=key: self._on_init_value_changed(k, value))
            row, col = idx // 3, (idx % 3) * 2
            grid.addWidget(QLabel(label, box), row, col)
            grid.addWidget(spin, row, col + 1)
            self._init_widgets[key] = spin

        buttons = QHBoxLayout()
        self.get_config_button = QPushButton("Get config from board", box)
        self.get_config_button.clicked.connect(self.fetch_board_config)
        self.push_button = QPushButton("Push to board", box)
        self.push_button.clicked.connect(self.push_to_board)
        buttons.addWidget(self.get_config_button)
        buttons.addWidget(self.push_button)
        buttons.addStretch(1)
        last_row = (len(INIT_FIELDS) - 1) // 3 + 1
        grid.addLayout(buttons, last_row, 0, 1, 6)
        return box

    def _on_init_value_changed(self, key: str, value: int) -> None:
        settings = dict(self._win.config.serial.get("init_settings", {}))
        settings[key] = int(value)
        self._win.config.serial["init_settings"] = settings
        self._win.config.save()

    def fetch_board_config(self) -> None:
        win = self._win
        broker = win.broker
        if broker is None or not getattr(broker, "is_connected", False):
            win.notify("Not connected", "Connect to the board first.")
            return
        win.set_status("Requesting config from board…")
        win.run_worker(broker.get_config, on_done=self._apply_board_config, on_error=self._on_board_error)

    def _apply_board_config(self, payload: dict[str, Any] | None) -> None:
        win = self._win
        if not payload:
            win.set_status("Board returned no config.")
            return
        applied = 0
        for key, value in payload.items():
            if key in self._init_widgets:
                try:
                    self._init_widgets[key].setValue(int(value))
                    applied += 1
                except (TypeError, ValueError):
                    pass
            elif key == "sortsteps":
                # sortsteps lives in the Sort arm group, not _init_widgets.
                try:
                    self.sort_steps_spin.setValue(int(value))
                    applied += 1
                except (TypeError, ValueError):
                    pass
        win.set_status(f"Loaded {applied} value(s) from board.")

    def push_to_board(self) -> None:
        win = self._win
        broker = win.broker
        if broker is None or not getattr(broker, "is_connected", False):
            win.notify("Not connected", "Connect to the board first.")
            return
        win.set_status("Pushing init settings to board…")
        settings = dict(win.config.serial.get("init_settings", {}))
        win.run_worker(
            lambda: broker.update_init_settings(settings),
            on_done=lambda _r: win.set_status("Init settings pushed."),
            on_error=self._on_board_error,
        )

    def _on_board_error(self, exc: Exception) -> None:
        self._win.notify("Serial error", str(exc))

    # ----- airdrop -----------------------------------------------------------------

    def _build_airdrop_group(self) -> QGroupBox:
        box = QGroupBox("Airdrop configuration", self)
        row = QHBoxLayout(box)
        init_settings = self._win.config.serial.get("init_settings", {})

        self.airdrop_enabled_check = QCheckBox("Airdrop enabled", box)
        self.airdrop_enabled_check.setChecked(bool(int(init_settings.get("airdropenabled", 0))))
        self.airdrop_enabled_check.toggled.connect(self._on_airdrop_enabled_toggled)
        row.addWidget(self.airdrop_enabled_check)

        for label, key, lo, hi, default in AIRDROP_FIELDS:
            row.addWidget(QLabel(label, box))
            spin = QSpinBox(box)
            spin.setRange(lo, hi)
            spin.setValue(int(init_settings.get(key, default)))
            spin.valueChanged.connect(lambda value, k=key: self._on_init_value_changed(k, value))
            row.addWidget(spin)
            self._init_widgets[key] = spin
        row.addStretch(1)
        return box

    def _on_airdrop_enabled_toggled(self, checked: bool) -> None:
        self._on_init_value_changed("airdropenabled", 1 if checked else 0)

    # ----- misc persisted fields ----------------------------------------------------

    def _on_probe_timeout_changed(self, value: float) -> None:
        self._win.config.serial["handshake_timeout_s"] = float(value)
        self._win.config.save()

    def _on_init_on_startup_toggled(self, checked: bool) -> None:
        self._win.config.serial["init_on_startup"] = bool(checked)
        self._win.config.save()


def build_serial_section(win: Any) -> SerialSection:
    return SerialSection(win)
