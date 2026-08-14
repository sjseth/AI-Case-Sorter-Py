"""The serial monitor dock: filter, direction toggles, pause/flush, save,
timestamps, line endings, command history, baud switch — and, at the
integration level, the real emulator exchange landing in it over the bus.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from sorter.hardware.serial_emulator import EMULATED_PORT
from sorter.ml import classifier
from sorter.qtui.serial_monitor import (
    LINE_ENDINGS,
    SETTING_SERIAL_ZOOM,
    TEXT_FILTER,
    ZOOM_DEFAULT,
    ZOOM_MAX,
    SerialMonitorWidget,
    build_serial_monitor,
)

from .conftest import drain_until


class FakeBroker:
    """Records everything written, like a real broker's ``send_raw`` would."""

    port = "COM-fake"
    baud = 9600
    firmware_version = "Fake-1.0"
    is_connected = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_raw(self, text: str) -> None:
        self.sent.append(text)

    def stop(self) -> None:
        pass


@pytest.fixture
def monitor(window) -> SerialMonitorWidget:
    return build_serial_monitor(window)


def post(window, topic: str, payload) -> None:
    window.bus.post(topic, payload)
    assert drain_until(window, lambda: True, timeout_s=1.0)


# ----- rendering --------------------------------------------------------------


def test_rx_tx_note_render_with_prefixes(window, monitor) -> None:
    post(window, "serial/rx", "hello")
    post(window, "serial/tx", "xf:0")
    post(window, "serial/note", "probing…")

    text = monitor.output.toPlainText()
    assert "<- hello" in text
    assert "-> xf:0" in text
    assert "-- probing…" in text


def test_timestamps_toggle_changes_rendering_not_lines(window, monitor) -> None:
    post(window, "serial/rx", "hello")
    lines_before = list(monitor._lines)

    monitor.timestamps_check.setChecked(True)

    assert list(monitor._lines) == lines_before
    stamped = monitor.output.toPlainText().strip()
    assert re.match(r"^\d{2}:\d{2}:\d{2} <- hello$", stamped)


# ----- zoom -----------------------------------------------------------------


def test_zoom_defaults_to_100_percent(window, monitor) -> None:
    assert monitor.zoom_slider.value() == ZOOM_DEFAULT
    assert monitor.zoom_value_label.text() == "100%"
    assert monitor.output.font().pointSizeF() == pytest.approx(monitor._base_output_pt)


def test_slider_drag_updates_label_live_without_applying(window, monitor) -> None:
    """``sliderMoved`` (drag) only updates the label; the font resize waits for
    ``valueChanged`` (drop), per ``setTracking(False)`` — same split as the
    history view's tile-zoom slider."""
    before = monitor.output.font().pointSizeF()

    monitor.zoom_slider.sliderMoved.emit(175)

    assert monitor.zoom_value_label.text() == "175%"
    assert monitor.output.font().pointSizeF() == pytest.approx(before)


def test_zoom_scales_output_and_command_font_proportionally(window, monitor) -> None:
    base_output = monitor._base_output_pt
    base_command = monitor._base_command_pt

    monitor.zoom_slider.setValue(200)

    assert monitor.output.font().pointSizeF() == pytest.approx(base_output * 2.0)
    assert monitor.command_edit.font().pointSizeF() == pytest.approx(base_command * 2.0)
    assert monitor.zoom_value_label.text() == "200%"


def test_zoom_persists_and_restores_across_a_rebuilt_monitor(window, config) -> None:
    first = build_serial_monitor(window)
    first.zoom_slider.setValue(150)

    from sorter.data.repository import SettingsRepo

    assert SettingsRepo(config.db).get(SETTING_SERIAL_ZOOM) == "150"

    second = build_serial_monitor(window)

    assert second.zoom_slider.value() == 150
    assert second.output.font().pointSizeF() == pytest.approx(second._base_output_pt * 1.5)


def test_palette_reapplication_preserves_the_zoomed_font(window, monitor) -> None:
    monitor.zoom_slider.setValue(ZOOM_MAX)
    zoomed_pt = monitor.output.font().pointSizeF()

    window.set_theme("Comic Book")
    monitor.apply_palette()

    assert monitor.output.font().pointSizeF() == pytest.approx(zoomed_pt)


# ----- filtering ----------------------------------------------------------


def test_filter_narrows_on_rerender(window, monitor) -> None:
    post(window, "serial/rx", "hello world")
    post(window, "serial/tx", "goodbye")

    monitor.filter_edit.setText("hello")
    monitor._rerender()  # bypass the debounce timer for a deterministic test

    text = monitor.output.toPlainText()
    assert "hello world" in text
    assert "goodbye" not in text
    assert monitor._lines[-1][2] == "goodbye"  # hidden, not deleted


def test_filter_narrows_live_for_new_lines(window, monitor) -> None:
    monitor.filter_edit.setText("wanted")
    monitor._rerender()

    post(window, "serial/rx", "not this one")
    post(window, "serial/rx", "wanted line")

    text = monitor.output.toPlainText()
    assert "wanted line" in text
    assert "not this one" not in text


def test_filter_matches_board_text_not_rendered_prefix(window, monitor) -> None:
    post(window, "serial/rx", "plain payload")

    monitor.filter_edit.setText("<-")
    monitor._rerender()

    assert monitor.output.toPlainText().strip() == ""


def test_direction_toggles_hide_without_deleting(window, monitor) -> None:
    post(window, "serial/rx", "rx line")
    post(window, "serial/tx", "tx line")

    monitor.show_kind_checks["rx"].setChecked(False)  # toggled -> auto rerender

    text = monitor.output.toPlainText()
    assert "rx line" not in text
    assert "tx line" in text
    assert any(k == "rx" for k, _s, _l in monitor._lines)


# ----- pause / resume -------------------------------------------------------


def test_pause_holds_and_resume_flushes_in_order(window, monitor) -> None:
    monitor.pause_check.setChecked(True)
    post(window, "serial/rx", "a")
    post(window, "serial/rx", "b")

    assert monitor.output.toPlainText().strip() == ""
    assert len(monitor._lines) == 0

    monitor.pause_check.setChecked(False)

    lines = [ln for ln in monitor.output.toPlainText().splitlines() if ln]
    assert lines == ["<- a", "<- b"]


# ----- save -----------------------------------------------------------------


def test_text_filter_label_survives_gnomes_paren_stripping() -> None:
    # GNOME's portal strips the "(*.txt)" Qt-pattern suffix from the label it
    # shows, so the human-readable half must carry its own, un-strippable
    # mention of the extension (JL live-testing).
    assert "*.txt" in TEXT_FILTER.split("(", 1)[0]
    assert TEXT_FILTER.endswith("(*.txt)")


def test_save_writes_the_filtered_view(window, monitor, tmp_path: Path, monkeypatch) -> None:
    post(window, "serial/rx", "keep me")
    post(window, "serial/tx", "drop me")
    monitor.filter_edit.setText("keep")
    monitor._rerender()

    out_path = tmp_path / "log.txt"
    monkeypatch.setattr(monitor, "_choose_save_path", lambda: str(out_path))

    monitor.save_to_file()

    saved = out_path.read_text(encoding="utf-8")
    assert "keep me" in saved
    assert "drop me" not in saved
    assert saved == monitor.dump()


def test_save_cancelled_writes_nothing(window, monitor, tmp_path: Path, monkeypatch) -> None:
    out_path = tmp_path / "never.txt"
    monkeypatch.setattr(monitor, "_choose_save_path", lambda: "")

    monitor.save_to_file()

    assert not out_path.exists()


# ----- command send + history -----------------------------------------------


def test_send_command_uses_each_line_ending(window, monitor) -> None:
    broker = FakeBroker()
    window.broker = broker

    for label, ending in LINE_ENDINGS.items():
        monitor.command_edit.setText("ping")
        monitor.ending_combo.setCurrentText(label)
        monitor.send_command()
        assert broker.sent[-1] == "ping" + ending
        assert monitor.command_edit.text() == ""


def test_send_command_without_a_broker_notes_and_does_not_raise(window, monitor) -> None:
    window.broker = None
    monitor.command_edit.setText("ping")

    monitor.send_command()

    assert "not connected" in monitor.output.toPlainText()


def test_command_history_recall_with_up_and_down(window, monitor) -> None:
    window.broker = FakeBroker()
    for cmd in ("first", "second", "third"):
        monitor.command_edit.setText(cmd)
        monitor.send_command()

    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Up)
    assert monitor.command_edit.text() == "third"
    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Up)
    assert monitor.command_edit.text() == "second"
    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Up)
    assert monitor.command_edit.text() == "first"
    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Up)  # past the oldest: stays put
    assert monitor.command_edit.text() == "first"
    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Down)
    assert monitor.command_edit.text() == "second"
    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Down)
    assert monitor.command_edit.text() == "third"
    QTest.keyClick(monitor.command_edit, Qt.Key.Key_Down)  # past the newest: clears
    assert monitor.command_edit.text() == ""


# ----- baud ------------------------------------------------------------------


def test_baud_change_persists_and_reconnects(window, monitor, config, monkeypatch) -> None:
    from sorter.data.config import Config

    calls: list[str | None] = []
    monkeypatch.setattr(window, "connect_serial", lambda port=None: calls.append(port))
    window.broker = FakeBroker()
    window.config.serial["port"] = "COM-fake"

    index = monitor.baud_combo.findText("19200")
    assert index >= 0
    monitor.baud_combo.setCurrentIndex(index)
    monitor.baud_combo.activated.emit(index)

    assert Config(config.db).load().serial["baud"] == 19200
    assert calls == ["COM-fake"]


def test_baud_unchanged_does_not_reconnect(window, monitor, monkeypatch) -> None:
    calls: list[str | None] = []
    monkeypatch.setattr(window, "connect_serial", lambda port=None: calls.append(port))
    # A connected broker already at the configured baud is what makes the
    # short-circuit in `_on_baud_changed` fire — with no broker at all it
    # would fall through (no live speed to compare against) and reconnect
    # regardless, per the Tk original's `_on_baud_selected`.
    window.broker = FakeBroker()  # baud=9600, matching the fresh config's default
    current = str(monitor._configured_baud())
    index = monitor.baud_combo.findText(current)

    monitor.baud_combo.activated.emit(index)

    assert calls == []


# ----- integration: real emulator exchange lands in the widget --------------


def test_manual_feed_exchange_appears_in_the_widget(window, config, monkeypatch) -> None:
    config.add_headstamp("9mm FC", 2)
    monkeypatch.setattr(classifier, "classify_active", lambda *a, **k: ("9mm FC", 99.0))
    window.camera = types.SimpleNamespace(
        capture_frame=lambda: np.zeros((480, 640, 3), np.uint8),
        latest_frame=lambda: None,
        stop=lambda: None,
    )
    monitor = build_serial_monitor(window)
    window.connect_serial(EMULATED_PORT)

    window.action_buttons["Manual feed"].click()

    assert drain_until(window, lambda: window.slot_grid.cards[2].count_label.text() == "1")
    log = monitor.output.toPlainText()
    assert "-> xf:0" in log
    assert "<- done" in log
