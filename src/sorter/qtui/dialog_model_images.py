"""Training-image browser: filter/count, thumbnail grid, reclassify/delete.

Thumbnails are decoded and downscaled on a background ``QThread`` — cv2
decode of a full-size image is too slow to do a page of on the UI thread —
and handed back as plain BGR numpy arrays via a queued signal; ``QPixmap``
construction happens on receipt, on the main thread.

``notify``/``confirm`` are attributes, not direct ``QMessageBox`` calls, so a
test can drive Reclassify/Delete without a modal.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..data import image_store
from ..data.repository import HeadstampRepo
from ..paths import model_images_dir
from .dialog_image_preview import ImagePreviewDialog, bgr_to_pixmap

_TILE_W = 140
_THUMB = 120
_GUTTER = 8
_PAGE_SIZES = (20, 50, 100, 150, 200)
_DEFAULT_PAGE_SIZE = 50
# Approximates the palette's `bg_input` role at decode time (baked into the
# thumbnail's pixels, so it can't live-retheme).
_THUMB_BG = 11


def _make_thumbnail(frame: np.ndarray, size: int) -> np.ndarray:
    """Downscale + letterbox a BGR frame into a ``size`` x ``size`` square."""
    import cv2

    height, width = frame.shape[:2]
    scale = min(size / width, size / height)
    new_w, new_h = max(1, round(width * scale)), max(1, round(height * scale))
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.full((size, size, 3), _THUMB_BG, dtype=np.uint8)
    y0, x0 = (size - new_h) // 2, (size - new_w) // 2
    canvas[y0 : y0 + new_h, x0 : x0 + new_w] = resized
    return canvas


class _ThumbWorker(QThread):
    """Decodes + downscales one page of images off the UI thread."""

    ready = Signal(int, str, object)  # token, path, BGR ndarray thumbnail (or None on failed decode)

    def __init__(self, token: int, paths: list[str], size: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.token = token
        self._paths = paths
        self._size = size

    def run(self) -> None:
        import cv2

        for path in self._paths:
            if self.isInterruptionRequested():
                return
            frame = cv2.imread(path)
            thumb = _make_thumbnail(frame, self._size) if frame is not None else None
            self.ready.emit(self.token, path, thumb)


class _ThumbTile(QFrame):
    """One image tile: thumbnail + caption, click to select, double-click to preview."""

    clicked = Signal(str)
    doubleClicked = Signal(str)

    def __init__(self, path: str, headstamp: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.headstamp = headstamp
        self.selected = False
        self.setObjectName("thumbTile")
        self.setFixedWidth(_TILE_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        column = QVBoxLayout(self)
        column.setContentsMargins(4, 4, 4, 4)
        column.setSpacing(2)
        self.image_label = QLabel(self)
        self.image_label.setObjectName("thumbImage")
        self.image_label.setFixedSize(_THUMB, _THUMB)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.image_label, 0, Qt.AlignmentFlag.AlignHCenter)
        caption = headstamp if len(headstamp) <= 16 else headstamp[:15] + "…"
        self.caption_label = QLabel(caption, self)
        self.caption_label.setObjectName("thumbCaption")
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.caption_label)

    def set_pixmap(self, pixmap: Any) -> None:
        self.image_label.setPixmap(pixmap)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.setProperty("selected", selected)
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def mousePressEvent(self, event: Any) -> None:
        self.clicked.emit(self.path)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        self.doubleClicked.emit(self.path)
        super().mouseDoubleClickEvent(event)


class ModelImagesDialog(QDialog):
    """Filterable, paged thumbnail grid over one model's training images."""

    def __init__(
        self,
        parent: QWidget | None,
        config: Any,
        model_id: int,
        images_dir: Path | str | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.model_id = model_id
        self.images_dir = Path(images_dir) if images_dir is not None else model_images_dir(model_id)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._headstamp_names = [h.name for h in HeadstampRepo(config.db).list_for_model(model_id)]

        self.setWindowTitle("Training images")
        self.resize(820, 560)

        self._filtered: list[Path] = []
        self.tiles: dict[str, _ThumbTile] = {}
        self._cols = 0
        self._page_index = 0
        self._page_size = _DEFAULT_PAGE_SIZE
        self._load_token = 0
        self._worker: _ThumbWorker | None = None
        # Attributes, not methods: a test replaces them so nothing modal opens.
        self.notify: Callable[[str, str], None] = self._warn
        self.confirm: Callable[[str, str], bool] = self._ask

        self._build_ui()
        self.refresh()

    # ----- construction ---------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Show", self))
        self.filter_combo = QComboBox(self)
        self.filter_combo.currentTextChanged.connect(lambda _text: self.refresh(reset_page=True))
        top.addWidget(self.filter_combo)
        top.addSpacing(12)
        top.addWidget(QLabel("Per page", self))
        self.pagesize_combo = QComboBox(self)
        self.pagesize_combo.addItems([str(n) for n in _PAGE_SIZES])
        self.pagesize_combo.setCurrentText(str(_DEFAULT_PAGE_SIZE))
        self.pagesize_combo.currentTextChanged.connect(self._on_pagesize)
        top.addWidget(self.pagesize_combo)
        top.addSpacing(12)
        self.open_folder_button = QPushButton("Open folder", self)
        self.open_folder_button.clicked.connect(self._open_folder)
        top.addWidget(self.open_folder_button)
        top.addStretch(1)
        self.counts_label = QLabel("", self)
        self.counts_label.setObjectName("mutedLabel")
        top.addWidget(self.counts_label)
        column.addLayout(top)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_host = QWidget(self._scroll)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setSpacing(_GUTTER)
        self._scroll.setWidget(self._grid_host)
        self.empty_label = QLabel("No images.", self._grid_host)
        self.empty_label.setObjectName("mutedLabel")
        self.empty_label.hide()
        column.addWidget(self._scroll, 1)

        pager = QHBoxLayout()
        self.first_button = QPushButton("⏮ First", self)
        self.prev_button = QPushButton("◀ Prev", self)
        self.next_button = QPushButton("Next ▶", self)
        self.last_button = QPushButton("Last ⏭", self)
        self.first_button.clicked.connect(lambda: self._go(0))
        self.prev_button.clicked.connect(lambda: self._go(self._page_index - 1))
        self.next_button.clicked.connect(lambda: self._go(self._page_index + 1))
        self.last_button.clicked.connect(lambda: self._go(self._page_count() - 1))
        pager.addWidget(self.first_button)
        pager.addWidget(self.prev_button)
        self.page_label = QLabel("", self)
        self.page_label.setObjectName("mutedLabel")
        pager.addWidget(self.page_label)
        pager.addStretch(1)
        pager.addWidget(self.last_button)
        pager.addWidget(self.next_button)
        column.addLayout(pager)

        actions = QHBoxLayout()
        self.selection_label = QLabel("0 selected", self)
        self.selection_label.setObjectName("mutedLabel")
        actions.addWidget(self.selection_label)
        actions.addStretch(1)
        actions.addWidget(QLabel("to", self))
        self.target_combo = QComboBox(self)
        self.target_combo.addItems(self._headstamp_names)
        actions.addWidget(self.target_combo)
        self.reclassify_button = QPushButton("Reclassify selected", self)
        self.reclassify_button.clicked.connect(self.reclassify_selected)
        actions.addWidget(self.reclassify_button)
        self.delete_button = QPushButton("Delete selected", self)
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete_selected)
        actions.addWidget(self.delete_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        column.addLayout(actions)

        self._populate_filter_combo()
        self._update_selection()

    def _populate_filter_combo(self) -> None:
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItems([image_store.ALL_FILTER, *self._headstamp_names, image_store.UNKNOWN_FILTER])
        self.filter_combo.setCurrentText(image_store.ALL_FILTER)
        self.filter_combo.blockSignals(False)

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    # ----- data + paging ----------------------------------------------------

    @property
    def loading(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _scan_filter(self) -> None:
        files = image_store.list_images(self.images_dir)
        self._filtered = image_store.filter_images(files, self.filter_combo.currentText(), self._headstamp_names)
        self._page_index = min(self._page_index, max(0, self._page_count() - 1))

    def _page_count(self) -> int:
        if not self._filtered:
            return 0
        return (len(self._filtered) + self._page_size - 1) // self._page_size

    def _page_files(self) -> list[Path]:
        start = self._page_index * self._page_size
        return self._filtered[start : start + self._page_size]

    def _on_pagesize(self, text: str) -> None:
        try:
            self._page_size = int(text)
        except ValueError:
            self._page_size = _DEFAULT_PAGE_SIZE
        self.refresh(reset_page=True)

    def _go(self, page_index: int) -> None:
        page_index = max(0, min(page_index, max(0, self._page_count() - 1)))
        if page_index != self._page_index:
            self._page_index = page_index
            self.refresh()

    def refresh(self, *, reset_page: bool = False) -> None:
        """Re-scan the folder, rebuild the visible page, and kick off thumbnail decode."""
        if reset_page:
            self._page_index = 0
        self._load_token += 1
        token = self._load_token
        self._scan_filter()
        self._build_tiles()
        self._update_pager()
        self._update_selection()
        page = self._page_files()
        if page:
            self._start_worker(token, [str(p) for p in page])

    def _build_tiles(self) -> None:
        # `empty_label` is shared across reloads; skip it so an empty->populated
        # transition doesn't deleteLater() the widget the next empty state needs.
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.empty_label:
                widget.setParent(None)
                widget.deleteLater()
        self.tiles = {}
        self._cols = 0
        page = self._page_files()
        if not page:
            self.empty_label.show()
            self._grid.addWidget(self.empty_label, 0, 0)
            return
        self.empty_label.hide()
        for path in page:
            tile = _ThumbTile(str(path), image_store.parse_headstamp(path), self._grid_host)
            tile.clicked.connect(self._toggle_tile)
            tile.doubleClicked.connect(self._open_preview)
            tile.customContextMenuRequested.connect(lambda pos, t=tile: self._show_context(t, pos))
            self.tiles[str(path)] = tile
        self._reflow(force=True)

    def _reflow(self, *, force: bool = False) -> None:
        if not self.tiles:
            return
        width = max(1, self._scroll.viewport().width())
        cols = max(1, width // (_TILE_W + _GUTTER))
        if not force and cols == self._cols:
            return
        self._cols = cols
        for i, tile in enumerate(self.tiles.values()):
            row, col = divmod(i, cols)
            self._grid.addWidget(tile, row, col)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._reflow()

    # ----- thumbnail loading (worker thread) --------------------------------

    def _start_worker(self, token: int, paths: list[str]) -> None:
        if self._worker is not None:
            self._worker.requestInterruption()
        worker = _ThumbWorker(token, paths, _THUMB, self)
        worker.ready.connect(self._on_thumb_ready)
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._worker = worker
        worker.start()

    def _on_worker_finished(self, worker: _ThumbWorker) -> None:
        # Drop the reference before deleteLater() — otherwise the next refresh's
        # requestInterruption()/wait() touches an already-deleted C++ object.
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _on_thumb_ready(self, token: int, path: str, thumb: Any) -> None:
        if token != self._load_token or thumb is None:
            return
        tile = self.tiles.get(path)
        if tile is not None:
            tile.set_pixmap(bgr_to_pixmap(thumb, _THUMB))

    # ----- selection ---------------------------------------------------------

    def _toggle_tile(self, path: str) -> None:
        tile = self.tiles.get(path)
        if tile is None:
            return
        tile.set_selected(not tile.selected)
        self._update_selection()

    def _selected_tiles(self) -> list[_ThumbTile]:
        return [t for t in self.tiles.values() if t.selected]

    def _update_selection(self) -> None:
        count = len(self._selected_tiles())
        self.selection_label.setText(f"{count} selected")
        self.reclassify_button.setEnabled(bool(count and self._headstamp_names))
        self.delete_button.setEnabled(bool(count))

    def _update_pager(self) -> None:
        total = len(self._filtered)
        count = self._page_count()
        if total == 0:
            self.page_label.setText("Page 0 of 0")
        else:
            start = self._page_index * self._page_size + 1
            end = min(total, (self._page_index + 1) * self._page_size)
            self.page_label.setText(f"Page {self._page_index + 1} of {count}   ({start}–{end} of {total})")
        self.counts_label.setText(f"{total} image" + ("" if total == 1 else "s"))
        at_first = self._page_index <= 0
        at_last = self._page_index >= count - 1 or count == 0
        self.first_button.setEnabled(not at_first)
        self.prev_button.setEnabled(not at_first)
        self.next_button.setEnabled(not at_last)
        self.last_button.setEnabled(not at_last)

    # ----- mutations -----------------------------------------------------------

    def reclassify_selected(self) -> None:
        target = self.target_combo.currentText()
        selected = self._selected_tiles()
        if not target or not selected:
            return
        for tile in selected:
            image_store.reclassify(tile.path, target)
        self.refresh()

    def delete_selected(self) -> None:
        selected = self._selected_tiles()
        if not selected:
            return
        if not self.confirm("Delete images", f"Delete {len(selected)} image(s)? This cannot be undone."):
            return
        failed = [tile for tile in selected if not image_store.delete(tile.path)]
        if failed:
            self.notify("Delete failed", f"{len(failed)} image(s) could not be deleted — are they open elsewhere?")
        self.refresh()

    def _open_preview(self, path: str) -> None:
        page_paths = self._page_files()
        try:
            index = page_paths.index(Path(path))
        except ValueError:
            index = 0
        dialog = ImagePreviewDialog(
            self, page_paths, index, self._headstamp_names, on_changed=lambda _change: self.refresh()
        )
        dialog.exec()

    def _show_context(self, tile: _ThumbTile, pos: Any) -> None:
        menu = QMenu(self)
        menu.addAction("Preview", lambda: self._open_preview(tile.path))
        reclass_menu = menu.addMenu("Reclassify to")
        reclass_menu.setEnabled(bool(self._headstamp_names))
        for name in self._headstamp_names:
            reclass_menu.addAction(name, lambda n=name: self._reclassify_one(tile, n))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self._delete_one(tile))
        menu.exec(tile.mapToGlobal(pos))

    def _reclassify_one(self, tile: _ThumbTile, name: str) -> None:
        image_store.reclassify(tile.path, name)
        self.refresh()

    def _delete_one(self, tile: _ThumbTile) -> None:
        if not self.confirm("Delete image", f"Delete this image?\n\n{Path(tile.path).name}"):
            return
        if not image_store.delete(tile.path):
            # A file that would not go must be said out loud (a silent False
            # left images behind on Windows), not swallowed.
            self.notify("Delete failed", f"Could not delete {Path(tile.path).name} — is it open elsewhere?")
        self.refresh()

    def _open_folder(self) -> None:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.images_dir))):
            self.notify("Open folder", str(self.images_dir))

    # ----- lifecycle -----------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        self._load_token += 1  # abandon any in-flight decode
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait(1000)
        super().closeEvent(event)
