"""The status bar's memory: a bounded ring of recent status lines.

The status bar shows one line, truncated to the window's width, until the next
one replaces it — a run error was unreadable and then gone (issue #112, B4).
``QtMainWindow.set_status`` appends every line here as well, and the Messages
panel (``messages_view.py``) renders it in full.

UI-free, like ``palettes.py``: no Qt import, so it tests without a display.

**An in-progress line is a placeholder.** A line ending in "…" ("Connecting to
COM3…", "Classifying…") — or one added with ``progress=True`` — is replaced by
the next non-error line, so a run's per-case steps occupy one entry instead of
flushing the ring every fifty cases. An error neither replaces nor is
replaced: it is appended after the step it interrupted, which stays above it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

MAX_ENTRIES = 200

INFO = "info"
ERROR = "error"
LEVELS = (INFO, ERROR)

IN_PROGRESS_SUFFIX = "…"


@dataclass(frozen=True)
class Entry:
    stamp: float  # epoch seconds
    text: str
    level: str = INFO
    progress: bool = False

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.stamp))

    def tag(self) -> str:
        """The level marker printed before the text; info lines carry none."""
        return "" if self.level == INFO else f"[{self.level}] "

    def format(self) -> str:
        return f"{self.clock()} {self.tag()}{self.text}"


# (entry, replaced_previous) — `replaced_previous` means the last entry was
# swapped for this one rather than this one appended after it.
Listener = Callable[[Entry, bool], None]


class MessageLog:
    """The last ``maxlen`` status lines, oldest first."""

    def __init__(self, maxlen: int = MAX_ENTRIES) -> None:
        self._entries: deque[Entry] = deque(maxlen=maxlen)
        self._listeners: list[Listener] = []

    @property
    def maxlen(self) -> int:
        return self._entries.maxlen or 0

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def add(
        self,
        text: str,
        *,
        level: str = INFO,
        progress: bool | None = None,
        stamp: float | None = None,
    ) -> Entry:
        """Record a line; ``progress=None`` infers it from a trailing "…"."""
        text = str(text)
        level = level if level in LEVELS else INFO
        if level == ERROR:
            progress = False  # an error is never a placeholder
        elif progress is None:
            progress = text.rstrip().endswith(IN_PROGRESS_SUFFIX)
        entry = Entry(time.time() if stamp is None else stamp, text, level, progress)
        replace = bool(self._entries) and self._entries[-1].progress and level != ERROR
        if replace:
            self._entries[-1] = entry
        else:
            self._entries.append(entry)
        for listener in list(self._listeners):
            listener(entry, replace)
        return entry

    def clear(self) -> None:
        self._entries.clear()

    def dump(self) -> str:
        """Every entry as plain text, one per line — what "Copy all" copies."""
        lines = [entry.format() for entry in self._entries]
        return "\n".join(lines) + ("\n" if lines else "")
