"""Settings -> Serial page: connection, board init settings, sort-arm test, airdrop.

Offscreen, against the real SQLite-backed ``Config`` from the shared UI
conftest. The connect/disconnect/get-config/push/sort-arm-test flows run
against a REAL ``EmulatorBroker`` (the same code path a physical board's
``SerialBroker`` takes) rather than a hand-rolled fake, so the wiring between
``build_serial_section`` and the broker's actual protocol methods is what's
under test, not a stand-in's guess at that protocol.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from sorter.data.config import Config
from sorter.hardware.serial_emulator import EMULATED_PORT, EmulatorBroker
from sorter.ui import settings_serial
from sorter.ui.settings_serial import (
    AIRDROP_FIELDS,
    INIT_FIELDS,
    build_serial_section,
)

from .conftest import drain_until


class _Notifier:
    """Stand-in for ``win.notify`` — a real QMessageBox would hang offscreen."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))


def _select_emulated(section) -> None:
    section.port_combo.setCurrentIndex(section.port_combo.findText(EMULATED_PORT))


def _connect_emulated(window, section) -> None:
    _select_emulated(section)
    section.connect_port()
    assert window.broker is not None


# ----- widgets reflect config defaults -----------------------------------------


def test_init_fields_reflect_config_defaults(window) -> None:
    section = build_serial_section(window)

    for _label, key, _lo, _hi, default in INIT_FIELDS:
        assert section._init_widgets[key].value() == default, key


def test_airdrop_fields_reflect_config_defaults(window) -> None:
    section = build_serial_section(window)

    assert section.airdrop_enabled_check.isChecked() is False
    for _label, key, _lo, _hi, default in AIRDROP_FIELDS:
        assert section._init_widgets[key].value() == default, key


def test_connection_and_sort_arm_fields_reflect_config_defaults(window) -> None:
    section = build_serial_section(window)

    assert section.baud_combo.currentText() == "9600"
    assert section.probe_timeout_spin.value() == pytest.approx(4.0)
    assert section.init_on_startup_check.isChecked() is False
    assert section.slot_count_spin.value() == 8
    assert section.sort_steps_spin.value() == 20
    assert section.sort_to_spin.value() == 0


# ----- edits persist -------------------------------------------------------------


def test_editing_an_init_field_persists_and_reads_back(window, config) -> None:
    section = build_serial_section(window)

    section._init_widgets["feedspeed"].setValue(123)

    assert window.config.serial["init_settings"]["feedspeed"] == 123
    assert Config(config.db).load().serial["init_settings"]["feedspeed"] == 123


def test_editing_an_airdrop_field_persists_and_reads_back(window, config) -> None:
    section = build_serial_section(window)

    section._init_widgets["airdroppredelay"].setValue(99)
    section.airdrop_enabled_check.setChecked(True)

    reloaded = Config(config.db).load().serial["init_settings"]
    assert reloaded["airdroppredelay"] == 99
    assert reloaded["airdropenabled"] == 1


def test_editing_sort_steps_persists_into_init_settings(window, config) -> None:
    section = build_serial_section(window)

    section.sort_steps_spin.setValue(55)

    assert Config(config.db).load().serial["init_settings"]["sortsteps"] == 55


def test_editing_slot_count_persists_as_a_direct_serial_key(window, config) -> None:
    section = build_serial_section(window)

    section.slot_count_spin.setValue(12)

    assert Config(config.db).load().serial["slot_quantity"] == 12


def test_editing_probe_timeout_persists(window, config) -> None:
    section = build_serial_section(window)

    section.probe_timeout_spin.setValue(6.5)

    assert Config(config.db).load().serial["handshake_timeout_s"] == pytest.approx(6.5)


def test_toggling_init_on_startup_persists(window, config) -> None:
    section = build_serial_section(window)

    section.init_on_startup_check.setChecked(True)

    assert Config(config.db).load().serial["init_on_startup"] is True


def test_sort_to_slot_change_is_not_persisted(window, config) -> None:
    """Transient test control, not a saved setting."""
    section = build_serial_section(window)

    section.sort_to_spin.setValue(7)

    assert "sort_to" not in Config(config.db).load().serial


# ----- port ordering -------------------------------------------------------------


def test_port_combo_pins_emulated_first_then_usb_acm_then_the_rest(window, monkeypatch) -> None:
    monkeypatch.setattr(
        settings_serial.serial_broker,
        "list_serial_ports",
        lambda: ["/dev/ttyS0", "/dev/ttyUSB0", "/dev/ttyACM0", "COM5"],
    )

    section = build_serial_section(window)

    ports = [section.port_combo.itemText(i) for i in range(section.port_combo.count())]
    assert ports == [EMULATED_PORT, "/dev/ttyUSB0", "/dev/ttyACM0", "COM5", "/dev/ttyS0"]


def test_refresh_ports_keeps_the_current_selection_if_still_present(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_serial.serial_broker, "list_serial_ports", lambda: ["/dev/ttyUSB0"])
    section = build_serial_section(window)
    section.port_combo.setCurrentText("/dev/ttyUSB0")

    monkeypatch.setattr(settings_serial.serial_broker, "list_serial_ports", lambda: ["/dev/ttyUSB1", "/dev/ttyUSB0"])
    section.refresh_ports()

    assert section.port_combo.currentText() == "/dev/ttyUSB0"


# ----- disabled without a broker --------------------------------------------------


def test_board_only_controls_disabled_without_a_broker(window) -> None:
    section = build_serial_section(window)

    assert window.broker is None
    assert section.connect_button.isEnabled()  # always available — it's how you get a broker
    assert not section.disconnect_button.isEnabled()
    assert not section.get_config_button.isEnabled()
    assert not section.push_button.isEnabled()
    assert not section.home_button.isEnabled()
    assert not section.sort_to_spin.isEnabled()


def test_offline_editable_controls_stay_enabled_without_a_broker(window) -> None:
    section = build_serial_section(window)

    assert section.port_combo.isEnabled()
    assert section.baud_combo.isEnabled()
    assert section.probe_timeout_spin.isEnabled()
    assert section.init_on_startup_check.isEnabled()
    assert section.slot_count_spin.isEnabled()
    assert section.sort_steps_spin.isEnabled()
    for _label, key, _lo, _hi, _default in INIT_FIELDS:
        assert section._init_widgets[key].isEnabled(), key


def test_board_only_actions_without_a_broker_notify_instead_of_crashing(window) -> None:
    window.notify = _Notifier()
    section = build_serial_section(window)

    section.fetch_board_config()
    section.push_to_board()
    section._home_sorter()

    assert len(window.notify.calls) == 3
    assert all(title == "Not connected" for title, _ in window.notify.calls)


def test_sort_to_change_without_a_broker_reports_status_not_a_crash(window) -> None:
    section = build_serial_section(window)

    section._on_sort_to_changed(3)

    assert "not connected" in window.statusBar().currentMessage()


# ----- connect / disconnect, against the real EmulatorBroker ----------------------


def test_connect_to_emulated_port_end_to_end(window, config) -> None:
    section = build_serial_section(window)

    _connect_emulated(window, section)

    assert drain_until(window, lambda: window._serial_state[1] is True)
    assert isinstance(window.broker, EmulatorBroker)
    assert window.config.serial["port"] == EMULATED_PORT
    assert Config(config.db).load().serial["port"] == EMULATED_PORT
    assert section.disconnect_button.isEnabled()
    assert section.get_config_button.isEnabled()
    assert section.push_button.isEnabled()
    assert section.home_button.isEnabled()
    assert section.sort_to_spin.isEnabled()


def test_disconnect_stops_the_broker_and_returns_to_disconnected(window) -> None:
    section = build_serial_section(window)
    _connect_emulated(window, section)

    section.disconnect_port()

    assert window.broker is None
    assert window.run_controller is None
    assert window._serial_state[1] is False
    assert "disconnected" in window._serial_state[0].lower()
    assert not section.disconnect_button.isEnabled()
    assert not section.sort_to_spin.isEnabled()


def test_connect_with_no_port_selected_reports_status(window) -> None:
    section = build_serial_section(window)
    section.port_combo.clear()

    section.connect_port()

    assert window.broker is None
    assert "no port selected" in window._serial_state[0].lower()


def test_reactive_to_a_broker_change_made_outside_this_page(window) -> None:
    """E.g. the window's own auto-connect at startup, not this page's Connect."""
    section = build_serial_section(window)
    assert not section.disconnect_button.isEnabled()

    window.broker = object()
    window._set_serial_indicator("Serial: connected (auto)", connected=True)

    assert drain_until(window, lambda: section.disconnect_button.isEnabled())


def test_init_on_startup_pushes_settings_after_connect(window, monkeypatch) -> None:
    """The shell's ``_after_connect`` doesn't push the board settings —
    this page owns that half of the flow itself."""
    recorded: list[dict] = []
    original = EmulatorBroker.update_init_settings

    def _spy(self, settings):
        recorded.append(dict(settings))
        return original(self, settings)

    monkeypatch.setattr(EmulatorBroker, "update_init_settings", _spy)
    section = build_serial_section(window)
    section.init_on_startup_check.setChecked(True)
    _select_emulated(section)

    section.connect_port()

    assert drain_until(window, lambda: len(recorded) == 1)
    assert recorded[0]["feedspeed"] == 90


# ----- get config from board, against the real emulator ---------------------------


def test_fetch_board_config_applies_matching_keys(window) -> None:
    section = build_serial_section(window)
    _connect_emulated(window, section)

    section.fetch_board_config()

    # Emulator's canned getconfig reply: {"feedspeed":60,"sortspeed":70,"slotquantity":6}.
    assert drain_until(window, lambda: section._init_widgets["feedspeed"].value() == 60)
    assert section._init_widgets["sortspeed"].value() == 70
    # "slotquantity" matches nothing in _init_widgets, so it is ignored.
    assert section.slot_count_spin.value() == 8
    assert "Loaded 2 value(s)" in window.statusBar().currentMessage()


def test_fetch_board_config_persists_the_applied_values(window, config) -> None:
    section = build_serial_section(window)
    _connect_emulated(window, section)

    section.fetch_board_config()

    assert drain_until(window, lambda: Config(config.db).load().serial["init_settings"]["feedspeed"] == 60)


# ----- push to board, against the real emulator ------------------------------------


def test_push_to_board_sends_every_init_setting(window) -> None:
    section = build_serial_section(window)
    _connect_emulated(window, section)
    sent: list[str] = []
    window.broker.on_sent.append(sent.append)

    section.push_to_board()

    assert drain_until(window, lambda: "Init settings pushed." in window.statusBar().currentMessage())
    assert "feedspeed:90" in sent
    assert "cameraledlevel:130" in sent
    assert "airdropenabled:0" in sent
    assert "airdroppredelay:50" in sent


# ----- sort-arm test, against the real emulator ------------------------------------


def test_sort_to_slot_sends_the_sortto_command(window) -> None:
    section = build_serial_section(window)
    _connect_emulated(window, section)
    sent: list[str] = []
    window.broker.on_sent.append(sent.append)

    section.sort_to_spin.setValue(3)

    assert drain_until(window, lambda: "sortto:3" in sent)


def test_home_sorter_sends_sortto_zero_and_resets_the_spinbox(window) -> None:
    section = build_serial_section(window)
    _connect_emulated(window, section)
    section.sort_to_spin.setValue(5)
    sent: list[str] = []
    window.broker.on_sent.append(sent.append)

    section._home_sorter()

    assert drain_until(window, lambda: "sortto:0" in sent)
    assert section.sort_to_spin.value() == 0
    # Resetting the spinbox must not fire a second, redundant sortto:0.
    assert sent.count("sortto:0") == 1


# ----- the monitor is one click away, and the two baud pickers agree ----------


def test_the_page_opens_the_serial_monitor_dock(window) -> None:
    """A user configuring
    serial is exactly the one who wants to watch the traffic (JL)."""
    section = build_serial_section(window)
    window.serial_dock.toggleView(False)

    section.monitor_button.click()

    assert not window.serial_dock.isClosed()


def test_a_monitor_baud_change_reaches_the_settings_combo(window, config) -> None:
    """Two surfaces show the baud (this page and the monitor's IDE-style
    picker); the config row is the truth and neither may go stale (JL) —
    including while DISCONNECTED, when no serial/state ever fires."""
    section = build_serial_section(window)
    assert section.baud_combo.currentText() == "9600"

    # Exactly what the monitor's picker does while disconnected.
    monitor = window.serial_monitor
    index = monitor.baud_combo.findText("115200")
    monitor.baud_combo.setCurrentIndex(index)
    monitor._on_baud_changed("115200")
    window.bus.drain()

    assert config.serial["baud"] == 115200
    assert section.baud_combo.currentText() == "115200"


def test_a_settings_baud_change_reaches_the_monitor_picker(window, config) -> None:
    """...and the other direction (JL: 'and vice versa')."""
    section = build_serial_section(window)
    monitor = window.serial_monitor
    assert monitor.baud_combo.currentText() == "9600"

    index = section.baud_combo.findText("57600")
    section.baud_combo.setCurrentIndex(index)
    section._on_baud_activated(index)
    window.bus.drain()

    assert config.serial["baud"] == 57600
    assert monitor.baud_combo.currentText() == "57600"
