"""Classification history: the ring-buffer tile grid and its recency trail.

Everything here runs offscreen against a real SQLite-backed ``Config`` and,
for the end-to-end case, the in-process serial emulator — no display, no
hardware, no network. Mirrors ``test_qt_sort.py``'s fixtures and its
emulator + manual-feed pattern rather than importing from it, so this module
stays independent of another increment's in-flight file.
"""

from __future__ import annotations

import types
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from sorter.hardware.serial_emulator import EMULATED_PORT
from sorter.ml import classifier
from sorter.qtui.history_view import (
    FALLBACK_COLS,
    FALLBACK_ROWS,
    GUTTER,
    SETTING_HISTORY_ZOOM,
    SNAKE_ROLES,
    THUMB,
    TILE_H,
    TILE_W,
    ZOOM_DEFAULT,
    ZOOM_MAX,
    HistoryPreviewDialog,
    build_history_view,
)

from .conftest import drain_until

_IMAGE = np.zeros((16, 16, 3), np.uint8)


def history(label: str, confidence: float, slot: int = 1, parent: str | None = None) -> dict:
    """A ``run/history`` payload as ``RunController._post_history`` builds it."""
    return {"image": _IMAGE, "label": label, "parent": parent, "confidence": confidence, "slot": slot}


def push(window, view, label: str, confidence: float, slot: int = 1, parent: str | None = None) -> None:
    window.bus.post("run/history", history(label, confidence, slot, parent))
    window.bus.drain()


# ----- empty state -------------------------------------------------------------


def test_empty_state_shows_hint_and_hides_the_list(window) -> None:
    view = build_history_view(window)

    assert not view.empty_label.isHidden()
    assert view.grid_area.isHidden()
    assert view._entries == []


def test_first_entry_reveals_the_list_and_hides_the_hint(window) -> None:
    view = build_history_view(window)

    push(window, view, "9mm FC", 92.0)

    assert view.empty_label.isHidden()
    assert not view.grid_area.isHidden()


# ----- rendering -----------------------------------------------------------------


def test_entries_render_newest_first_with_correct_text(window) -> None:
    view = build_history_view(window)

    push(window, view, "9mm", 91.0, slot=1)
    push(window, view, ".223", 82.0, slot=3)
    push(window, view, "45 ACP", 77.0, slot=0)

    labels = [entry.label_label.text() for entry in view._entries]
    assert labels == ["45 ACP", ".223", "9mm"]
    assert view._entries[0].confidence_label.text() == "77%"
    assert view._entries[0].slot_label.text() == "Catch-All"
    assert view._entries[1].slot_label.text() == "Slot 3"
    assert not view._entries[0].thumb_label.pixmap().isNull()


def test_a_parent_classification_prefixes_the_label(window) -> None:
    view = build_history_view(window)

    push(window, view, "FC 12", 88.0, parent="9mm")

    assert view._entries[0].label_label.text() == "9mm · FC 12"


def test_non_dict_payloads_are_ignored(window) -> None:
    view = build_history_view(window)

    window.bus.post("run/history", None)
    window.bus.post("run/history", "not a dict")
    window.bus.drain()

    assert view._entries == []


# ----- cap -------------------------------------------------------------------------


def test_full_grid_overwrites_the_oldest_cell_in_place(window) -> None:
    """The Windows monitor contract (Seth): tiles never move or scroll — a
    new record overwrites the oldest cell, everything else stays put."""
    view = build_history_view(window)
    capacity = FALLBACK_COLS * FALLBACK_ROWS

    for index in range(capacity):
        push(window, view, f"hs{index}", 99.0)
    assert len(view._tiles) == capacity
    tiles_before = list(view._tiles)

    push(window, view, "overwriter", 99.0)

    assert view._tiles == tiles_before  # same widgets, same positions
    assert len(view._entries) == capacity
    assert view._tiles[0].label_label.text() == "overwriter"  # oldest cell reused
    assert view._tiles[1].label_label.text() == "hs1"  # neighbours untouched
    assert view._entries[0] is view._tiles[0]  # ...and it is now the newest


def test_tiles_fill_top_to_bottom_then_next_column(window) -> None:
    view = build_history_view(window)

    for index in range(FALLBACK_ROWS + 2):
        push(window, view, f"hs{index}", 99.0)

    def cell(tile) -> tuple[int, int]:
        # PySide6 stubs type getItemPosition as `object`; it is a 4-tuple.
        position: Any = view._grid.getItemPosition(view._grid.indexOf(tile))
        return position[0], position[1]

    assert cell(view._tiles[0]) == (0, 0)
    assert cell(view._tiles[1]) == (1, 0)
    assert cell(view._tiles[FALLBACK_ROWS]) == (0, 1)


def test_shrinking_the_view_reflows_and_keeps_the_newest(qapp, window) -> None:
    view = build_history_view(window)
    view.resize(4 * (TILE_W + GUTTER) + GUTTER, 4 * (TILE_H + GUTTER) + GUTTER)
    view.show()
    qapp.processEvents()
    capacity = view._capacity
    assert capacity >= 4

    for index in range(capacity):
        push(window, view, f"hs{index}", 99.0)

    view.resize(TILE_W + 2 * GUTTER, 2 * (TILE_H + GUTTER) + GUTTER)
    qapp.processEvents()

    assert view._capacity < capacity
    assert len(view._entries) == view._capacity
    labels = [entry.label_label.text() for entry in view._entries]
    assert labels[0] == f"hs{capacity - 1}"  # newest survived
    assert "hs0" not in labels  # oldest discarded


# ----- the running case number (Seth: WinForms parity) -------------------------


def test_every_record_carries_a_running_case_number(window) -> None:
    view = build_history_view(window)

    push(window, view, "first", 90.0)
    push(window, view, "second", 91.0)
    push(window, view, "third", 92.0)

    # Newest first: the numbers read 3, 2, 1 down the recency list.
    assert [entry.number_label.text() for entry in view._entries] == ["3", "2", "1"]
    # Accent, not green: hue is meaning, and misses get numbered too.
    assert f"color: {window.palette_colors['accent']}" in view._entries[0].number_label.styleSheet()


def test_case_numbers_survive_a_zoom_replay(window) -> None:
    view = build_history_view(window)
    for index in range(3):
        push(window, view, f"hs{index}", 99.0)

    view._apply_zoom(150)

    assert [entry.number_label.text() for entry in view._entries] == ["3", "2", "1"]


# ----- recency highlight ("snake") --------------------------------------------------


def test_latest_entry_highlight_moves(window) -> None:
    view = build_history_view(window)
    colors = window.palette_colors

    push(window, view, "first", 99.0)
    assert colors[SNAKE_ROLES[0]] in view._entries[0].styleSheet()

    push(window, view, "second", 99.0)
    assert colors[SNAKE_ROLES[0]] in view._entries[0].styleSheet()
    assert colors[SNAKE_ROLES[1]] in view._entries[1].styleSheet()
    # The now-second-oldest entry no longer carries the newest's highlight.
    assert colors[SNAKE_ROLES[0]] not in view._entries[1].styleSheet()


def test_entries_past_the_snake_carry_no_highlight(window) -> None:
    view = build_history_view(window)
    colors = window.palette_colors

    for index in range(len(SNAKE_ROLES) + 2):
        push(window, view, f"hs{index}", 99.0)

    plain_border = colors.get("border")
    tail = view._entries[len(SNAKE_ROLES)]
    for role in SNAKE_ROLES:
        assert colors[role] not in tail.styleSheet()
    assert plain_border in tail.styleSheet()


# ----- confidence floor coloring ------------------------------------------------------


def test_below_floor_confidence_colored_warning(window, config) -> None:
    config.set_run_confidence_floor(80)
    view = build_history_view(window)

    push(window, view, "9mm", 92.0)
    push(window, view, "unknown", 41.0)

    above, below = view._entries[1], view._entries[0]  # newest first: 41 pushed last
    assert f"color: {window.palette_colors['warning']}" in below.confidence_label.styleSheet()
    assert f"color: {window.palette_colors['text_muted']}" in above.confidence_label.styleSheet()


def test_a_disabled_floor_never_warns(window, config) -> None:
    config.set_run_confidence_floor(0)
    view = build_history_view(window)

    push(window, view, "unknown", 3.0)

    assert f"color: {window.palette_colors['text_muted']}" in view._entries[0].confidence_label.styleSheet()


# ----- click -> preview --------------------------------------------------------------


def test_clicking_an_entry_opens_the_preview_with_its_record(window) -> None:
    view = build_history_view(window)
    push(window, view, "9mm FC", 92.0, slot=2)
    opened = []
    view.open_preview = lambda record: opened.append(record)

    view._entries[0].clicked.emit(view._entries[0].record)

    assert len(opened) == 1
    assert opened[0]["label"] == "9mm FC"
    assert opened[0]["slot"] == 2


def test_preview_dialog_shows_the_record(qapp, window) -> None:
    view = build_history_view(window)
    push(window, view, "9mm FC", 92.0, slot=2, parent="9mm")

    dialog = HistoryPreviewDialog(view, view._entries[0].record)

    assert dialog.title_label.text() == "9mm · 9mm FC"
    assert dialog.detail_label.text() == "92% · Slot 2"
    assert not dialog.image_label.pixmap().isNull()


# ----- theme -------------------------------------------------------------------------


def test_apply_palette_repaints_after_a_theme_switch(window) -> None:
    view = build_history_view(window)
    push(window, view, "9mm", 99.0)
    before = view._entries[0].styleSheet()

    window.set_theme("Comic Book")
    view.apply_palette()

    after = view._entries[0].styleSheet()
    assert after != before
    assert window.palette_colors[SNAKE_ROLES[0]] in after


# ----- zoom --------------------------------------------------------------------------


def test_zoom_bar_sits_below_the_tile_grid(qapp, window) -> None:
    """Seth/JL, 2026-08-13: the zoom bar belongs at the bottom of the dock,
    not above the tiles."""
    view = build_history_view(window)
    view.resize(400, 400)
    view.show()
    qapp.processEvents()

    assert view.zoom_slider.y() > view.grid_area.y()
    # Pinned to the foot even in the empty state: the surplus height goes to
    # the label/grid, never below the bar (JL: it floated mid-panel).
    assert view.zoom_slider.geometry().bottom() >= view.height() - view.zoom_slider.height()
    assert view.zoom_slider.y() > view.empty_label.y()


def test_zoom_defaults_to_100_percent(window) -> None:
    view = build_history_view(window)

    assert view.zoom_slider.value() == ZOOM_DEFAULT
    assert view.zoom_value_label.text() == "100%"
    assert view._tile_w == TILE_W
    assert view._tile_h == TILE_H
    assert view._thumb == THUMB


def test_zoom_change_scales_tile_footprint_and_thumbnail(window) -> None:
    view = build_history_view(window)
    push(window, view, "9mm", 90.0)

    view.zoom_slider.setValue(200)

    factor = 200 / 100.0
    assert view._tile_w == round(TILE_W * factor)
    assert view._tile_h == round(TILE_H * factor)
    assert view._thumb == round(THUMB * factor)
    entry = view._entries[0]
    assert (entry.width(), entry.height()) == (view._tile_w, view._tile_h)
    pixmap = entry.thumb_label.pixmap()
    assert (pixmap.width(), pixmap.height()) == (view._thumb, view._thumb)


def test_slider_drag_updates_label_live_without_applying(window) -> None:
    """``sliderMoved`` (drag in progress) only touches the label — the expensive
    tile rebuild waits for ``valueChanged`` (drop), per ``setTracking(False)``."""
    view = build_history_view(window)
    push(window, view, "9mm", 90.0)

    view.zoom_slider.sliderMoved.emit(175)

    assert view.zoom_value_label.text() == "175%"
    assert view._tile_w == TILE_W  # unchanged — no rebuild yet


def test_zoom_change_preserves_records_when_capacity_still_fits(window) -> None:
    """Below the fallback grid's capacity, nothing is discarded by a zoom change."""
    view = build_history_view(window)
    push(window, view, "9mm", 91.0, slot=1)
    push(window, view, ".223", 82.0, slot=3)
    push(window, view, "45 ACP", 77.0, slot=0)

    view.zoom_slider.setValue(75)

    labels = [entry.label_label.text() for entry in view._entries]
    assert labels == ["45 ACP", ".223", "9mm"]  # newest-first order kept
    assert view._entries[0].confidence_label.text() == "77%"
    assert view._entries[0].slot_label.text() == "Catch-All"


def test_zoom_in_on_a_fixed_size_widget_shrinks_capacity_and_drops_the_oldest(qapp, window) -> None:
    """Same rule as a plain widget-resize shrink (Seth): newest survive, oldest go."""
    view = build_history_view(window)
    view.resize(4 * (TILE_W + GUTTER) + GUTTER, 4 * (TILE_H + GUTTER) + GUTTER)
    view.show()
    qapp.processEvents()
    capacity_before = view._capacity
    assert capacity_before >= 4

    for index in range(capacity_before):
        push(window, view, f"hs{index}", 99.0)

    view.zoom_slider.setValue(ZOOM_MAX)
    qapp.processEvents()

    assert view._capacity < capacity_before
    assert len(view._entries) == view._capacity
    labels = [entry.label_label.text() for entry in view._entries]
    assert labels[0] == f"hs{capacity_before - 1}"  # newest survived
    assert "hs0" not in labels  # oldest discarded


def test_zoom_choice_persists_across_view_instances(window) -> None:
    view = build_history_view(window)

    view.zoom_slider.setValue(150)

    second = build_history_view(window)
    assert second.zoom_slider.value() == 150
    assert second._tile_w == view._tile_w

    from sorter.data.repository import SettingsRepo

    assert SettingsRepo(window.db).get(SETTING_HISTORY_ZOOM) == "150"


def test_legacy_percent_sign_value_migrates_on_load(window) -> None:
    """A pre-slider install stored ``"150%"`` via the old combo; the sign is
    stripped so the choice still applies rather than falling back to 100."""
    from sorter.data.repository import SettingsRepo

    SettingsRepo(window.db).set(SETTING_HISTORY_ZOOM, "150%")

    view = build_history_view(window)

    assert view.zoom_slider.value() == 150
    assert view.zoom_value_label.text() == "150%"
    assert view._tile_w == round(TILE_W * 1.5)


def test_apply_palette_still_recolors_after_a_zoom_change(window) -> None:
    view = build_history_view(window)
    push(window, view, "9mm", 99.0)
    view.zoom_slider.setValue(150)

    window.set_theme("Comic Book")
    view.apply_palette()

    assert window.palette_colors[SNAKE_ROLES[0]] in view._entries[0].styleSheet()


# ----- lifecycle -----------------------------------------------------------------------


def test_unsubscribe_stops_further_pushes(window) -> None:
    view = build_history_view(window)
    push(window, view, "9mm", 99.0)

    view.unsubscribe()
    window.bus.post("run/history", history("after-unsubscribe", 99.0))
    window.bus.drain()

    assert len(view._entries) == 1
    assert view._entries[0].label_label.text() == "9mm"


# ----- integration: a real manual-feed cycle lands in the widget ---------------------


def test_manual_feed_cycle_lands_in_the_history_view(window, config, monkeypatch) -> None:
    config.add_headstamp("9mm FC", 2)
    monkeypatch.setattr(classifier, "classify_active", lambda *a, **k: ("9mm FC", 97.0))
    # The controller captures the camera it is built with, so stub before connecting.
    window.camera = types.SimpleNamespace(
        capture_frame=lambda: np.zeros((480, 640, 3), np.uint8),
        latest_frame=lambda: None,
        stop=lambda: None,
    )
    view = build_history_view(window)
    window.connect_serial(EMULATED_PORT)

    window.action_buttons["Manual feed"].click()

    assert drain_until(window, lambda: len(view._entries) == 1)
    assert view._entries[0].label_label.text() == "9mm FC"
    assert view._entries[0].slot_label.text() == "Slot 2"
