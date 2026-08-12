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


def _flush_filter(win: SerialMonitorWindow) -> None:
    """Run the debounced re-render now rather than waiting out the timer."""
    if win._filter_job is not None:
        win.after_cancel(win._filter_job)
        win._filter_job = None
    win._rerender()


def _post(app: _FakeApp, *pairs: tuple[str, str]) -> None:
    for topic, line in pairs:
        app.bus.post(topic, line)
    app.bus.drain()


def test_typing_a_filter_debounces_instead_of_rerendering_per_keystroke(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        win.filter_var.set("do")
        assert win._filter_job is not None, "no re-render was scheduled"
    finally:
        win.close()


def test_filter_hides_non_matching_lines_but_keeps_them_for_later(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        _post(app, ("serial/tx", "xf:0"), ("serial/rx", "done"), ("serial/rx", "waiting"))
        win.filter_var.set("done")
        _flush_filter(win)
        body = _body(win)
        assert "done" in body
        assert "xf:0" not in body
        assert "waiting" not in body

        # Filtering is a view, not a deletion.
        win.clear_filter()
        _flush_filter(win)
        body = _body(win)
        assert "xf:0" in body and "waiting" in body
    finally:
        win.close()


def test_filter_is_case_insensitive_and_matches_the_board_text_only(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        _post(app, ("serial/rx", "Done"))
        win.filter_var.set("dOnE")
        _flush_filter(win)
        assert "Done" in _body(win)

        # The `<-`/`->` prefixes are rendering, not content — a filter must not
        # key off them or "->" would silently mean "everything I sent".
        win.filter_var.set("<-")
        _flush_filter(win)
        assert "Done" not in _body(win)
    finally:
        win.close()


def test_direction_toggles_hide_a_whole_kind(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        _post(app, ("serial/tx", "version"), ("serial/rx", "7.2.1"), ("serial/note", "probing"))
        win.show_kind_vars["tx"].set(False)
        win._rerender()
        body = _body(win)
        assert "version" not in body
        assert "7.2.1" in body and "probing" in body
    finally:
        win.close()


def test_lines_arriving_while_a_filter_is_active_respect_it(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        win.filter_var.set("done")
        _flush_filter(win)
        _post(app, ("serial/rx", "waiting"), ("serial/rx", "done"))
        body = _body(win)
        assert "done" in body
        assert "waiting" not in body
        # …and are still there once the filter goes away.
        win.clear_filter()
        _flush_filter(win)
        assert "waiting" in _body(win)
    finally:
        win.close()


def test_dump_follows_the_filter_so_save_writes_what_is_shown(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        _post(app, ("serial/rx", "done"), ("serial/rx", "waiting"))
        win.filter_var.set("done")
        _flush_filter(win)
        dumped = win.dump()
        assert "done" in dumped
        assert "waiting" not in dumped
    finally:
        win.close()


def test_count_reports_shown_against_total_only_while_filtering(root) -> None:
    app = _FakeApp(broker=_FakeBroker())
    win = _open(root, app)
    try:
        _post(app, ("serial/rx", "done"), ("serial/rx", "waiting"), ("serial/tx", "xf:0"))
        assert win._count_var.get() == "3 lines"
        win.filter_var.set("done")
        _flush_filter(win)
        assert win._count_var.get() == "1 of 3 lines"
    finally:
        win.close()
