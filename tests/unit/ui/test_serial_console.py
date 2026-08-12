"""Serial console — rendering, filtering, pause/resume, history, send path.

The same widget the Serial Config tab embeds and the monitor window wraps, so
these cases cover both; `test_serial_monitor.py` only covers what the window
adds on top. Needs a real Tk display (uses xvfb in CI); skipped where tkinter
isn't importable, matching the other UI tests.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("tkinter")

import tkinter as tk

from sorter.ui.serial_console import LINE_ENDINGS, SerialConsole

from .conftest import FakeApp, FakeBroker


def _console(root, app: FakeApp, **kwargs) -> SerialConsole:
    console = SerialConsole(root, app=app, **kwargs)
    console.pack(fill=tk.BOTH, expand=True)
    return console


def _body(console: SerialConsole) -> str:
    return console.text.get("1.0", tk.END)


def test_bus_lines_land_in_the_log_with_direction_tags(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        app.bus.post("serial/rx", "done")
        app.bus.post("serial/tx", "xf:0")
        app.bus.drain()
        body = _body(console)
        assert "<- done" in body
        assert "-> xf:0" in body
        # RX and TX print in different palette roles, which is the whole point
        # of tagging them.
        assert console.text.tag_cget("rx", "foreground") != console.text.tag_cget("tx", "foreground")
    finally:
        console.close()


def test_backlog_is_replayed_so_a_failed_probe_is_still_readable(root) -> None:
    stamp = time.time()
    backlog = [
        ("note", stamp, "probing /dev/ttyUSB0 @ 9600…"),
        ("rx", stamp, "\x00garbage"),
        ("note", stamp, "/dev/ttyUSB0 did not handshake"),
    ]
    app = FakeApp(backlog=backlog)
    console = _console(root, app)
    try:
        body = _body(console)
        assert "probing /dev/ttyUSB0 @ 9600…" in body
        assert "garbage" in body
        assert "did not handshake" in body
        assert "end of replay (3 earlier line(s))" in body
    finally:
        console.close()


def test_pause_holds_lines_and_resume_flushes_them_in_order(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        console.paused_var.set(True)
        app.bus.post("serial/rx", "first")
        app.bus.post("serial/rx", "second")
        app.bus.drain()
        assert "first" not in _body(console)

        console.paused_var.set(False)
        console._on_pause_toggled()
        body = _body(console)
        assert body.index("first") < body.index("second"), "held lines flush in arrival order"
    finally:
        console.close()


def test_timestamp_toggle_rerenders_what_is_already_there(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        console.append("rx", "done", stamp=time.mktime((2026, 8, 12, 13, 45, 7, 0, 0, -1)))
        assert "13:45:07" not in _body(console)
        console.timestamps_var.set(True)
        console._rerender()
        assert "13:45:07 <- done" in _body(console)
    finally:
        console.close()


def test_clear_empties_both_the_widget_and_the_dump(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        console.append("rx", "done")
        console.clear()
        assert _body(console).strip() == ""
        assert console.dump() == ""
    finally:
        console.close()


@pytest.mark.parametrize(
    ("label", "ending"),
    [("No line ending", ""), ("New Line", "\n"), ("Carriage Return", "\r"), ("Both NL & CR", "\r\n")],
)
def test_send_applies_the_selected_line_ending(root, label: str, ending: str) -> None:
    broker = FakeBroker()
    app = FakeApp(broker=broker)
    console = _console(root, app)
    try:
        assert LINE_ENDINGS[label] == ending
        console.ending_var.set(label)
        console.entry_var.set("getconfig")
        console.send_command()
        assert broker.raw == [f"getconfig{ending}"]
        assert console.entry_var.get() == ""
    finally:
        console.close()


def test_send_without_a_connection_says_so_instead_of_raising(root) -> None:
    app = FakeApp(broker=FakeBroker(connected=False))
    console = _console(root, app)
    try:
        console.entry_var.set("version")
        console.send_command()
        assert app.broker.raw == []
        assert "not connected" in _body(console)
    finally:
        console.close()


def test_up_and_down_walk_the_command_history(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        for cmd in ("version", "getconfig"):
            console.entry_var.set(cmd)
            console.send_command()
        console._history_step(-1)
        assert console.entry_var.get() == "getconfig"
        console._history_step(-1)
        assert console.entry_var.get() == "version"
        console._history_step(1)
        assert console.entry_var.get() == "getconfig"
        console._history_step(1)
        assert console.entry_var.get() == "", "past the newest entry the field clears"
    finally:
        console.close()


def test_changing_the_baud_persists_it_and_reconnects(root) -> None:
    app = FakeApp(broker=FakeBroker())
    repaints: list[int] = []
    console = _console(root, app, show_baud=True, on_baud_changed=lambda: repaints.append(1))
    try:
        console.baud_var.set("115200")
        console._on_baud_selected()
        assert app.config.serial["baud"] == 115200
        assert app.config.saved == 1
        assert app.reconnects == ["/dev/ttyFAKE"]
        assert repaints, "the owner is told to repaint its connection header"
    finally:
        console.close()


def test_the_baud_selector_is_opt_in(root) -> None:
    """The tab's Connection panel owns baud; two controls would disagree."""
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        assert "Baud" not in _labels(console)
    finally:
        console.close()

    with_baud = _console(root, FakeApp(broker=FakeBroker()), show_baud=True)
    try:
        assert "Baud" in _labels(with_baud)
    finally:
        with_baud.close()


def test_detach_button_is_opt_in_and_calls_back(root) -> None:
    app = FakeApp(broker=FakeBroker())
    plain = _console(root, app)
    try:
        assert not _buttons(plain, "Open monitor ↗")
    finally:
        plain.close()

    console = _console(root, app, detach_command=app.open_serial_monitor)
    try:
        button = _buttons(console, "Open monitor ↗")
        assert button, "the tab's route to the detached window"
        button[0].invoke()
        assert app.monitors_opened == 1
    finally:
        console.close()


def _widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from _widgets(child)


def _labels(console: SerialConsole) -> set[str]:
    out = set()
    for widget in _widgets(console):
        try:
            out.add(str(widget.cget("text")))
        except tk.TclError:
            pass
    return out


def _buttons(console: SerialConsole, text: str) -> list:
    return [
        widget for widget in _widgets(console) if widget.winfo_class() == "TButton" and str(widget.cget("text")) == text
    ]


def test_unsubscribes_on_close(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    assert console._on_rx in app.bus._subs.get("serial/rx", [])
    console.close()
    for topic in ("serial/rx", "serial/tx", "serial/note"):
        assert not app.bus._subs.get(topic), f"{topic} still has a subscriber"


def test_destroying_the_widget_unsubscribes_too(root) -> None:
    """A console torn down with its parent must not keep receiving lines."""
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    console.destroy()
    assert not app.bus._subs.get("serial/rx")


def test_apply_palette_repaints_the_text_tags(root) -> None:
    from sorter.ui.theme import apply_theme

    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        before = console.text.tag_cget("rx", "foreground")
        apply_theme(root, theme="Light")
        console.apply_palette()
        assert console.text.tag_cget("rx", "foreground") != before
    finally:
        apply_theme(root, theme="Dark")
        console.close()


def _flush_filter(console: SerialConsole) -> None:
    """Run the debounced re-render now rather than waiting out the timer."""
    if console._filter_job is not None:
        console.after_cancel(console._filter_job)
        console._filter_job = None
    console._rerender()


def _post(app: FakeApp, *pairs: tuple[str, str]) -> None:
    for topic, line in pairs:
        app.bus.post(topic, line)
    app.bus.drain()


def test_typing_a_filter_debounces_instead_of_rerendering_per_keystroke(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        console.filter_var.set("do")
        assert console._filter_job is not None, "no re-render was scheduled"
    finally:
        console.close()


def test_filter_hides_non_matching_lines_but_keeps_them_for_later(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        _post(app, ("serial/tx", "xf:0"), ("serial/rx", "done"), ("serial/rx", "waiting"))
        console.filter_var.set("done")
        _flush_filter(console)
        body = _body(console)
        assert "done" in body
        assert "xf:0" not in body
        assert "waiting" not in body

        # Filtering is a view, not a deletion.
        console.clear_filter()
        _flush_filter(console)
        body = _body(console)
        assert "xf:0" in body and "waiting" in body
    finally:
        console.close()


def test_filter_is_case_insensitive_and_matches_the_board_text_only(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        _post(app, ("serial/rx", "Done"))
        console.filter_var.set("dOnE")
        _flush_filter(console)
        assert "Done" in _body(console)

        # The `<-`/`->` prefixes are rendering, not content — a filter must not
        # key off them or "->" would silently mean "everything I sent".
        console.filter_var.set("<-")
        _flush_filter(console)
        assert "Done" not in _body(console)
    finally:
        console.close()


def test_direction_toggles_hide_a_whole_kind(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        _post(app, ("serial/tx", "version"), ("serial/rx", "7.2.1"), ("serial/note", "probing"))
        console.show_kind_vars["tx"].set(False)
        console._rerender()
        body = _body(console)
        assert "version" not in body
        assert "7.2.1" in body and "probing" in body
    finally:
        console.close()


def test_lines_arriving_while_a_filter_is_active_respect_it(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        console.filter_var.set("done")
        _flush_filter(console)
        _post(app, ("serial/rx", "waiting"), ("serial/rx", "done"))
        body = _body(console)
        assert "done" in body
        assert "waiting" not in body
        # …and are still there once the filter goes away.
        console.clear_filter()
        _flush_filter(console)
        assert "waiting" in _body(console)
    finally:
        console.close()


def test_dump_follows_the_filter_so_save_writes_what_is_shown(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        _post(app, ("serial/rx", "done"), ("serial/rx", "waiting"))
        console.filter_var.set("done")
        _flush_filter(console)
        dumped = console.dump()
        assert "done" in dumped
        assert "waiting" not in dumped
    finally:
        console.close()


def test_count_reports_shown_against_total_only_while_filtering(root) -> None:
    app = FakeApp(broker=FakeBroker())
    console = _console(root, app)
    try:
        _post(app, ("serial/rx", "done"), ("serial/rx", "waiting"), ("serial/tx", "xf:0"))
        assert console._count_var.get() == "3 lines"
        console.filter_var.set("done")
        _flush_filter(console)
        assert console._count_var.get() == "1 of 3 lines"
    finally:
        console.close()
