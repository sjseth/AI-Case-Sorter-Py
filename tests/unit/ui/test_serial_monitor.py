"""Serial monitor window — backlog replay, pause/resume, history, send path.

Needs a real Tk display (uses xvfb in CI); skipped where tkinter isn't
importable, matching the other UI tests.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("tkinter")

import tkinter as tk

from sorter.control.events import EventBus
from sorter.ui.serial_monitor import LINE_ENDINGS, SerialMonitorWindow
from sorter.ui.theme import apply_theme


class _FakeBroker:
    def __init__(self, *, connected: bool = True) -> None:
        self.port = "/dev/ttyFAKE"
        self.baud = 9600
        self.firmware_version = "7.2.1"
        self.is_connected = connected
        self.raw: list[str] = []

    def send_raw(self, text: str) -> None:
        self.raw.append(text)


class _FakeConfig:
    def __init__(self) -> None:
        self.serial: dict[str, Any] = {"port": "/dev/ttyFAKE", "baud": 9600}
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


class _FakeApp:
    def __init__(self, *, broker: Any = None, backlog: list | None = None) -> None:
        self.bus = EventBus()
        self.config = _FakeConfig()
        self.broker = broker
        self.serial_backlog = backlog or []
        self.reconnects: list[str] = []

    def connect_serial(self, port: str | None = None) -> None:
        self.reconnects.append(port or "")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    r.withdraw()
    apply_theme(r)
    yield r
    r.destroy()


def _open(root, app: _FakeApp) -> SerialMonitorWindow:
    win = SerialMonitorWindow(root, app=app)
    win.withdraw()
    return win


def _body(win: SerialMonitorWindow) -> str:
    return win.text.get("1.0", tk.END)


def test_bus_lines_land_in_the_log_with_direction_tags(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        app.bus.post("serial/rx", "done")
        app.bus.post("serial/tx", "xf:0")
        app.bus.drain()
        body = _body(win)
        assert "<- done" in body
        assert "-> xf:0" in body
        # RX and TX print in different palette roles, which is the whole point
        # of tagging them.
        assert win.text.tag_cget("rx", "foreground") != win.text.tag_cget("tx", "foreground")
    finally:
        win.close()


def test_backlog_is_replayed_so_a_failed_probe_is_still_readable(root) -> None:
    stamp = time.time()
    backlog = [
        ("note", stamp, "probing /dev/ttyUSB0 @ 9600…"),
        ("rx", stamp, "\x00garbage"),
        ("note", stamp, "/dev/ttyUSB0 did not handshake"),
    ]
    app = _FakeApp(backlog=backlog)
    win = _open(root, app)
    try:
        body = _body(win)
        assert "probing /dev/ttyUSB0 @ 9600…" in body
        assert "garbage" in body
        assert "did not handshake" in body
        assert "end of replay (3 earlier line(s))" in body
    finally:
        win.close()


def test_pause_holds_lines_and_resume_flushes_them_in_order(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        win.paused_var.set(True)
        app.bus.post("serial/rx", "first")
        app.bus.post("serial/rx", "second")
        app.bus.drain()
        assert "first" not in _body(win)

        win.paused_var.set(False)
        win._on_pause_toggled()
        body = _body(win)
        assert body.index("first") < body.index("second"), "held lines flush in arrival order"
    finally:
        win.close()


def test_timestamp_toggle_rerenders_what_is_already_there(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        win.append("rx", "done", stamp=time.mktime((2026, 8, 12, 13, 45, 7, 0, 0, -1)))
        assert "13:45:07" not in _body(win)
        win.timestamps_var.set(True)
        win._rerender()
        assert "13:45:07 <- done" in _body(win)
    finally:
        win.close()


def test_clear_empties_both_the_widget_and_the_dump(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        win.append("rx", "done")
        win.clear()
        assert _body(win).strip() == ""
        assert win.dump() == ""
    finally:
        win.close()


@pytest.mark.parametrize(
    ("label", "ending"),
    [("No line ending", ""), ("New Line", "\n"), ("Carriage Return", "\r"), ("Both NL & CR", "\r\n")],
)
def test_send_applies_the_selected_line_ending(root, label: str, ending: str) -> None:
    broker = _FakeBroker()
    app = _FakeApp(broker=broker)
    win = _open(root, app)
    try:
        assert LINE_ENDINGS[label] == ending
        win.ending_var.set(label)
        win.entry_var.set("getconfig")
        win.send_command()
        assert broker.raw == [f"getconfig{ending}"]
        assert win.entry_var.get() == ""
    finally:
        win.close()


def test_send_without_a_connection_says_so_instead_of_raising(root) -> None:
    app = _FakeApp(broker=_FakeBroker(connected=False))
    win = _open(root, app)
    try:
        win.entry_var.set("version")
        win.send_command()
        assert app.broker.raw == []
        assert "not connected" in _body(win)
    finally:
        win.close()


def test_up_and_down_walk_the_command_history(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        for cmd in ("version", "getconfig"):
            win.entry_var.set(cmd)
            win.send_command()
        win._history_step(-1)
        assert win.entry_var.get() == "getconfig"
        win._history_step(-1)
        assert win.entry_var.get() == "version"
        win._history_step(1)
        assert win.entry_var.get() == "getconfig"
        win._history_step(1)
        assert win.entry_var.get() == "", "past the newest entry the field clears"
    finally:
        win.close()


def test_changing_the_baud_persists_it_and_reconnects(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        win.baud_var.set("115200")
        win._on_baud_selected()
        assert app.config.serial["baud"] == 115200
        assert app.config.saved == 1
        assert app.reconnects == ["/dev/ttyFAKE"]
    finally:
        win.close()


def test_header_tracks_the_connection_state(root) -> None:
    broker = _FakeBroker()
    app = _FakeApp(broker=broker)
    win = _open(root, app)
    try:
        assert win._header_var.get() == "/dev/ttyFAKE @ 9600 — 7.2.1"
        broker.is_connected = False
        app.bus.post("serial/state", {"connected": False, "message": "Serial: disconnected"})
        app.bus.drain()
        assert "disconnected" in win._header_var.get()
    finally:
        win.close()


def test_unsubscribes_on_close(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    assert win._on_rx in app.bus._subs.get("serial/rx", [])
    win.close()
    for topic in ("serial/rx", "serial/tx", "serial/note", "serial/state"):
        assert not app.bus._subs.get(topic), f"{topic} still has a subscriber"


def test_apply_palette_repaints_the_text_tags(root) -> None:
    from sorter.ui.theme import apply_theme as _apply

    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        before = win.text.tag_cget("rx", "foreground")
        _apply(root, theme="Light")
        win.apply_palette()
        assert win.text.tag_cget("rx", "foreground") != before
    finally:
        _apply(root, theme="Dark")
        win.close()
