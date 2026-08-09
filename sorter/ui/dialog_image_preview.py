"""Shared single-image preview popup with reclassify + delete actions.

Used by both the model image manager and the evaluator's results table. The
dialog performs the file operation itself (via :mod:`sorter.image_store`) and
notifies the caller through ``on_changed`` so each caller refreshes its own
view.

``on_changed`` payloads:
    {"action": "reclassified", "old_path", "new_path", "new_headstamp"}
    {"action": "deleted", "old_path"}
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .. import image_store
from .theme import PALETTE
from .widgets import ImagePanel


class ImagePreviewDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        image_path: Path | str,
        headstamps: list[str],
        current_headstamp: str | None = None,
        *,
        on_changed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = Path(image_path)
        self._current = current_headstamp or image_store.parse_headstamp(self._path)
        self._headstamps = headstamps
        self._on_changed = on_changed

        self.title("Image preview")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.configure(bg=PALETTE["bg_window"])

        wrap = ttk.Frame(self, padding=12, style="Window.TFrame")
        wrap.pack(fill=tk.BOTH, expand=True)

        self._panel = ImagePanel(wrap, width=480, height=480)
        self._panel.pack()
        try:
            import cv2

            self._panel.show_bgr(cv2.imread(str(self._path)))
        except Exception:
            self._panel.show_bgr(None)

        ttk.Label(
            wrap,
            text=f"Classification: {self._current}    File: {self._path.name}",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(8, 6))

        row = ttk.Frame(wrap, style="Window.TFrame")
        row.pack(fill=tk.X)
        ttk.Label(row, text="Reclassify to", style="Muted.TLabel").pack(side=tk.LEFT)
        self._combo = ttk.Combobox(row, state="readonly", width=26, values=headstamps)
        if self._current in headstamps:
            self._combo.set(self._current)
        elif headstamps:
            self._combo.current(0)
        self._combo.pack(side=tk.LEFT, padx=(6, 6))
        ttk.Button(row, text="Reclassify", command=self._reclassify, style="Accent.TButton").pack(side=tk.LEFT)
        ttk.Button(row, text="Delete", command=self._delete, style="Danger.TButton").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        self.bind("<Escape>", lambda _e: self.destroy())

    def _reclassify(self) -> None:
        target = self._combo.get()
        if not target or target == self._current:
            self.destroy()
            return
        new_path = image_store.reclassify(self._path, target)
        if new_path is None:
            messagebox.showwarning(
                "Reclassify failed",
                "Could not reclassify — a file for that headstamp may already exist.",
                parent=self,
            )
            return
        if self._on_changed:
            self._on_changed(
                {
                    "action": "reclassified",
                    "old_path": str(self._path),
                    "new_path": str(new_path),
                    "new_headstamp": target,
                }
            )
        self.destroy()

    def _delete(self) -> None:
        if not messagebox.askyesno(
            "Delete image",
            f"Delete this image?\n\n{self._path.name}",
            parent=self,
        ):
            return
        if not image_store.delete(self._path):
            messagebox.showerror("Delete failed", "Could not delete the file.", parent=self)
            return
        if self._on_changed:
            self._on_changed({"action": "deleted", "old_path": str(self._path)})
        self.destroy()
