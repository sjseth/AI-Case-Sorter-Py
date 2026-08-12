"""Detachable serial monitor — the Arduino IDE's, for this board.

Opened from the status-bar serial indicator (and from the Serial Config tab),
so it is reachable exactly when the connection is the thing that's broken. It
replays the app's rolling backlog on open, which is what makes a failed
auto-connect diagnosable after the fact.

The log, the filter and the send row are `SerialConsole` — the same widget the
Serial Config tab embeds (see that module). What lives here is only what a
detached window adds: a connection header, the baud selector, and the
single-instance lifecycle.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from .serial_console import SerialConsole
from .theme import PALETTE, get_fonts


class SerialMonitorWindow(tk.Toplevel):
    """A `SerialConsole` in its own window, under a live connection header."""

    def __init__(self, parent: tk.Misc, *, app: Any) -> None:
        super().__init__(parent)
        self.app = app
        self.bus = app.bus
        self.title("Serial Monitor")
        self.geometry("900x560")
        # Below this the send row's fixed controls start eating the entry.
        self.minsize(820, 380)
        self.configure(bg=PALETTE["bg_window"])

        self._fonts = getattr(app, "fonts", None) or get_fonts(self)

        self._build_header()
        self.console = SerialConsole(
            self,
            app=app,
            on_baud_changed=self.refresh_connection,
        )
        self.console.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))

        self.bus.subscribe("serial/state", self._on_state)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.refresh_connection()

    # ----- construction -------------------------------------------------------

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(10, 8, 10, 0))
        header.pack(side=tk.TOP, fill=tk.X)

        self._dot = tk.Label(
            header,
            text="●",  # BLACK CIRCLE — mirrors the status-bar indicator
            background=PALETTE["bg_surface"],
            foreground=PALETTE["error"],
            font=self._fonts["small"],
        )
        self._dot.pack(side=tk.LEFT, padx=(0, 6))
        self._header_var = tk.StringVar(value="disconnected")
        ttk.Label(header, textvariable=self._header_var).pack(side=tk.LEFT)

    def apply_palette(self) -> None:
        """Re-read the palette after a theme switch (see `SerialConsole`)."""
        self.configure(bg=PALETTE["bg_window"])
        self.console.apply_palette()
        self.refresh_connection()

    # ----- connection state ---------------------------------------------------

    def refresh_connection(self) -> None:
        """Repaint the header from the app's live broker."""
        broker = getattr(self.app, "broker", None)
        connected = bool(broker is not None and getattr(broker, "is_connected", False))
        port = self.console.current_port() or "no port"
        baud = int(getattr(broker, "baud", 0) or self.console.configured_baud())
        if connected:
            firmware = getattr(broker, "firmware_version", "") or "Unknown"
            self._header_var.set(f"{port} @ {baud} — {firmware}")
        else:
            self._header_var.set(f"{port} @ {baud} — disconnected")
        self._dot.config(foreground=PALETTE["success" if connected else "error"])

    def _on_state(self, _payload: Any = None) -> None:
        self.refresh_connection()

    # ----- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.console.close()
        try:
            self.bus.unsubscribe("serial/state", self._on_state)
        except Exception:
            pass
        self.destroy()
