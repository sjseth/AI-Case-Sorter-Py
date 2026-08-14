"""A newer version of the active community model has been published.

Reached from the status-bar "Model update: vN" button, which the Sort page's
``FetchModelSettings`` raises when the server's version outranks the installed
one — the app-update flow's quiet posture rather than upstream's modal on
arrival (issue #29, A24 addendum).

Shows what upstream's dialog shows: INSTALLED → AVAILABLE, the catalogue's
published date / size / author when it has them, and what an update keeps.
That carry-over promise is ``model_io.import_model``'s update-in-place path,
which is exactly what ``on_update`` drives (the Community page's own download,
never a second implementation).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import formatting
from .community_page import format_size

TITLE = "Model update available"
BUTTON_UPDATE = "Update now"
BUTTON_LATER = "Not now"
INSTALLED_CAPTION = "INSTALLED"
AVAILABLE_CAPTION = "AVAILABLE"
ARROW = "→"

CARRY_OVER = (
    "Updating replaces the model files, headstamp definitions and training images "
    "for this model. Your feedback loop settings and slot assignments — including "
    "any sorting templates — are carried over. Only assignments for headstamps "
    "that no longer exist in the new version are dropped."
)


def format_meta(info: Any) -> str:
    """``Published <date> · <size> · by <author>`` from the catalogue entry.

    Empty when there is no entry or it carries none of the three — the fetch
    only promises a version number, so the dialog has to read without them.
    """
    if info is None:
        return ""
    parts: list[str] = []
    published = formatting.format_date(getattr(info, "publish_date", ""))
    if published != formatting.EMPTY_VALUE:
        parts.append(f"Published {published}")
    size = int(getattr(info, "download_size", 0) or 0)
    if size > 0:
        parts.append(format_size(size))
    author = (getattr(info, "author", "") or "").strip()
    if author:
        parts.append(f"by {author}")
    return "  ·  ".join(parts)


class ModelUpdateDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        model_name: str,
        installed_version: int,
        available_version: int,
        info: Any = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_update = on_update
        self.setWindowTitle(TITLE)

        column = QVBoxLayout(self)
        title = QLabel(TITLE, self)
        title.setObjectName("updateTitle")
        column.addWidget(title)
        subtitle = QLabel(model_name, self)
        subtitle.setObjectName("mutedLabel")
        column.addWidget(subtitle)

        column.addLayout(self._build_versions(installed_version, available_version))

        self.meta_label = QLabel(format_meta(info), self)
        self.meta_label.setObjectName("mutedLabel")
        self.meta_label.setVisible(bool(self.meta_label.text()))
        column.addWidget(self.meta_label)

        carry_over = QLabel(CARRY_OVER, self)
        carry_over.setObjectName("dialogHint")
        carry_over.setWordWrap(True)
        column.addWidget(carry_over)

        row = QHBoxLayout()
        row.addStretch(1)
        self.update_button = QPushButton(BUTTON_UPDATE, self)
        # "update" blue, not action green: replacing something installed.
        self.update_button.setObjectName("update")
        self.update_button.clicked.connect(self._update)
        row.addWidget(self.update_button)
        later = QPushButton(BUTTON_LATER, self)
        later.clicked.connect(self.reject)
        row.addWidget(later)
        column.addLayout(row)

    def _build_versions(self, installed: int, available: int) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        row.addLayout(self._version_box(INSTALLED_CAPTION, installed, highlight=False))
        arrow = QLabel(ARROW, self)
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(arrow)
        row.addLayout(self._version_box(AVAILABLE_CAPTION, available, highlight=True))
        row.addStretch(1)
        return row

    def _version_box(self, caption: str, version: int, *, highlight: bool) -> QVBoxLayout:
        box = QVBoxLayout()
        label = QLabel(caption, self)
        label.setObjectName("mutedLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(label)
        value = QLabel(f"v{int(version)}", self)
        value.setObjectName("updateVersion" if highlight else "mutedLabel")
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(value)
        if highlight:
            self.available_label = value
        else:
            self.installed_label = value
        return box

    def _update(self) -> None:
        if self.on_update is not None:
            self.on_update()
        self.accept()


def build_model_update_dialog(
    win: Any,
    *,
    model_name: str,
    installed_version: int,
    available_version: int,
    info: Any = None,
    on_update: Callable[[], None] | None = None,
) -> ModelUpdateDialog:
    return ModelUpdateDialog(
        win,
        model_name=model_name,
        installed_version=installed_version,
        available_version=available_version,
        info=info,
        on_update=on_update,
    )
