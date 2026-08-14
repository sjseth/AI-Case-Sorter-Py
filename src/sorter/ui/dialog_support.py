"""Help -> Export support package…: preview, copy, or save the support package.

Seth's ask: make it easy to share the current configuration (run options,
training options, serial and camera settings) on Discord to get help. The
content — and every redaction rule — lives in ``support_bundle``; this dialog
only shows the report and hands it out, as clipboard text or as the ZIP.

Seam discipline (§5): ``notify`` and ``ask_save_path`` are instance
attributes a test replaces, never direct ``QMessageBox``/``QFileDialog``
calls from a handler.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .support_bundle import collect_report, write_bundle

ZIP_FILTER = "Support package (*.zip)"


def default_bundle_name() -> str:
    return f"casesorter-support-{date.today():%Y%m%d}.zip"


class SupportDialog(QDialog):
    """Read-only report preview with Copy / Save package / Close."""

    def __init__(self, win: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._win = win
        self.setWindowTitle("Get support")
        self.resize(640, 520)

        self.report_text = collect_report(win.config, win.config.db)

        # Attributes, not methods: a test drives every path without a modal.
        self.notify: Callable[[str, str], None] = self._notify
        self.ask_save_path: Callable[[str], str | None] = self._ask_save_path

        column = QVBoxLayout(self)
        intro = QLabel(
            "Share this report on the community Discord to get help. "
            "Secrets are redacted — the API key is reported only as set/not set.",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        column.addWidget(intro)

        self.preview = QPlainTextEdit(self)
        self.preview.setObjectName("supportReport")
        self.preview.setReadOnly(True)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.preview.setPlainText(self.report_text)
        column.addWidget(self.preview, 1)

        row = QHBoxLayout()
        self.copy_button = QPushButton("Copy to clipboard", self)
        self.copy_button.clicked.connect(self.copy_report)
        row.addWidget(self.copy_button)
        self.save_button = QPushButton("Save package…", self)
        self.save_button.clicked.connect(self.save_package)
        row.addWidget(self.save_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        column.addLayout(row)

    # ----- actions ------------------------------------------------------------

    def copy_report(self) -> None:
        QGuiApplication.clipboard().setText(self.report_text)

    def save_package(self) -> None:
        raw = self.ask_save_path(default_bundle_name())
        if not raw:
            return
        try:
            written = write_bundle(Path(raw), self._win.config, self._win.config.db)
        except OSError as exc:
            self.notify("Save failed", str(exc))
            return
        self.notify("Support package saved", f"Wrote {written}")

    # ----- dialog hooks -------------------------------------------------------

    def _notify(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _ask_save_path(self, default_name: str) -> str | None:
        path, _filter = QFileDialog.getSaveFileName(self, "Save support package", default_name, ZIP_FILTER)
        return path or None


def build_support_dialog(win: Any) -> SupportDialog:
    return SupportDialog(win, win)


def open_support_dialog(win: Any) -> None:
    """The Help-menu entry point (wired by ``app.py``, not here)."""
    build_support_dialog(win).exec()
