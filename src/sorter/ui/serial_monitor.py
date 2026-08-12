"""Detachable serial monitor — the Arduino IDE's, for this board.

Opened from the status-bar serial indicator (and from the Serial Config tab),
so it is reachable exactly when the connection is the thing that's broken. It
replays the app's rolling backlog on open, which is what makes a failed
auto-connect diagnosable after the fact.

Everything arrives over the EventBus (`serial/rx`, `serial/tx`, `serial/note`)
and is therefore delivered on the Tk main thread — nothing here may be called
from the reader thread. See CLAUDE.md §3.

The log is deliberately never re-ordered. Serial traffic only means anything
in the order it happened — a command and the reply it drew are one exchange —
so the way to narrow it down is the filter box and the direction toggles,
which hide lines without moving the ones that remain.
"""

from __future__ import annotations

import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, ttk
from typing import Any

from .theme import PALETTE, get_fonts

# The Arduino IDE's ladder, and its default.
BAUD_RATES = (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 74880, 115200, 230400, 250000)
DEFAULT_BAUD = 9600

# Label → what actually goes on the wire after the typed text.
LINE_ENDINGS: dict[str, str] = {
    "No line ending": "",
    "New Line": "\n",
    "Carriage Return": "\r",
    "Both NL & CR": "\r\n",
}
DEFAULT_LINE_ENDING = "New Line"

MAX_LINES = 4000  # scrollback cap; the oldest are dropped, not the newest
MAX_HISTORY = 100
# Typing in the filter box re-renders the whole scrollback, so coalesce
# keystrokes instead of doing it per character.
FILTER_DEBOUNCE_MS = 120

# Line kinds → the palette role they print in and the prefix they carry.
KIND_ROLE = {"rx": "text", "tx": "update", "note": "text_muted"}
KIND_PREFIX = {"rx": "<- ", "tx": "-> ", "note": "-- "}


class SerialMonitorWindow(tk.Toplevel):
    """Live serial traffic: filtered, timestamped, pausable, saveable."""

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
        # (kind, epoch seconds, text) — the rendered scrollback, kept so a
        # timestamp toggle can re-render and Save… can dump exactly what's on
        # screen.
        self._lines: deque[tuple[str, float, str]] = deque(maxlen=MAX_LINES)
        self._held: deque[tuple[str, float, str]] = deque(maxlen=MAX_LINES)
        self._history: list[str] = []
        self._history_pos = 0
        self._filter_job: str | None = None

        self._build_header()
        self._build_filter_row()
        # The send row is packed against the bottom before the output claims
        # the rest, or a window smaller than the Text's natural size clips it.
        self._build_send_row()
        self._build_output()

        self.bus.subscribe("serial/rx", self._on_rx)
        self.bus.subscribe("serial/tx", self._on_tx)
        self.bus.subscribe("serial/note", self._on_note)
        self.bus.subscribe("serial/state", self._on_state)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self._replay_backlog()
        self.refresh_connection()

    # ----- construction -------------------------------------------------------

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(10, 8))
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

        controls = ttk.Frame(self, padding=(10, 0))
        controls.pack(side=tk.TOP, fill=tk.X)
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.timestamps_var = tk.BooleanVar(value=False)
        self.paused_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Autoscroll", variable=self.autoscroll_var).pack(side=tk.LEFT)
        ttk.Checkbutton(
            controls,
            text="Timestamps",
            variable=self.timestamps_var,
            command=self._rerender,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(
            controls,
            text="Pause",
            variable=self.paused_var,
            command=self._on_pause_toggled,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(controls, text="Save…", command=self.save_to_file).pack(side=tk.RIGHT)
        ttk.Button(controls, text="Clear", command=self.clear).pack(side=tk.RIGHT, padx=(0, 6))

    def _build_filter_row(self) -> None:
        """Substring filter + per-direction toggles, over the whole scrollback.

        Filtering is a view, not a deletion: `_lines` keeps everything, so
        clearing the box brings the hidden traffic straight back.
        """
        row = ttk.Frame(self, padding=(10, 6, 10, 0))
        row.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(row, text="Filter").pack(side=tk.LEFT, padx=(0, 6))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._schedule_rerender())
        entry = ttk.Entry(row, textvariable=self.filter_var, width=10)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Escape>", lambda _e: self.clear_filter())
        ttk.Button(row, text="✕", width=3, command=self.clear_filter).pack(side=tk.LEFT, padx=(4, 0))

        self.show_kind_vars: dict[str, tk.BooleanVar] = {}
        for kind, label in (("rx", "RX"), ("tx", "TX"), ("note", "Notes")):
            var = tk.BooleanVar(value=True)
            self.show_kind_vars[kind] = var
            ttk.Checkbutton(row, text=label, variable=var, command=self._rerender).pack(side=tk.LEFT, padx=(12, 0))

        self._count_var = tk.StringVar(value="0 lines")
        ttk.Label(row, textvariable=self._count_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=(16, 0))

    def _build_output(self) -> None:
        box = ttk.Frame(self, padding=(10, 8))
        box.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.text = tk.Text(
            box,
            height=12,
            wrap=tk.NONE,
            state=tk.DISABLED,
            font=self._fonts["mono"],
            background=PALETTE["bg_input"],
            foreground=PALETTE["text"],
        )
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for kind, role in KIND_ROLE.items():
            self.text.tag_configure(kind, foreground=PALETTE[role])
        self.text.tag_configure("stamp", foreground=PALETTE["text_subtle"])

    def apply_palette(self) -> None:
        """Re-read the palette after a theme switch.

        `retheme_widgets` translates the colours baked into widgets, but not
        the ones baked into Text *tags* — those are ours to refresh.
        """
        self.configure(bg=PALETTE["bg_window"])
        self.text.configure(background=PALETTE["bg_input"], foreground=PALETTE["text"])
        for kind, role in KIND_ROLE.items():
            self.text.tag_configure(kind, foreground=PALETTE[role])
        self.text.tag_configure("stamp", foreground=PALETTE["text_subtle"])
        self.refresh_connection()

    def _build_send_row(self) -> None:
        row = ttk.Frame(self, padding=(10, 0, 10, 10))
        row.pack(side=tk.BOTTOM, fill=tk.X)

        # Right-hand controls claim their width first; the entry then absorbs
        # whatever is left, so nothing falls off the edge on a narrow window.
        self.baud_var = tk.StringVar(value=str(self._configured_baud()))
        baud = ttk.Combobox(
            row,
            textvariable=self.baud_var,
            values=[str(b) for b in BAUD_RATES],
            state="readonly",
            width=8,
        )
        baud.pack(side=tk.RIGHT)
        baud.bind("<<ComboboxSelected>>", self._on_baud_selected)
        ttk.Label(row, text="Baud").pack(side=tk.RIGHT, padx=(16, 4))

        ttk.Button(row, text="Send", style="Accent.TButton", command=self.send_command).pack(side=tk.RIGHT)
        self.ending_var = tk.StringVar(value=DEFAULT_LINE_ENDING)
        ttk.Combobox(
            row,
            textvariable=self.ending_var,
            values=list(LINE_ENDINGS),
            state="readonly",
            width=15,
        ).pack(side=tk.RIGHT, padx=6)

        self.entry_var = tk.StringVar()
        # A small request, not a small field: the entry takes the row's slack.
        entry = ttk.Entry(row, textvariable=self.entry_var, width=10)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self.send_command())
        entry.bind("<Up>", lambda _e: self._history_step(-1))
        entry.bind("<Down>", lambda _e: self._history_step(1))
        self.entry = entry

    # ----- connection state ---------------------------------------------------

    def _configured_baud(self) -> int:
        try:
            return int(self.app.config.serial.get("baud", DEFAULT_BAUD))
        except (AttributeError, TypeError, ValueError):
            return DEFAULT_BAUD

    def _current_port(self) -> str:
        broker = getattr(self.app, "broker", None)
        port = getattr(broker, "port", "") if broker is not None else ""
        if port:
            return str(port)
        try:
            return str(self.app.config.serial.get("port", "") or "")
        except AttributeError:
            return ""

    def refresh_connection(self) -> None:
        """Repaint the header from the app's live broker."""
        broker = getattr(self.app, "broker", None)
        connected = bool(broker is not None and getattr(broker, "is_connected", False))
        port = self._current_port() or "no port"
        baud = int(getattr(broker, "baud", 0) or self._configured_baud())
        if connected:
            firmware = getattr(broker, "firmware_version", "") or "Unknown"
            self._header_var.set(f"{port} @ {baud} — {firmware}")
        else:
            self._header_var.set(f"{port} @ {baud} — disconnected")
        self._dot.config(foreground=PALETTE["success" if connected else "error"])

    def _on_state(self, _payload: Any = None) -> None:
        self.refresh_connection()

    def _on_baud_selected(self, _event: Any = None) -> None:
        """Persist the new speed and re-open the port at it, IDE-style."""
        try:
            baud = int(self.baud_var.get())
        except (TypeError, ValueError):
            return
        if baud == self._configured_baud() and getattr(getattr(self.app, "broker", None), "baud", None) == baud:
            return
        try:
            self.app.config.serial["baud"] = baud
            self.app.config.save()
        except Exception as exc:
            self.note(f"could not save baud: {exc}")
        port = self._current_port()
        self.note(f"baud set to {baud}")
        if port:
            self.app.connect_serial(port=port)
        self.refresh_connection()

    # ----- inbound ------------------------------------------------------------

    def _on_rx(self, line: Any) -> None:
        self.append("rx", str(line))

    def _on_tx(self, line: Any) -> None:
        self.append("tx", str(line))

    def _on_note(self, line: Any) -> None:
        self.append("note", str(line))

    def note(self, line: str) -> None:
        """Record a monitor-local remark (not board traffic)."""
        self.append("note", line)

    def append(self, kind: str, line: str, *, stamp: float | None = None) -> None:
        record = (kind, time.time() if stamp is None else stamp, line)
        if self.paused_var.get():
            self._held.append(record)
            return
        self._lines.append(record)
        self._render(record)
        self._update_count()

    def _on_pause_toggled(self) -> None:
        if self.paused_var.get():
            return
        held, self._held = list(self._held), deque(maxlen=MAX_LINES)
        for record in held:
            self._lines.append(record)
            self._render(record)
        self._update_count()

    def _replay_backlog(self) -> None:
        """Show what the app captured before this window existed."""
        backlog = list(getattr(self.app, "serial_backlog", ()))
        if not backlog:
            return
        for kind, stamp, line in backlog:
            self._lines.append((kind, stamp, line))
        self._rerender()
        self._lines.append(("note", time.time(), f"end of replay ({len(backlog)} earlier line(s))"))
        self._render(self._lines[-1])
        self._update_count()

    # ----- filtering ----------------------------------------------------------

    def clear_filter(self) -> None:
        self.filter_var.set("")

    def _schedule_rerender(self) -> None:
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(FILTER_DEBOUNCE_MS, self._run_scheduled_rerender)

    def _run_scheduled_rerender(self) -> None:
        self._filter_job = None
        self._rerender()

    def matches(self, record: tuple[str, float, str]) -> bool:
        """Does this line survive the direction toggles and the filter box?

        Case-insensitive substring, matched against the text as the board sent
        it — not the rendered line, so a filter can't accidentally key off the
        `<-`/`->` prefix or a timestamp that is only sometimes shown.
        """
        kind, _stamp, line = record
        var = self.show_kind_vars.get(kind)
        if var is not None and not var.get():
            return False
        needle = self.filter_var.get().strip().lower()
        return not needle or needle in line.lower()

    def _shown_line_count(self) -> int:
        """How many lines the widget is actually displaying.

        Every rendered record ends in a newline, so the insertion point sits at
        the start of an empty trailing line that isn't output — hence the -1.
        """
        return max(0, int(self.text.index("end-1c").split(".")[0]) - 1)

    def _update_count(self) -> None:
        total = len(self._lines)
        shown = self._shown_line_count()
        self._count_var.set(f"{total} lines" if shown == total else f"{shown} of {total} lines")

    # ----- rendering ----------------------------------------------------------

    def _render(self, record: tuple[str, float, str]) -> None:
        kind, stamp, line = record
        if not self.matches(record):
            return
        self.text.configure(state=tk.NORMAL)
        if self.timestamps_var.get():
            self.text.insert(tk.END, f"{time.strftime('%H:%M:%S', time.localtime(stamp))} ", ("stamp",))
        self.text.insert(tk.END, f"{KIND_PREFIX.get(kind, '')}{line}\n", (kind,))
        self._trim()
        if self.autoscroll_var.get():
            self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _rerender(self) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        for record in list(self._lines):
            self._render(record)
        self._update_count()

    def _trim(self) -> None:
        """Keep the widget's line count in step with the deque's cap."""
        shown = self._shown_line_count()
        if shown > MAX_LINES:
            self.text.delete("1.0", f"{shown - MAX_LINES + 1}.0")

    def clear(self) -> None:
        self._lines.clear()
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        self._update_count()

    def dump(self) -> str:
        """The scrollback as plain text — what's on screen, filter included."""
        stamps = self.timestamps_var.get()
        out = []
        for record in self._lines:
            if not self.matches(record):
                continue
            kind, stamp, line = record
            prefix = f"{time.strftime('%H:%M:%S', time.localtime(stamp))} " if stamps else ""
            out.append(f"{prefix}{KIND_PREFIX.get(kind, '')}{line}")
        return "\n".join(out) + ("\n" if out else "")

    def save_to_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save serial log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.dump())
        except OSError as exc:
            self.note(f"save failed: {exc}")
            return
        self.note(f"saved to {path}")

    # ----- outbound -----------------------------------------------------------

    def send_command(self) -> None:
        text = self.entry_var.get()
        if not text:
            return
        broker = getattr(self.app, "broker", None)
        if broker is None or not getattr(broker, "is_connected", False):
            self.note("not connected — nothing sent")
            return
        # The broker echoes the write through on_sent → serial/tx, so the line
        # lands in the log the same way an auto-connect probe's does.
        broker.send_raw(text + LINE_ENDINGS.get(self.ending_var.get(), "\n"))
        if not self._history or self._history[-1] != text:
            self._history.append(text)
            del self._history[:-MAX_HISTORY]
        self._history_pos = len(self._history)
        self.entry_var.set("")

    def _history_step(self, delta: int) -> str:
        """Walk the command history; past the newest entry the field clears."""
        if not self._history:
            return "break"
        self._history_pos = max(0, min(len(self._history), self._history_pos + delta))
        value = "" if self._history_pos >= len(self._history) else self._history[self._history_pos]
        self.entry_var.set(value)
        self.entry.icursor(tk.END)
        return "break"

    # ----- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        # A debounced re-render still in flight would fire against a dead widget.
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except tk.TclError:
                pass
            self._filter_job = None
        for topic, handler in (
            ("serial/rx", self._on_rx),
            ("serial/tx", self._on_tx),
            ("serial/note", self._on_note),
            ("serial/state", self._on_state),
        ):
            try:
                self.bus.unsubscribe(topic, handler)
            except Exception:
                pass
        self.destroy()
