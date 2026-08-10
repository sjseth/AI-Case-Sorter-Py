"""Reusable Tk widgets for the app."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageTk

from .theme import PALETTE


class NumericField(ttk.Frame):
    """Label + Spinbox bound to an IntVar/DoubleVar."""

    def __init__(
        self,
        parent: tk.Misc,
        label: str,
        *,
        from_: float = 0,
        to: float = 9999,
        increment: float = 1,
        initial: float = 0,
        is_float: bool = False,
        width: int = 8,
    ) -> None:
        super().__init__(parent)
        ttk.Label(self, text=label).pack(side=tk.LEFT, padx=(0, 8))
        self.var: tk.Variable = tk.DoubleVar(value=initial) if is_float else tk.IntVar(value=int(initial))
        self.spin = ttk.Spinbox(
            self,
            from_=from_,
            to=to,
            increment=increment,
            textvariable=self.var,
            width=width,
        )
        self.spin.pack(side=tk.LEFT)

    def get(self) -> Any:
        return self.var.get()

    def set(self, value: Any) -> None:
        self.var.set(value)


class ScrollableFrame(ttk.Frame):
    """ttk.Frame whose content is a vertically scrollable inner frame.

    Add the actual tab content to ``self.body``. Pass ``viewport=(w, h)``
    to fix how much of it is visible at once; without it the frame is as big
    as its content and scrolling only kicks in when the window is too small. The body is sized to match
    the canvas width (so ``fill=X`` children get the right size) and grows
    to at least the canvas height (so ``expand=True`` children still fill
    available space). When the content's natural height exceeds the
    visible area, the vertical scrollbar engages — letting users on small
    displays reach controls that would otherwise be clipped.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        viewport: tuple[int, int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._canvas = tk.Canvas(
            self,
            highlightthickness=0,
            bg=PALETTE["bg_surface"],
            borderwidth=0,
        )
        if viewport is not None:
            # Size the *canvas*, not the frame: sizing the frame and turning
            # off propagation would let the canvas (packed to expand) eat the
            # scrollbar's width.
            self._canvas.configure(width=viewport[0], height=viewport[1])
        self._scrollbar = ttk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self._canvas.yview,
        )
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.body = ttk.Frame(self._canvas)
        self._window_id = self._canvas.create_window(
            (0, 0),
            window=self.body,
            anchor=tk.NW,
        )

        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        # Pointer-driven wheel binding: only the canvas currently under the
        # mouse claims the wheel — keeps wheel scrolling intuitive when
        # other scrollable widgets (e.g. the slot-details panel) sit inside.
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        body_h = self.body.winfo_reqheight()
        self._canvas.itemconfigure(
            self._window_id,
            width=event.width,
            height=max(event.height, body_h),
        )
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_body_configure(self, _event: tk.Event) -> None:
        canvas_h = self._canvas.winfo_height()
        body_h = self.body.winfo_reqheight()
        self._canvas.itemconfigure(
            self._window_id,
            height=max(canvas_h, body_h),
        )
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _bind_mousewheel(self, _event: tk.Event) -> None:
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind_all("<Button-4>", self._on_mousewheel)
        self._canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event) -> None:
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        # If everything fits, leave the wheel alone so it can drive other
        # widgets (combobox dropdowns, the slot-details inner scroll, etc.).
        first, last = self._canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        delta = getattr(event, "delta", 0)
        if event.num == 4 or delta > 0:
            self._canvas.yview_scroll(-3, "units")
        elif event.num == 5 or delta < 0:
            self._canvas.yview_scroll(3, "units")


class ImagePanel(ttk.Frame):
    """Canvas-backed BGR-image panel that scales the input frame to fit."""

    def __init__(self, parent: tk.Misc, *, width: int = 320, height: int = 240) -> None:
        super().__init__(parent)
        self._target_w = width
        self._target_h = height
        # Wrap the canvas in a thin border using the surface color so the
        # image panel reads as a "card" rather than a void.
        self.canvas = tk.Canvas(
            self,
            width=width,
            height=height,
            bg=PALETTE["bg_input"],
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            borderwidth=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._tk_image: ImageTk.PhotoImage | None = None
        self._last_frame: np.ndarray | None = None

    def set_size(self, width: int, height: int) -> None:
        """Resize the canvas to ``width`` x ``height`` and re-render."""
        if width == self._target_w and height == self._target_h:
            return
        self._target_w = max(1, width)
        self._target_h = max(1, height)
        self.canvas.config(width=self._target_w, height=self._target_h)
        if self._last_frame is not None:
            self.show_bgr(self._last_frame)

    def show_bgr(self, frame_bgr: np.ndarray | None) -> None:
        self._last_frame = frame_bgr
        if frame_bgr is None:
            self.canvas.delete("all")
            self._tk_image = None
            return
        h, w = frame_bgr.shape[:2]
        scale = min(self._target_w / max(1, w), self._target_h / max(1, h))
        if scale <= 0:
            return
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        self._tk_image = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(self._target_w // 2, self._target_h // 2, image=self._tk_image)


def build_labeled_entry(
    parent: tk.Misc,
    label: str,
    *,
    show: str | None = None,
    width: int = 40,
) -> tuple[ttk.Frame, tk.StringVar]:
    frame = ttk.Frame(parent)
    ttk.Label(frame, text=label, width=18, anchor=tk.W).pack(side=tk.LEFT)
    var = tk.StringVar()
    kwargs: dict[str, Any] = {"textvariable": var, "width": width}
    if show is not None:
        kwargs["show"] = show
    entry = ttk.Entry(frame, **kwargs)
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    return frame, var


def build_button_row(
    parent: tk.Misc,
    buttons: list[tuple[str, Callable[[], None]]],
    *,
    primary: str | None = None,
) -> ttk.Frame:
    """Build a row of buttons. Pass ``primary=<label>`` to render that one
    in the accent style for visual emphasis."""
    row = ttk.Frame(parent)
    for label, command in buttons:
        style = "Accent.TButton" if label == primary else "TButton"
        ttk.Button(row, text=label, command=command, style=style).pack(side=tk.LEFT, padx=(0, 8))
    return row
