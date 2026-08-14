"""Classification history — the right-hand panel (CLAUDE.md §5).

A fixed-position tile grid with a "snake" of border colours trailing the
newest classification — the same ring-buffer semantics as the Windows app's
Monitor, and those semantics are **intentional** (Seth, PR #30 feedback):
images must never scroll or shift position, or the operator can't track a
case by where it sits.
New records overwrite the oldest cell in place; capacity is however many
tiles fit the space the host gives the widget, reflowing on resize (wider
window → more columns). The recency trail uses the same neutral (hue-free)
palette roles the rest of the chrome uses for focus/selection — see
CLAUDE.md's "Hue is meaning" note.

Subscribes ``run/history`` on ``win.bus`` at construction; payload shape is
``{"image": <BGR ndarray>, "label", "parent", "confidence", "slot"}``, the same
one ``RunController`` posts and ``qtui.app._on_run_history`` reads for the Sort
page's current-case panel. The running case number (WinForms parity, Seth) is
stamped here, on the live path only — a zoom rebuild replays the stored
records, so each keeps the number it was given.

Thumbnails are decoded to ``QPixmap`` on arrival, on the main thread: a
classification lands here at most once per case, not in bulk, so there is no
batch of frames to justify a worker thread the way the live camera preview
would.

A slim "Zoom" bar at the bottom (below the tile grid — Seth/JL, 2026-08-13)
carries a 50-200% slider for the tile footprint,
thumbnail and entry fonts alike — capacity is derived from tile size ÷ widget
size, so a bigger tile naturally holds fewer, larger records and a smaller
tile holds more, smaller ones (Seth, 2026-08-13). The slider drags with
``setTracking(False)``: a live "%d%%" label follows the handle via
``sliderMoved``, but the expensive tile rebuild only fires on release
(``valueChanged``), so dragging through the range doesn't rebuild the grid at
every intermediate value. A zoom change rebuilds every tile at the new size
from the recency list, applying the same "keep newest" rule as a plain
shrink; the choice persists via ``SettingsRepo`` under ``SETTING_HISTORY_ZOOM``
as a plain integer string (JL, 2026-08-13 — was a combo of preset labels;
a legacy ``"150%"`` value is migrated by stripping the sign).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# Base (100% zoom) sizes. A view's *current* tile/thumb size lives on the
# instance (``_tile_w``/``_tile_h``/``_thumb``) — read those, not these
# constants, once zoom is in play.
THUMB = 56
PREVIEW_SIZE = 320
# One tile's footprint at 100% zoom; capacity = how many fit the widget's
# current size ÷ the current (possibly zoomed) footprint.
TILE_W = 190
TILE_H = 72
GUTTER = 6
# Before the widget has a real size (dock hidden, offscreen tests without an
# explicit resize) capacity falls back to this grid rather than 1×1, so
# records pushed while unmapped are still there when the dock opens. Zoom
# does not affect this fallback — it's an emergency buffer, not a layout.
FALLBACK_COLS = 4
FALLBACK_ROWS = 10
EMPTY_TEXT = "Recent classifications will appear here."

# Newest -> oldest border tint for the trailing "snake". Neutral roles only
# (focus/selection brightness, not hue) so recency reads the same way in
# every theme, including the hue-free surfaces the tinted themes keep.
SNAKE_ROLES = ("border_focus", "accent", "accent_dim")

# Uniform tile scale, user-selectable from the zoom slider (percent of the
# base TILE_W/TILE_H/THUMB sizes above).
ZOOM_MIN = 50
ZOOM_MAX = 200
ZOOM_DEFAULT = 100
ZOOM_STEP = 25  # pageStep and tick interval
SETTING_HISTORY_ZOOM = "ui.history_zoom"


def _parse_zoom_percent(raw: Any) -> int:
    """Parse a stored zoom value, clamped to [ZOOM_MIN, ZOOM_MAX].

    Migrates the legacy combo-box format (``"150%"``) by stripping the sign;
    anything unparsable falls back to :data:`ZOOM_DEFAULT`.
    """
    text = str(raw).strip()
    if text.endswith("%"):
        text = text[:-1]
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return ZOOM_DEFAULT
    return max(ZOOM_MIN, min(ZOOM_MAX, value))


def _bgr_to_pixmap(image: Any, size: int) -> QPixmap:
    """A BGR numpy frame as a square QPixmap; anything else renders blank.

    ``QImage`` borrows the buffer it is handed, so ``.copy()`` cuts the result
    loose — same technique as ``qtui.app.frame_to_image`` and
    ``dialog_image_preview.bgr_to_pixmap``, duplicated rather than imported so
    this module has no dependency on the main window or another dialog.
    """
    if not isinstance(image, np.ndarray) or image.size == 0:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        return pixmap
    buffer = np.ascontiguousarray(image)
    height, width = buffer.shape[:2]
    qimage = QImage(buffer.data, width, height, buffer.strides[0], QImage.Format.Format_BGR888).copy()
    return QPixmap.fromImage(qimage).scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


def _slot_text(slot: Any) -> str:
    try:
        slot_number = int(slot or 0)
    except (TypeError, ValueError):
        slot_number = 0
    return "Catch-All" if slot_number == 0 else f"Slot {slot_number}"


def _confidence(record: dict[str, Any]) -> float:
    try:
        return float(record.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


class HistoryEntry(QFrame):
    """One classification: thumbnail + label + confidence + slot, click to enlarge."""

    clicked = Signal(object)  # emits its own record dict

    def __init__(
        self,
        parent: QWidget | None = None,
        tile_w: int = TILE_W,
        tile_h: int = TILE_H,
        thumb: int = THUMB,
        font_scale: float = 1.0,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("historyEntry")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Fixed footprint: the grid's capacity math depends on every tile
        # being exactly tile_w × tile_h (the zoomed TILE_W × TILE_H).
        self.setFixedSize(tile_w, tile_h)
        self._thumb = thumb
        self.record: dict[str, Any] = {}
        self.below_floor = False

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)

        self.thumb_label = QLabel(self)
        self.thumb_label.setFixedSize(thumb, thumb)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.thumb_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.label_label = QLabel(self)
        self.label_label.setObjectName("historyLabel")
        self.confidence_label = QLabel(self)
        self.confidence_label.setObjectName("historyConfidence")
        self.slot_label = QLabel(self)
        self.slot_label.setObjectName("historySlot")
        if font_scale != 1.0:
            for text_label in (self.label_label, self.confidence_label, self.slot_label):
                font = QFont(text_label.font())
                base_pt = font.pointSizeF()
                if base_pt > 0:
                    font.setPointSizeF(base_pt * font_scale)
                text_label.setFont(font)
        text_col.addWidget(self.label_label)
        text_col.addWidget(self.confidence_label)
        text_col.addWidget(self.slot_label)
        row.addLayout(text_col, 1)

        # The running case number, WinForms-style: big, green, right-aligned
        # (Seth: "the counter in those panels [must] be consistent").
        self.number_label = QLabel(self)
        self.number_label.setObjectName("historyNumber")
        self.number_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        number_font = QFont(self.number_label.font())
        base_pt = number_font.pointSizeF()
        if base_pt > 0:
            number_font.setPointSizeF(base_pt * 1.7 * font_scale)
        number_font.setBold(True)
        self.number_label.setFont(number_font)
        row.addWidget(self.number_label)

    def set_record(self, record: dict[str, Any]) -> None:
        self.record = record
        self.thumb_label.setPixmap(_bgr_to_pixmap(record.get("image"), self._thumb))
        label = str(record.get("label") or "(empty)")
        parent = record.get("parent")
        self.label_label.setText(f"{parent} · {label}" if parent else label)
        self.confidence_label.setText(f"{_confidence(record):.0f}%")
        self.slot_label.setText(_slot_text(record.get("slot", 0)))
        number = record.get("number")
        self.number_label.setText(str(number) if number else "")

    def apply_style(self, colors: dict[str, str], *, highlight: str | None) -> None:
        """Card chrome + recency border. Re-run on every push and on a theme switch."""
        border = highlight or colors.get("border", "#3a3a3a")
        width = 2 if highlight else 1
        self.setStyleSheet(
            "QFrame#historyEntry {"
            f"background-color: {colors.get('bg_card', '#272727')};"
            f"border: {width}px solid {border};"
            "border-radius: 4px;"
            "}"
            "QFrame#historyEntry:hover {"
            f"background-color: {colors.get('bg_card_hover', '#333333')};"
            "}"
            "QLabel { background: transparent; }"
        )
        self.label_label.setStyleSheet(f"color: {colors.get('text', '#d4d4d4')}; font-weight: bold;")
        # Accent, not the WinForms green: hue is meaning here (ui/theme.py) and
        # green says "success" — but below-floor misses get numbered too. The
        # accent family is emphasis without a verdict. One word to revert if
        # Seth wants literal parity.
        self.number_label.setStyleSheet(f"color: {colors.get('accent', '#6ea8fe')};")
        conf_color = colors.get("warning", "#f59e0b") if self.below_floor else colors.get("text_muted", "#9a9a9a")
        self.confidence_label.setStyleSheet(f"color: {conf_color};")
        self.slot_label.setStyleSheet(f"color: {colors.get('text_muted', '#9a9a9a')};")

    def mousePressEvent(self, event: Any) -> None:
        self.clicked.emit(self.record)
        super().mousePressEvent(event)


class HistoryPreviewDialog(QDialog):
    """Enlarged single-record view, opened from a click on a `HistoryEntry`."""

    def __init__(self, parent: QWidget | None, record: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Classification detail")
        column = QVBoxLayout(self)

        self.image_label = QLabel(self)
        self.image_label.setObjectName("imagePreview")
        self.image_label.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setPixmap(_bgr_to_pixmap(record.get("image"), PREVIEW_SIZE))
        column.addWidget(self.image_label)

        label = str(record.get("label") or "(empty)")
        parent_name = record.get("parent")
        self.title_label = QLabel(f"{parent_name} · {label}" if parent_name else label, self)
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        column.addWidget(self.title_label)

        self.detail_label = QLabel(f"{_confidence(record):.0f}% · {_slot_text(record.get('slot', 0))}", self)
        self.detail_label.setObjectName("mutedLabel")
        column.addWidget(self.detail_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        column.addWidget(buttons)


class HistoryView(QWidget):
    """Fixed-tile classification history: a ring buffer over a reflowing grid.

    Semantics ported from ``ui/monitor.MonitorWindow`` (and deliberately from
    the Windows app): tiles never move or scroll — a new record overwrites the
    oldest cell in place, and the recency snake is what points at "current".
    Capacity is whatever fits the widget's size; a resize reflows the grid,
    discarding the oldest records if it shrank.

    Construct via :func:`build_history_view`, not directly — that keeps the
    one required argument (the main window) obvious at the call site.
    """

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        self._tiles: list[HistoryEntry] = []  # fixed grid positions
        self._entries: list[HistoryEntry] = []  # newest first (drives the snake)
        self._write_index = 0
        self._case_number = 0
        self._capacity = 0
        self._cols = 1
        self._rows = 1
        # Swappable like ImagePreviewDialog's notify/confirm: a test replaces
        # this to observe a click without a modal ever opening.
        self.open_preview: Any = self._open_preview_dialog

        self._zoom_percent = self._load_zoom_percent()
        self._factor = self._zoom_percent / 100.0
        self._tile_w = round(TILE_W * self._factor)
        self._tile_h = round(TILE_H * self._factor)
        self._thumb = round(THUMB * self._factor)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.empty_label = QLabel(EMPTY_TEXT, self)
        self.empty_label.setObjectName("mutedLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        # Stretch 1 like the grid below: whichever of the two is visible must
        # soak up the surplus height, or the layout splits it between the
        # label and the zoom bar and the bar floats mid-panel (JL screenshot).
        outer.addWidget(self.empty_label, 1)

        self.grid_area = QWidget(self)
        self._grid = QGridLayout(self.grid_area)
        self._grid.setContentsMargins(GUTTER, GUTTER, GUTTER, GUTTER)
        self._grid.setSpacing(GUTTER)
        # Pin tiles to the top-left so partial fills look like the Windows
        # monitor, not a centered cloud.
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.grid_area.hide()
        outer.addWidget(self.grid_area, 1)

        # Zoom bar sits at the *bottom* of the dock (Seth/JL, 2026-08-13) —
        # below the tile grid, not above it.
        zoom_bar = QHBoxLayout()
        zoom_bar.setContentsMargins(GUTTER, 4, GUTTER, 4)
        zoom_bar.setSpacing(6)
        zoom_caption = QLabel("Zoom", self)
        zoom_caption.setObjectName("mutedLabel")
        zoom_bar.addWidget(zoom_caption)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setObjectName("historyZoomSlider")
        self.zoom_slider.setRange(ZOOM_MIN, ZOOM_MAX)
        self.zoom_slider.setPageStep(ZOOM_STEP)
        self.zoom_slider.setTickInterval(ZOOM_STEP)
        self.zoom_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.zoom_slider.setFixedWidth(140)
        self.zoom_slider.setValue(self._zoom_percent)
        # Rebuilding the tile grid is the expensive part; only do it once the
        # user settles on a value, not on every intermediate pixel of drag.
        self.zoom_slider.setTracking(False)
        self.zoom_slider.sliderMoved.connect(self._on_zoom_slider_moved)
        self.zoom_slider.valueChanged.connect(self._on_zoom_value_changed)
        zoom_bar.addWidget(self.zoom_slider)
        self.zoom_value_label = QLabel(f"{self._zoom_percent}%", self)
        self.zoom_value_label.setObjectName("mutedLabel")
        zoom_bar.addWidget(self.zoom_value_label)
        zoom_bar.addStretch(1)
        outer.addLayout(zoom_bar)

        win.bus.subscribe("run/history", self._on_history)

    # ----- layout --------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._recompute_capacity()

    def _recompute_capacity(self) -> None:
        width = self.grid_area.width()
        height = self.grid_area.height()
        if width < self._tile_w + GUTTER or height < self._tile_h + GUTTER:
            # Not laid out yet (hidden dock, unshown test widget): keep a
            # usable buffer instead of collapsing to one cell.
            cols, rows = FALLBACK_COLS, FALLBACK_ROWS
        else:
            cols = max(1, width // (self._tile_w + GUTTER))
            rows = max(1, height // (self._tile_h + GUTTER))
        capacity = cols * rows
        if capacity == self._capacity and cols == self._cols and rows == self._rows:
            return
        self._cols = cols
        self._rows = rows
        self._capacity = capacity
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        """Re-place tiles for the current capacity, keeping the newest records.

        When the grid shrinks, the oldest
        records (by recency) are discarded, survivors re-pack into the first
        positions oldest-first so the next overwrite hits the oldest cell,
        and the write cursor resets.
        """
        if len(self._tiles) > self._capacity:
            keep = set(self._entries[: self._capacity])
            for tile in self._tiles:
                if tile not in keep:
                    self._grid.removeWidget(tile)
                    tile.deleteLater()
            self._tiles = [t for t in reversed(self._entries) if t in keep]
            self._entries = [t for t in self._entries if t in keep]
            self._write_index = 0

        for index, tile in enumerate(self._tiles):
            row, col = self._position(index)
            self._grid.addWidget(tile, row, col)
        self._recolor()

    def _position(self, index: int) -> tuple[int, int]:
        """Top-to-bottom, then left-to-right — the Windows monitor's fill order."""
        rows = max(1, self._rows)
        return index % rows, index // rows

    # ----- record push ---------------------------------------------------------

    def _on_history(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        # Stamp the running case number here, on the LIVE path only — a zoom
        # replay reuses the stored dicts, so every record keeps the number it
        # arrived with (WinForms keeps counting across runs; so does this).
        self._case_number += 1
        payload = {**payload, "number": self._case_number}
        self._push_record(payload)

    def _make_tile(self) -> HistoryEntry:
        tile = HistoryEntry(self.grid_area, self._tile_w, self._tile_h, self._thumb, self._factor)
        tile.clicked.connect(lambda record: self.open_preview(record))
        return tile

    def _push_record(self, record: dict[str, Any]) -> None:
        """Ring-buffer insert: reuse the oldest tile once at capacity.

        Shared by ``_on_history`` (a live classification) and ``_apply_zoom``
        (replaying the recency list into freshly-sized tiles) — same rule
        either way, so a zoom change can't diverge from a live push.
        """
        if self._capacity <= 0:
            self._recompute_capacity()

        if len(self._tiles) < self._capacity:
            tile = self._make_tile()
            self._tiles.append(tile)
            row, col = self._position(len(self._tiles) - 1)
            self._grid.addWidget(tile, row, col)
            tile.show()
        else:
            if self._write_index >= len(self._tiles):
                self._write_index = 0
            tile = self._tiles[self._write_index]
            self._write_index = (self._write_index + 1) % len(self._tiles)

        tile.set_record(record)
        floor = float(getattr(self._win.config, "run_confidence_floor", 0) or 0)
        tile.below_floor = floor > 0 and _confidence(record) < floor
        if tile in self._entries:
            self._entries.remove(tile)
        self._entries.insert(0, tile)
        self._recolor()
        self._update_empty_state()

    def _recolor(self) -> None:
        colors = self._win.palette_colors
        for index, entry in enumerate(self._entries):
            role = SNAKE_ROLES[index] if index < len(SNAKE_ROLES) else None
            highlight = colors.get(role) if role else None
            entry.apply_style(colors, highlight=highlight)

    def _update_empty_state(self) -> None:
        has_entries = bool(self._entries)
        self.empty_label.setVisible(not has_entries)
        self.grid_area.setVisible(has_entries)

    # ----- zoom --------------------------------------------------------------------

    def _load_zoom_percent(self) -> int:
        db = getattr(self._win, "db", None)
        if db is None:
            return ZOOM_DEFAULT
        try:
            from ..data.repository import SettingsRepo

            raw = SettingsRepo(db).get(SETTING_HISTORY_ZOOM, str(ZOOM_DEFAULT))
        except Exception:
            return ZOOM_DEFAULT
        return _parse_zoom_percent(raw)

    def _save_zoom_percent(self, percent: int) -> None:
        db = getattr(self._win, "db", None)
        if db is None:
            return
        try:
            from ..data.repository import SettingsRepo

            SettingsRepo(db).set(SETTING_HISTORY_ZOOM, str(percent))
        except Exception:
            pass  # a preference that can't be persisted still applies this session

    def _on_zoom_slider_moved(self, value: int) -> None:
        """Live label only — the tile rebuild waits for release (``valueChanged``)."""
        self.zoom_value_label.setText(f"{value}%")

    def _on_zoom_value_changed(self, value: int) -> None:
        self.zoom_value_label.setText(f"{value}%")
        if value == self._zoom_percent:
            return
        self._apply_zoom(value)
        self._save_zoom_percent(value)

    def _apply_zoom(self, percent: int) -> None:
        """Rebuild every tile at the new size from the recency list.

        Existing tiles are the wrong footprint for the new zoom, so rather
        than resize them in place this tears them all down and replays the
        records (oldest first) through :meth:`_push_record` — the same path
        a live classification takes, so a shrunk capacity discards the
        oldest exactly as a plain widget-resize shrink would.
        """
        records = [tile.record for tile in self._entries]  # newest first

        self._zoom_percent = percent
        self._factor = percent / 100.0
        self._tile_w = round(TILE_W * self._factor)
        self._tile_h = round(TILE_H * self._factor)
        self._thumb = round(THUMB * self._factor)

        for tile in self._tiles:
            self._grid.removeWidget(tile)
            tile.deleteLater()
        self._tiles = []
        self._entries = []
        self._write_index = 0
        # Sentinels so _recompute_capacity can't shortcut on an unchanged
        # cols/rows/capacity tuple — the tile size behind them did change.
        self._cols = -1
        self._rows = -1
        self._capacity = -1
        self._recompute_capacity()

        for record in reversed(records):
            self._push_record(record)

    # ----- theme -----------------------------------------------------------------

    def apply_palette(self) -> None:
        """Re-paint every entry from ``win.palette_colors``. Call after a theme switch.

        Entry borders and the confidence warning colour are baked into each
        widget's own stylesheet (same reason ``ui/serial_monitor.py``'s text
        tags need an explicit ``apply_palette`` call rather than a generic
        retheme sweep), so a switch has to re-run this explicitly.
        """
        muted = f"color: {self._win.palette_colors.get('text_muted', '#9a9a9a')};"
        self.empty_label.setStyleSheet(muted)
        self._recolor()

    # ----- preview ---------------------------------------------------------------

    def _open_preview_dialog(self, record: dict[str, Any]) -> None:
        HistoryPreviewDialog(self, record).exec()

    # ----- lifecycle ---------------------------------------------------------------

    def unsubscribe(self) -> None:
        """Detach from the bus. Call when the host (dock or page) is torn down."""
        try:
            self._win.bus.unsubscribe("run/history", self._on_history)
        except Exception:
            pass

    def closeEvent(self, event: Any) -> None:
        self.unsubscribe()
        super().closeEvent(event)


def build_history_view(win: Any) -> HistoryView:
    """Build the classification history widget for ``win`` (a ``QtMainWindow``).

    The caller decides how to host it; today that is the Classification
    History panel, which needs nothing but the one widget instance.
    """
    return HistoryView(win)
