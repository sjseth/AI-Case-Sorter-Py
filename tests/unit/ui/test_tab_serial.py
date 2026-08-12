"""Serial Config tab — that its monitor panel is the shared console.

The point of the panel is that it is not a second, lesser log: it is the same
`SerialConsole` the detached window wraps, so the two can't drift apart again.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")

import tkinter as tk

from sorter.hardware import serial_broker
from sorter.ui.serial_console import SerialConsole
from sorter.ui.tab_serial import SerialTab

from .conftest import FakeApp, FakeBroker


@pytest.fixture
def tab(root, monkeypatch):
    monkeypatch.setattr(serial_broker, "list_serial_ports", lambda: ["/dev/ttyFAKE"])
    app = FakeApp(broker=FakeBroker())
    widget = SerialTab(root, config=app.config, bus=app.bus, app=app)
    widget.pack(fill=tk.BOTH, expand=True)
    yield widget, app
    widget.console.close()
    widget.destroy()


def test_the_panel_is_the_shared_console_and_shows_live_traffic(tab) -> None:
    widget, app = tab
    assert isinstance(widget.console, SerialConsole)
    app.bus.post("serial/rx", "done")
    app.bus.post("serial/tx", "xf:0")
    app.bus.drain()
    body = widget.console.text.get("1.0", tk.END)
    assert "<- done" in body and "-> xf:0" in body


def test_the_panel_sends_through_the_console(tab) -> None:
    widget, app = tab
    widget.console.entry_var.set("getconfig")
    widget.console.send_command()
    assert app.broker.raw == ["getconfig\n"]


def test_the_panel_offers_the_detached_window(tab) -> None:
    widget, app = tab
    buttons = [
        child
        for child in _descendants(widget.console)
        if child.winfo_class() == "TButton" and str(child.cget("text")) == "Open monitor ↗"
    ]
    assert buttons
    buttons[0].invoke()
    assert app.monitors_opened == 1


def _descendants(parent):
    for child in parent.winfo_children():
        yield child
        yield from _descendants(child)
