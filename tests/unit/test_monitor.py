"""Monitor window ring-buffer + recency-colour logic.

Needs a real Tk display (uses xvfb in CI); skipped where tkinter/cv2 aren't
importable, matching the other UI tests.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

import tkinter as tk

from sorter.events import EventBus
from sorter.ui.monitor import SNAKE_COLORS, MonitorWindow, _Tile


def _rec(label: str, slot: int, *, image=None) -> dict:
    # The ring-buffer/recency tests don't need a rendered thumbnail; keeping
    # image=None avoids creating an ImageTk.PhotoImage whose teardown is
    # fragile when many independent Tk roots churn through one process.
    return {
        "image": image,
        "label": label,
        "parent": None,
        "confidence": 90,
        "slot": slot,
    }


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    r.withdraw()
    yield r
    r.destroy()


def _fixed_capacity(win: MonitorWindow, *, rows: int, cols: int) -> None:
    win._rows = rows
    win._cols = cols
    win._capacity = rows * cols


def test_grows_then_wraps_as_ring_buffer(root) -> None:
    bus = EventBus()
    win = MonitorWindow(root, bus=bus, config=None)
    _fixed_capacity(win, rows=2, cols=2)  # capacity 4

    for i in range(4):
        win._on_history(_rec(f"H{i}", i))
    assert len(win._tiles) == 4  # filled, no extra tiles created
    first_write = win._write_index

    # Fifth record reuses an existing tile (ring buffer), count stays at 4.
    win._on_history(_rec("H4", 4))
    assert len(win._tiles) == 4
    assert win._write_index == (first_write + 1) % 4


def test_recency_colours_head_is_black(root) -> None:
    bus = EventBus()
    win = MonitorWindow(root, bus=bus, config=None)
    _fixed_capacity(win, rows=2, cols=2)
    for i in range(3):
        win._on_history(_rec(f"H{i}", i))
    # Most-recent tile carries the black head colour; the one before it the
    # first trailing colour.
    head = win._recency[0]
    assert str(head.cget("highlightbackground")).lower() in ("#000000", "black")
    assert str(win._recency[1].cget("highlightbackground")).lower() == SNAKE_COLORS[1]


def test_shrink_keeps_newest_records(root) -> None:
    bus = EventBus()
    win = MonitorWindow(root, bus=bus, config=None)
    _fixed_capacity(win, rows=2, cols=3)  # capacity 6
    for i in range(6):
        win._on_history(_rec(f"H{i}", i))
    newest = win._recency[0]
    # Shrink to capacity 2 and rebuild.
    _fixed_capacity(win, rows=1, cols=2)
    win._rebuild_layout()
    assert len(win._tiles) == 2
    assert newest in win._tiles  # newest survived the shrink
    assert len(win._recency) == 2


def test_tile_renders_bgr_image_and_text(root) -> None:
    # Exercises the BGR→thumbnail path on a single tile, cleaning the
    # PhotoImage up explicitly so it can't outlive the interpreter.
    tile = _Tile(root)
    try:
        img = np.full((48, 48, 3), 100, dtype=np.uint8)
        tile.set_record({"image": img, "label": "WIN", "parent": "Brass", "confidence": 91, "slot": 3})
        assert tile._photo is not None
        assert tile._label_var.get() == "Brass · WIN"
        assert tile._conf_var.get() == "91%"
        assert tile._slot_var.get() == "Slot 3"
    finally:
        tile._photo = None
        tile.destroy()


def test_unsubscribes_on_close(root) -> None:
    bus = EventBus()
    win = MonitorWindow(root, bus=bus, config=None)
    assert win._on_history in bus._subs.get("run/history", [])
    win._on_close()
    assert win._on_history not in bus._subs.get("run/history", [])
