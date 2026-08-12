"""Serial monitor window — the connection header and the lifecycle.

Everything below the header is `SerialConsole`, covered once in
`test_serial_console.py` rather than twice here.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("tkinter")

from sorter.ui.serial_monitor import SerialMonitorWindow

from .conftest import FakeApp, FakeBroker


def _open(root, app: FakeApp) -> SerialMonitorWindow:
    win = SerialMonitorWindow(root, app=app)
    win.withdraw()
    return win


def test_the_window_is_a_console_plus_a_header(root) -> None:
    app = FakeApp(broker=FakeBroker())
    win = _open(root, app)
    try:
        app.bus.post("serial/rx", "done")
        app.bus.drain()
        assert "<- done" in win.console.text.get("1.0", "end")
        # Only the detached window carries the speed selector.
        assert win.console.baud_var.get() == "9600"
    finally:
        win.close()


def test_backlog_is_replayed_so_a_failed_probe_is_still_readable(root) -> None:
    stamp = time.time()
    app = FakeApp(backlog=[("note", stamp, "/dev/ttyUSB0 did not handshake")])
    win = _open(root, app)
    try:
        assert "did not handshake" in win.console.text.get("1.0", "end")
    finally:
        win.close()


def test_header_tracks_the_connection_state(root) -> None:
    broker = FakeBroker()
    app = FakeApp(broker=broker)
    win = _open(root, app)
    try:
        assert win._header_var.get() == "/dev/ttyFAKE @ 9600 — 7.2.1"
        broker.is_connected = False
        app.bus.post("serial/state", {"connected": False, "message": "Serial: disconnected"})
        app.bus.drain()
        assert "disconnected" in win._header_var.get()
    finally:
        win.close()


def test_changing_the_baud_repaints_the_header(root) -> None:
    broker = FakeBroker()
    app = FakeApp(broker=broker)
    win = _open(root, app)
    try:
        broker.baud = 115200
        win.console.baud_var.set("115200")
        win.console._on_baud_selected()
        assert "@ 115200" in win._header_var.get()
    finally:
        win.close()


def test_apply_palette_reaches_the_console(root) -> None:
    from sorter.ui.theme import apply_theme

    app = FakeApp(broker=FakeBroker())
    win = _open(root, app)
    try:
        before = win.console.text.tag_cget("rx", "foreground")
        apply_theme(root, theme="Light")
        win.apply_palette()
        assert win.console.text.tag_cget("rx", "foreground") != before
    finally:
        apply_theme(root, theme="Dark")
        win.close()


def test_unsubscribes_everything_on_close(root) -> None:
    app = FakeApp(broker=FakeBroker())
    win = _open(root, app)
    assert win._on_state in app.bus._subs.get("serial/state", [])
    win.close()
    for topic in ("serial/rx", "serial/tx", "serial/note", "serial/state"):
        assert not app.bus._subs.get(topic), f"{topic} still has a subscriber"
