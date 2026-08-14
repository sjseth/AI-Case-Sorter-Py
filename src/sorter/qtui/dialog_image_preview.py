"""Zoomed single-image preview with prev/next navigation, reclassify and delete.

Prev/next walks the caller's current page. Reclassify and delete keep the
dialog open afterward so browsing continues instead of forcing a reopen from
the grid.

``on_changed`` fires after every mutation so the caller (the grid) can resync:
    {"action": "reclassified", "old_path", "new_path", "new_headstamp"}
    {"action": "deleted", "old_path"}

``notify``/``confirm`` are attributes, not direct ``QMessageBox`` calls, so a
test can drive Reclassify/Delete without a modal.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..data import image_store

_PREVIEW = 480


def bgr_to_pixmap(frame: np.ndarray | None, size: int = _PREVIEW) -> QPixmap:
    """A BGR numpy frame as a square QPixmap; ``None`` (failed decode) renders blank.

    ``QImage`` borrows the buffer it is handed, so ``.copy()`` is what cuts the
    result loose — same technique as ``qtui.app.frame_to_image``, duplicated
    rather than imported: this module must not reach for the main window.
    """
    if frame is None:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.black)
        return pixmap
    buffer = np.ascontiguousarray(frame)
    height, width = buffer.shape[:2]
    image = QImage(buffer.data, width, height, buffer.strides[0], QImage.Format.Format_BGR888).copy()
    return QPixmap.fromImage(image).scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


class ImagePreviewDialog(QDialog):
    """One image at a time, with prev/next through ``paths`` and reclassify/delete."""

    def __init__(
        self,
        parent: QWidget | None,
        paths: Sequence[Path | str],
        index: int,
        headstamp_names: Sequence[str],
        *,
        on_changed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths: list[Path] = [Path(p) for p in paths]
        self._index = index
        self._headstamp_names = list(headstamp_names)
        self._on_changed = on_changed
        # Attributes, not methods: a test replaces them so nothing modal opens.
        self.notify: Callable[[str, str], None] = self._warn
        self.confirm: Callable[[str, str], bool] = self._ask

        self.setWindowTitle("Image preview")
        column = QVBoxLayout(self)

        self.image_label = QLabel(self)
        self.image_label.setObjectName("imagePreview")
        self.image_label.setFixedSize(_PREVIEW, _PREVIEW)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.image_label)

        self.info_label = QLabel(self)
        self.info_label.setObjectName("mutedLabel")
        column.addWidget(self.info_label)

        nav = QHBoxLayout()
        self.prev_button = QPushButton("◀ Prev", self)
        self.next_button = QPushButton("Next ▶", self)
        self.prev_button.clicked.connect(self.show_prev)
        self.next_button.clicked.connect(self.show_next)
        nav.addWidget(self.prev_button)
        nav.addStretch(1)
        nav.addWidget(self.next_button)
        column.addLayout(nav)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("Reclassify to", self))
        self.target_combo = QComboBox(self)
        self.target_combo.addItems(self._headstamp_names)
        actions.addWidget(self.target_combo)
        self.reclassify_button = QPushButton("Reclassify", self)
        self.reclassify_button.setObjectName("action")
        self.reclassify_button.clicked.connect(self.reclassify)
        actions.addWidget(self.reclassify_button)
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete_current)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        column.addLayout(actions)

        self._render()

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    @property
    def current_path(self) -> Path:
        return self._paths[self._index]

    @property
    def index(self) -> int:
        return self._index

    def _render(self) -> None:
        path = self.current_path
        import cv2

        self.image_label.setPixmap(bgr_to_pixmap(cv2.imread(str(path))))
        headstamp = image_store.parse_headstamp(path)
        self.info_label.setText(
            f"Classification: {headstamp}    File: {path.name}    ({self._index + 1} of {len(self._paths)})"
        )
        combo_index = self.target_combo.findText(headstamp)
        if combo_index >= 0:
            self.target_combo.setCurrentIndex(combo_index)
        elif self.target_combo.count():
            self.target_combo.setCurrentIndex(0)
        self.prev_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index < len(self._paths) - 1)
        self.reclassify_button.setEnabled(bool(self._headstamp_names))

    def show_prev(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._render()

    def show_next(self) -> None:
        if self._index < len(self._paths) - 1:
            self._index += 1
            self._render()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Left:
            self.show_prev()
        elif event.key() == Qt.Key.Key_Right:
            self.show_next()
        elif event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    def reclassify(self) -> None:
        target = self.target_combo.currentText()
        current = image_store.parse_headstamp(self.current_path)
        if not target or target == current:
            return
        new_path = image_store.reclassify(self.current_path, target)
        if new_path is None:
            self.notify("Reclassify failed", "Could not reclassify — a file for that headstamp may already exist.")
            return
        old_path = self._paths[self._index]
        self._paths[self._index] = new_path
        if self._on_changed:
            self._on_changed(
                {
                    "action": "reclassified",
                    "old_path": str(old_path),
                    "new_path": str(new_path),
                    "new_headstamp": target,
                }
            )
        self._render()

    def delete_current(self) -> None:
        path = self.current_path
        if not self.confirm("Delete image", f"Delete this image?\n\n{path.name}"):
            return
        if not image_store.delete(path):
            self.notify("Delete failed", "Could not delete the file.")
            return
        del self._paths[self._index]
        if self._on_changed:
            self._on_changed({"action": "deleted", "old_path": str(path)})
        if not self._paths:
            self.accept()
            return
        if self._index >= len(self._paths):
            self._index = len(self._paths) - 1
        self._render()
