"""Shared fakes for the serial console/monitor UI tests.

The console is embedded twice (Serial Config tab, detached monitor), so both
test modules drive it against the same stand-in app.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("tkinter")


class FakeBroker:
    def __init__(self, *, connected: bool = True) -> None:
        self.port = "/dev/ttyFAKE"
        self.baud = 9600
        self.firmware_version = "7.2.1"
        self.is_connected = connected
        self.raw: list[str] = []

    def send_raw(self, text: str) -> None:
        self.raw.append(text)


class FakeConfig:
    def __init__(self) -> None:
        self.serial: dict[str, Any] = {"port": "/dev/ttyFAKE", "baud": 9600}
        self.saved = 0

    def save(self) -> None:
        self.saved += 1


class FakeApp:
    def __init__(self, *, broker: Any = None, backlog: list | None = None) -> None:
        from sorter.control.events import EventBus

        self.bus = EventBus()
        self.config = FakeConfig()
        self.broker = broker
        self.serial_backlog = backlog or []
        self.reconnects: list[str] = []
        self.disconnects = 0
        self.monitors_opened = 0

    def connect_serial(self, port: str | None = None) -> None:
        self.reconnects.append(port or "")

    def disconnect_serial(self) -> None:
        self.disconnects += 1

    def open_serial_monitor(self) -> None:
        self.monitors_opened += 1

    def set_status(self, message: str) -> None:
        pass


@pytest.fixture
def root():
    import tkinter as tk

    from sorter.ui.theme import apply_theme

    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    r.withdraw()
    apply_theme(r)
    yield r
    r.destroy()
