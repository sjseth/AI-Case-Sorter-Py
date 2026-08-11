"""Monitor window — live in-memory image history.

A separate Toplevel opened from the Run tab. It keeps only as many recent
classifications as fit in the window, laid out top-to-bottom then left-to-right
in a fixed-position ring buffer: once the grid is full the write position wraps
back to the top-left and overwrites the oldest tile.

A short "snake" of border colours trails the most recent classification: the
current one has a black border, and the few before it fade from dark blue out
to white; everything older has no border. Resizing recomputes how many tiles
fit and rebuilds the grid, preserving the newest records.

Ports the legacy app's image-history tray.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

import cv2
from PIL import Image, ImageTk

from ..control.events import EventBus
from .theme import PALETTE

# Tile geometry (px). The image is square; text sits to its right.
THUMB = 76
TILE_W = 210
TILE_H = 92
GUTTER = 6
BORDER = 2

# Most-recent → oldest border colours. Index 0 (the current classification)
# is black; the trailing few fade dark-blue → white. Matches the legacy
# snake-colors gradient with a black head per the Monitor spec.
SNAKE_COLORS = (
    "#000000",  # current classification
    "#00334d",  # dark blue
    "#0a5c8a",
    "#3399cc",
    "#99d6f0",
    "#ffffff",  # white
)


class _Tile(tk.Frame):
    """One history cell: square thumbnail + classification / confidence / slot."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            bg=PALETTE["bg_surface"],
            highlightthickness=BORDER,
            highlightbackground=PALETTE["bg_surface"],
            highlightcolor=PALETTE["bg_surface"],
            width=TILE_W,
            height=TILE_H,
        )
        self.pack_propagate(False)
        self._photo: ImageTk.PhotoImage | None = None

        self._img = tk.Label(self, bg=PALETTE["bg_surface"])
        self._img.pack(side=tk.LEFT, padx=4, pady=4)

        text = tk.Frame(self, bg=PALETTE["bg_surface"])
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2, 4))
        self._label_var = tk.StringVar(value="")
        self._conf_var = tk.StringVar(value="")
        self._slot_var = tk.StringVar(value="")
        tk.Label(
            text,
            textvariable=self._label_var,
            bg=PALETTE["bg_surface"],
            fg=PALETTE["text"],
            font=("TkDefaultFont", 9, "bold"),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=TILE_W - THUMB - 24,
        ).pack(anchor=tk.W, pady=(6, 0))
        tk.Label(
            text,
            textvariable=self._conf_var,
            bg=PALETTE["bg_surface"],
            fg=PALETTE["text_muted"],
            font=("TkDefaultFont", 8),
            anchor=tk.W,
        ).pack(anchor=tk.W)
        tk.Label(
            text,
            textvariable=self._slot_var,
            bg=PALETTE["bg_surface"],
            fg=PALETTE["text_muted"],
            font=("TkDefaultFont", 8),
            anchor=tk.W,
        ).pack(anchor=tk.W)

    def set_record(self, rec: dict[str, Any]) -> None:
        image = rec.get("image")
        if image is not None:
            try:
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(rgb)
                pil.thumbnail((THUMB, THUMB))
                self._photo = ImageTk.PhotoImage(pil)
                self._img.config(image=self._photo)
            except Exception:
                self._photo = None
                self._img.config(image="")
        label = rec.get("label") or ""
        parent = rec.get("parent")
        self._label_var.set(f"{parent} · {label}" if parent else label)
        try:
            self._conf_var.set(f"{float(rec.get('confidence', 0)):.0f}%")
        except (TypeError, ValueError):
            self._conf_var.set("")
        slot = rec.get("slot", 0)
        self._slot_var.set("Catch-All" if slot == 0 else f"Slot {slot}")

    def set_border(self, color: str) -> None:
        self.config(highlightbackground=color, highlightcolor=color)


class MonitorWindow(tk.Toplevel):
    def __init__(self, parent: tk.Misc, *, bus: EventBus, config: Any) -> None:
        super().__init__(parent)
        self.title("Monitor — Image History")
        self.geometry("720x520")
        self.configure(bg=PALETTE["bg_window"])
        self.bus = bus
        self.config_obj = config

        self._grid = tk.Frame(self, bg=PALETTE["bg_window"])
        self._grid.pack(fill=tk.BOTH, expand=True, padx=GUTTER, pady=GUTTER)

        self._tiles: list[_Tile] = []  # fixed-position ring buffer
        self._recency: list[_Tile] = []  # newest-first, drives border colour
        self._write_index = 0
        self._capacity = 0
        self._cols = 1
        self._rows = 1

        self.bus.subscribe("run/history", self._on_history)
        self.bind("<Configure>", self._on_configure)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._recompute_capacity)

    # ----- layout -------------------------------------------------------------

    def _on_configure(self, event: tk.Event) -> None:
        # Only react to the Toplevel's own resize, not child <Configure> noise.
        if event.widget is self:
            self._recompute_capacity()

    def _recompute_capacity(self) -> None:
        width = max(1, self._grid.winfo_width())
        height = max(1, self._grid.winfo_height())
        cols = max(1, width // (TILE_W + GUTTER))
        rows = max(1, height // (TILE_H + GUTTER))
        capacity = max(1, cols * rows)
        if capacity == self._capacity and cols == self._cols and rows == self._rows:
            return
        self._cols = cols
        self._rows = rows
        self._capacity = capacity
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        """Re-place tiles for the current capacity, keeping the newest records.

        When the window shrinks, the oldest tiles (by recency) are discarded.
        Surviving records are re-packed into the first positions, newest last,
        and the write cursor resets to the top-left.
        """
        if len(self._tiles) > self._capacity:
            keep = set(self._recency[: self._capacity])
            for tile in self._tiles:
                if tile not in keep:
                    tile.destroy()
            # oldest → newest so the next overwrite hits the oldest position.
            survivors = [t for t in reversed(self._recency) if t in keep]
            self._tiles = survivors
            self._recency = [t for t in self._recency if t in keep]
            self._write_index = 0

        for i, tile in enumerate(self._tiles):
            row, col = self._position(i)
            tile.grid(row=row, column=col, padx=GUTTER // 2, pady=GUTTER // 2, sticky=tk.NW)
        self._recolor()

    def _position(self, index: int) -> tuple[int, int]:
        """Top-to-bottom, then left-to-right."""
        rows = max(1, self._rows)
        return index % rows, index // rows

    # ----- record push --------------------------------------------------------

    def _on_history(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        if self._capacity <= 0:
            self._recompute_capacity()

        if len(self._tiles) < self._capacity:
            target = _Tile(self._grid)
            self._tiles.append(target)
            row, col = self._position(len(self._tiles) - 1)
            target.grid(row=row, column=col, padx=GUTTER // 2, pady=GUTTER // 2, sticky=tk.NW)
        else:
            if self._write_index >= len(self._tiles):
                self._write_index = 0
            target = self._tiles[self._write_index]
            self._write_index = (self._write_index + 1) % len(self._tiles)

        target.set_record(payload)
        if target in self._recency:
            self._recency.remove(target)
        self._recency.insert(0, target)
        self._recolor()

    def _recolor(self) -> None:
        for i, tile in enumerate(self._recency):
            color = SNAKE_COLORS[i] if i < len(SNAKE_COLORS) else PALETTE["bg_surface"]
            try:
                tile.set_border(color)
            except tk.TclError:
                pass

    # ----- lifecycle ----------------------------------------------------------

    def _on_close(self) -> None:
        try:
            self.bus.unsubscribe("run/history", self._on_history)
        except Exception:
            pass
        self.destroy()
