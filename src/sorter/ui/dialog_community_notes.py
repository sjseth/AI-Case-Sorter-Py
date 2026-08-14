"""Moderator notes for the active community model — the ack flow and the history.

One dialog serves both: it lists every note the store holds for the model
(newest first, unacknowledged ones badged "New") and shows the selected one's
timestamp and body. Raised automatically on entering Sort while something is
unacknowledged, and opened by hand from the Sort page's "Moderator notes (N)"
button.

**Acknowledge marks every unacknowledged note shown here**, not just the
selected one — they are all on screen, and acknowledgement is a "I have read
this" signal rather than a per-item action. Closing (or "Remind me later")
changes nothing, so the notes come back on the next visit.

Note bodies are moderator-written and may carry links, so the body is rendered
as rich text with external links live. Everything else in the body is escaped:
only ``http(s)`` URLs become anchors.

``on_acknowledge`` is the seam — the caller persists; this dialog never touches
the store.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..community.notes import StoredNote, unacknowledged
from . import formatting

TITLE = "Moderator note"
HEADER = (
    "A moderator of “{model}” has left you a note about your feedback images. "
    "Acknowledge it so it is not shown again, or close this window to be reminded "
    "next time you open the Sort page."
)
NEW_BADGE = "New"
BUTTON_ACKNOWLEDGE = "Acknowledge"
BUTTON_LATER = "Remind me later"
NO_NOTES = "No notes for this model."

# Only http(s) — a note is someone else's text, and nothing else should be
# turned into something clickable. Trailing sentence punctuation is left out
# of the link.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRIM = ".,;:!?)]}\"'"


def linkify(text: str) -> str:
    """Escaped note text with bare URLs turned into anchors."""
    out: list[str] = []
    cursor = 0
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRIM)
        out.append(html.escape(text[cursor : match.start()]))
        escaped = html.escape(url, quote=True)
        out.append(f'<a href="{escaped}">{escaped}</a>')
        cursor = match.start() + len(url)
    out.append(html.escape(text[cursor:]))
    return "".join(out).replace("\n", "<br>")


def render_note(note: StoredNote) -> str:
    """One note as the detail pane shows it: timestamp, then body."""
    when = formatting.format_datetime(note.created)
    return f"<p><b>{html.escape(when)}</b></p><p>{linkify(note.note)}</p>"


def sort_notes(notes: Sequence[StoredNote]) -> list[StoredNote]:
    """Newest first, on the parsed date — never on the locale-rendered text."""
    return sorted(notes, key=lambda n: formatting.parse(n.created) or datetime.min, reverse=True)


class CommunityNotesDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        model_name: str,
        notes: Sequence[StoredNote],
        on_acknowledge: Callable[[list[int]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.notes = sort_notes(notes)
        self.on_acknowledge = on_acknowledge
        self.setWindowTitle(f"{TITLE} — {model_name}" if model_name else TITLE)
        self.setMinimumSize(620, 340)

        column = QVBoxLayout(self)
        self.header_label = QLabel(HEADER.format(model=model_name or "this model"), self)
        self.header_label.setObjectName("dialogHint")
        self.header_label.setWordWrap(True)
        column.addWidget(self.header_label)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.list = QListWidget(splitter)
        self.list.currentRowChanged.connect(self._show_note)
        splitter.addWidget(self.list)
        self.body = QTextBrowser(splitter)
        self.body.setOpenExternalLinks(True)
        splitter.addWidget(self.body)
        splitter.setSizes([180, 440])
        column.addWidget(splitter, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.acknowledge_button = QPushButton(BUTTON_ACKNOWLEDGE, self)
        self.acknowledge_button.setObjectName("action")
        self.acknowledge_button.clicked.connect(self.acknowledge)
        row.addWidget(self.acknowledge_button)
        later = QPushButton(BUTTON_LATER, self)
        later.clicked.connect(self.reject)
        row.addWidget(later)
        column.addLayout(row)

        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for note in self.notes:
            label = formatting.format_date(note.created)
            self.list.addItem(f"{label}  ·  {NEW_BADGE}" if not note.acknowledged else label)
        if self.notes:
            self.list.setCurrentRow(0)
        else:
            self.body.setHtml(f"<p>{NO_NOTES}</p>")
        self.acknowledge_button.setEnabled(bool(unacknowledged(self.notes)))

    def _show_note(self, row: int) -> None:
        if 0 <= row < len(self.notes):
            self.body.setHtml(render_note(self.notes[row]))

    def acknowledge(self) -> None:
        ids = [note.id for note in unacknowledged(self.notes)]
        if self.on_acknowledge is not None:
            self.on_acknowledge(ids)
        self.accept()


def build_community_notes_dialog(
    win: Any,
    *,
    model_name: str,
    notes: Sequence[StoredNote],
    on_acknowledge: Callable[[list[int]], None] | None = None,
) -> CommunityNotesDialog:
    return CommunityNotesDialog(win, model_name=model_name, notes=notes, on_acknowledge=on_acknowledge)
