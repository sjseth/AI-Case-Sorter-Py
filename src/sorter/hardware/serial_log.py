"""Continuous serial traffic log: every ``serial/*`` line, kept on disk.

The serial monitor (``ui/serial_monitor.py``) shows a session's traffic on
screen. This module writes the same traffic to a file, for a board problem
that has to be sent to someone else. It subscribes to the same bus topics the
monitor renders, so the emulator's traffic lands here exactly as a real
board's does. Switched on and off live from Settings → Serial
(``config.serial["log_traffic"]``). No restart is needed.

Each app session gets one file, ``<data root>/logs/serial-<stamp>.log``, and
re-enabling within a session appends to it. A file past
``MAX_SERIAL_LOG_BYTES`` rolls over to a fresh one. Opening a new file prunes
the directory to the newest ``MAX_SERIAL_LOGS``, as the training logs do.

Best-effort, like the launch and training logs: an I/O error disables the log
with one warning. It never raises into a bus handler and never touches the
connection. Writes happen on the main thread (the bus drain) and are
buffered. The buffer is flushed at most ``FLUSH_INTERVAL_S`` apart while
traffic flows, and again when logging is switched off or the window closes.
"""

from __future__ import annotations

import functools
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .. import paths

log = logging.getLogger(__name__)

SERIAL_LOG_PREFIX = "serial-"
MAX_SERIAL_LOGS = 10
MAX_SERIAL_LOG_BYTES = 5 * 1024 * 1024
FLUSH_INTERVAL_S = 1.0

# The serial monitor's direction markers, shared so a saved dump and this log read alike.
KIND_PREFIX = {"rx": "<- ", "tx": "-> ", "note": "-- "}
TOPIC_KINDS = {"serial/rx": "rx", "serial/tx": "tx", "serial/note": "note"}


def serial_log_path(stamp: str) -> Path:
    return paths.logs_dir() / f"{SERIAL_LOG_PREFIX}{stamp}.log"


def serial_logs(newest_first: bool = True) -> list[Path]:
    return paths.session_logs(SERIAL_LOG_PREFIX, newest_first=newest_first)


class SerialTrafficLog:
    """Appends bus-carried serial traffic to a per-session file while enabled."""

    def __init__(self, *, enabled: bool = False, clock: Any = time.time) -> None:
        self.path: Path | None = None
        self._handle: TextIO | None = None
        self._bytes = 0
        self._last_flush = 0.0
        self._clock = clock
        if enabled:
            self.set_enabled(True)

    @property
    def enabled(self) -> bool:
        """True while lines are actually being written, so False after a failure too."""
        return self._handle is not None

    def attach(self, bus: Any) -> None:
        for topic, kind in TOPIC_KINDS.items():
            bus.subscribe(topic, functools.partial(self.write, kind))

    def set_enabled(self, on: bool) -> bool:
        """Start or stop logging; returns whether it is now logging."""
        if on and self._handle is None:
            self._open()
        elif not on:
            self._close()
        return self.enabled

    def close(self) -> None:
        self._close()

    def write(self, kind: str, line: Any) -> None:
        if self._handle is None:
            return
        now = self._clock()
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._emit(f"{stamp} {KIND_PREFIX.get(kind, '')}{line}", now)
        if self._handle is not None and self._bytes >= MAX_SERIAL_LOG_BYTES:
            self._close()
            self._open()

    def _open(self) -> None:
        try:
            if self.path is None or self._bytes >= MAX_SERIAL_LOG_BYTES:
                paths.logs_dir().mkdir(parents=True, exist_ok=True)
                paths.prune_session_logs(SERIAL_LOG_PREFIX, MAX_SERIAL_LOGS)
                self.path = serial_log_path(datetime.now().strftime("%Y%m%d-%H%M%S"))
                self._bytes = 0
            self._handle = self.path.open("a", encoding="utf-8", errors="replace")
        except OSError as exc:
            self._fail(exc)
            return
        now = self._clock()
        self._emit(f"# serial log opened {datetime.fromtimestamp(now).isoformat(timespec='seconds')}", now)

    def _emit(self, text: str, now: float) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.write(text + "\n")
            self._bytes += len(text) + 1
            if now - self._last_flush >= FLUSH_INTERVAL_S:
                handle.flush()
                self._last_flush = now
        except (OSError, ValueError) as exc:
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        log.warning("Serial traffic log disabled: %s", exc)
        self._close()

    def _close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except (OSError, ValueError):
            pass
