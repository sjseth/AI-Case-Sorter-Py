"""Messages: the content of the right-hand panel (View → Messages).

Renders the window's ``MessageLog`` — every status-bar line of the session, up
to ``message_log.MAX_ENTRIES``, in full and word-wrapped. Clicking the status
bar's message area opens it (``QtMainWindow.open_messages``).

Newest at the bottom, like the serial monitor: the log reads top-down as cause
then effect, and it follows new lines only while it is already scrolled to the
bottom, so reading back through it is never yanked away.

The text is a read-only ``QPlainTextEdit``, so any span selects and copies with
the usual shortcut; **Copy all** copies ``MessageLog.dump()``. Colours are baked
into each line's format, so a theme switch re-renders through
``apply_palette()`` (CLAUDE.md §5).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor, QFont, QGuiApplication, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .message_log import ERROR, Entry, MessageLog

# Level -> the palette role its text prints in.
LEVEL_ROLE = {"info": "text", ERROR: "error"}

# Dark-theme values, used only if the host window has no live palette yet.
_FALLBACK = {"text": "#d4d4d4", "text_subtle": "#6f6f6f", "error": "#ef4444"}


class MessagesView(QWidget):
    """The status-message log, readable in full and copyable."""

    def __init__(self, win: Any, log: MessageLog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._win = win
        self.log = log

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.count_label = QLabel("", self)
        self.count_label.setObjectName("mutedLabel")
        row.addWidget(self.count_label)
        row.addStretch(1)
        self.copy_button = QPushButton("Copy all", self)
        self.copy_button.clicked.connect(self.copy_all)
        row.addWidget(self.copy_button)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.clicked.connect(self.clear)
        row.addWidget(self.clear_button)
        layout.addLayout(row)

        self.output = QPlainTextEdit(self)
        self.output.setObjectName("messageLog")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # One block per entry, so the widget trims in step with the ring.
        self.output.setMaximumBlockCount(log.maxlen)
        layout.addWidget(self.output, 1)

        # The ring may already hold lines from before the panel existed.
        self._rerender()
        log.subscribe(self._on_entry)

    # ----- theme ------------------------------------------------------------

    def _role_color(self, role: str) -> str:
        palette = getattr(self._win, "palette_colors", None) or {}
        return str(palette.get(role) or _FALLBACK[role])

    def apply_palette(self) -> None:
        self._rerender()

    # ----- rendering --------------------------------------------------------

    def _on_entry(self, entry: Entry, replaced: bool) -> None:
        self._render(entry, replace=replaced)
        self._update_count()

    def _render(self, entry: Entry, *, replace: bool = False) -> None:
        bar = self.output.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum()
        document = self.output.document()
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if replace and not document.isEmpty():
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        elif not document.isEmpty():
            cursor.insertBlock()

        stamp_fmt = QTextCharFormat()
        stamp_fmt.setForeground(QColor(self._role_color("text_subtle")))
        cursor.insertText(f"{entry.clock()} ", stamp_fmt)
        text_fmt = QTextCharFormat()
        text_fmt.setForeground(QColor(self._role_color(LEVEL_ROLE.get(entry.level, "text"))))
        if entry.level == ERROR:
            # Weight as well as hue: the error must not depend on seeing red.
            text_fmt.setFontWeight(QFont.Weight.Bold)
        cursor.insertText(f"{entry.tag()}{entry.text}", text_fmt)
        if at_bottom:
            bar.setValue(bar.maximum())

    def _rerender(self) -> None:
        self.output.clear()
        for entry in self.log.entries():
            self._render(entry)
        self._update_count()

    def _update_count(self) -> None:
        count = len(self.log)
        self.count_label.setText(f"{count} message{'' if count == 1 else 's'} (last {self.log.maxlen} kept)")

    # ----- actions ----------------------------------------------------------

    def text(self) -> str:
        """What the panel shows, as plain text."""
        return self.output.toPlainText()

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.log.dump())

    def clear(self) -> None:
        self.log.clear()
        self.output.clear()
        self._update_count()


def build_messages_view(win: Any, log: MessageLog) -> MessagesView:
    return MessagesView(win, log)
