"""Model image manager.

A paged, filterable thumbnail browser for a model's training images with
per-image preview, bulk reclassify/delete, and a right-click context menu.
Thumbnails are decoded on a worker thread and handed back to the UI thread via
the event bus (PhotoImage must be created on the main thread).

Launched from the Models tab's per-row "Images" button.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .. import paths
from ..data import image_store
from ..data.db import Database
from ..data.models import Model
from ..data.repository import HeadstampRepo
from . import sysutil
from .dialog_image_preview import ImagePreviewDialog
from .theme import PALETTE
from .widgets import ScrollableFrame

_TILE_W = 140
_GUTTER = 8
_THUMB = 120
_PAGE_SIZES = (20, 50, 100, 150, 200)


def _decode_thumb(path: str, size: int = _THUMB):
    """Decode + letterbox a thumbnail (worker thread). Returns a PIL image."""
    try:
        from PIL import Image

        img = Image.open(path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        bg = Image.new("RGB", (size, size), (10, 17, 34))  # PALETTE bg_input
        bg.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
        return bg
    except Exception:
        return None


class _ThumbTile(tk.Frame):
    def __init__(self, parent, *, path, headstamp, on_click, on_double, on_context):
        super().__init__(
            parent, bg=PALETTE["bg_card"], bd=0, highlightthickness=2, highlightbackground=PALETTE["bg_card"]
        )
        self.path = path
        self.headstamp = headstamp
        self.selected = False
        self._photo = None

        holder = tk.Frame(self, width=_THUMB, height=_THUMB, bg=PALETTE["bg_input"])
        holder.pack(padx=4, pady=(4, 2))
        holder.pack_propagate(False)
        self._img = tk.Label(holder, bg=PALETTE["bg_input"])
        self._img.pack(fill=tk.BOTH, expand=True)

        caption = headstamp if len(headstamp) <= 16 else headstamp[:15] + "…"
        # `font=(None, 8)` isn't a valid family per the Tk font spec — Tcl
        # took it literally as a family named "None" rather than "use the
        # default family". "TkDefaultFont" is the named default font that
        # actually resolves.
        self._cap = tk.Label(self, text=caption, bg=PALETTE["bg_card"], fg=PALETTE["text"], font=("TkDefaultFont", 8))
        self._cap.pack(pady=(0, 4))

        for w in (self, holder, self._img, self._cap):
            w.bind("<Button-1>", lambda _e: on_click(self))
            w.bind("<Double-Button-1>", lambda _e: on_double(self))
            w.bind("<Button-3>", lambda e: on_context(self, e))

    def set_image(self, photo) -> None:
        self._photo = photo  # keep a reference so Tk doesn't GC it
        self._img.configure(image=photo)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        color = PALETTE["accent"] if selected else PALETTE["bg_card"]
        self.configure(highlightbackground=color, highlightcolor=color)


class ModelImagesDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, db: Database, model: Model, *, app: Any) -> None:
        super().__init__(parent)
        self.db = db
        self.model = model
        self.app = app
        self.images_dir = paths.model_images_dir(model.id)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._headstamp_names = [h.name for h in HeadstampRepo(db).list_for_model(model.id)]

        self.title(f"Images — {model.name}")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.minsize(820, 560)
        self.configure(bg=PALETTE["bg_window"])

        self._filtered: list[Path] = []
        self._tiles: list[_ThumbTile] = []
        self._cols = 0
        self._page_index = 0
        self._page_size = 50
        self._load_token = 0
        self._context_tile: _ThumbTile | None = None
        self._thumb_topic = f"images/thumb/{id(self)}"

        self._build_ui()
        self.app.bus.subscribe(self._thumb_topic, self._on_thumb)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())
        self._reload()

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self, style="Window.TFrame", padding=(10, 10, 10, 4))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Show", style="Muted.TLabel").pack(side=tk.LEFT)
        self._filter_var = tk.StringVar()
        self._filter_combo = ttk.Combobox(top, state="readonly", width=22, textvariable=self._filter_var)
        self._filter_combo.pack(side=tk.LEFT, padx=(6, 12))
        self._filter_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload(reset_page=True))
        self._populate_filter_combo()

        ttk.Label(top, text="Per page", style="Muted.TLabel").pack(side=tk.LEFT)
        self._pagesize_var = tk.StringVar(value=str(self._page_size))
        pagesize = ttk.Combobox(
            top, state="readonly", width=6, textvariable=self._pagesize_var, values=[str(n) for n in _PAGE_SIZES]
        )
        pagesize.pack(side=tk.LEFT, padx=(6, 12))
        pagesize.bind("<<ComboboxSelected>>", lambda _e: self._on_pagesize())

        ttk.Button(top, text="Open folder", command=self._open_folder).pack(side=tk.LEFT)
        self._counts_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self._counts_var, style="Subtle.TLabel").pack(side=tk.RIGHT)

        # ---- thumbnail grid (scrollable) ------------------------------------
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self._scroll = ScrollableFrame(body)
        self._scroll.pack(fill=tk.BOTH, expand=True)
        self._grid = self._scroll.body
        self._grid.bind("<Configure>", lambda _e: self._reflow(), add="+")
        self._empty = ttk.Label(
            self._grid,
            text="No images.",
            style="Subtle.TLabel",
        )

        # ---- pager ----------------------------------------------------------
        pager = ttk.Frame(self, style="Window.TFrame", padding=(10, 0))
        pager.pack(fill=tk.X)
        self._first_btn = ttk.Button(pager, text="⏮ First", width=8, command=lambda: self._go(0))
        self._prev_btn = ttk.Button(pager, text="◀ Prev", width=8, command=lambda: self._go(self._page_index - 1))
        self._next_btn = ttk.Button(pager, text="Next ▶", width=8, command=lambda: self._go(self._page_index + 1))
        self._last_btn = ttk.Button(pager, text="Last ⏭", width=8, command=lambda: self._go(self._page_count() - 1))
        self._first_btn.pack(side=tk.LEFT)
        self._prev_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._page_var = tk.StringVar(value="")
        ttk.Label(pager, textvariable=self._page_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=12)
        self._last_btn.pack(side=tk.LEFT)
        self._next_btn.pack(side=tk.LEFT, padx=(0, 6))

        # ---- bulk actions ---------------------------------------------------
        actions = ttk.Frame(self, style="Window.TFrame", padding=(10, 6, 10, 10))
        actions.pack(fill=tk.X)
        self._sel_var = tk.StringVar(value="0 selected")
        ttk.Label(actions, textvariable=self._sel_var, style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(actions, text="Close", command=self._close).pack(side=tk.RIGHT)
        self._delete_btn = ttk.Button(
            actions, text="Delete selected", command=self._delete_selected, style="Danger.TButton"
        )
        self._delete_btn.pack(side=tk.RIGHT, padx=(8, 12))
        self._reclassify_btn = ttk.Button(actions, text="Reclassify selected", command=self._reclassify_selected)
        self._reclassify_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self._target_var = tk.StringVar()
        self._target_combo = ttk.Combobox(
            actions, state="readonly", width=20, textvariable=self._target_var, values=self._headstamp_names
        )
        if self._headstamp_names:
            self._target_combo.current(0)
        self._target_combo.pack(side=tk.RIGHT)
        ttk.Label(actions, text="to", style="Muted.TLabel").pack(side=tk.RIGHT, padx=6)

    def _populate_filter_combo(self) -> None:
        values = [image_store.ALL_FILTER] + self._headstamp_names + [image_store.UNKNOWN_FILTER]
        self._filter_combo["values"] = values
        self._filter_var.set(image_store.ALL_FILTER)

    # ----- data + paging ------------------------------------------------------

    def _scan_filter(self) -> None:
        files = image_store.list_images(self.images_dir)
        self._filtered = image_store.filter_images(
            files,
            self._filter_var.get(),
            self._headstamp_names,
        )
        max_page = max(0, self._page_count() - 1)
        self._page_index = min(self._page_index, max_page)

    def _page_count(self) -> int:
        if not self._filtered:
            return 0
        return (len(self._filtered) + self._page_size - 1) // self._page_size

    def _page_files(self) -> list[Path]:
        start = self._page_index * self._page_size
        return self._filtered[start : start + self._page_size]

    def _on_pagesize(self) -> None:
        try:
            self._page_size = int(self._pagesize_var.get())
        except ValueError:
            self._page_size = 50
        self._reload(reset_page=True)

    def _go(self, page_index: int) -> None:
        page_index = max(0, min(page_index, max(0, self._page_count() - 1)))
        if page_index != self._page_index:
            self._page_index = page_index
            self._reload()

    def _reload(self, *, reset_page: bool = False) -> None:
        if reset_page:
            self._page_index = 0
        self._load_token += 1
        token = self._load_token
        self._scan_filter()
        self._build_tiles()
        self._update_pager()
        self._update_selection()
        page = [str(p) for p in self._page_files()]
        if page:
            self.app.run_worker(lambda: self._decode_page(token, page))

    def _build_tiles(self) -> None:
        for tile in self._tiles:
            tile.destroy()
        self._tiles = []
        self._cols = 0
        page = self._page_files()
        if not page:
            self._empty.grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return
        self._empty.grid_forget()
        for path in page:
            tile = _ThumbTile(
                self._grid,
                path=path,
                headstamp=image_store.parse_headstamp(path),
                on_click=self._toggle_tile,
                on_double=self._open_preview,
                on_context=self._show_context,
            )
            self._tiles.append(tile)
        self._reflow(force=True)

    def _reflow(self, *, force: bool = False) -> None:
        if not self._tiles:
            return
        width = max(1, self._grid.winfo_width())
        cols = max(1, width // (_TILE_W + _GUTTER))
        if not force and cols == self._cols:
            return
        self._cols = cols
        for i, tile in enumerate(self._tiles):
            row, col = divmod(i, cols)
            tile.grid(row=row, column=col, padx=_GUTTER // 2, pady=_GUTTER // 2, sticky="nw")

    # ----- thumbnail loading (worker thread) ----------------------------------

    def _decode_page(self, token: int, page: list[str]) -> None:
        for idx, path in enumerate(page):
            if token != self._load_token:
                return  # page/filter changed — abandon stale work
            pil = _decode_thumb(path)
            if pil is not None:
                self.app.bus.post(self._thumb_topic, (token, idx, pil))

    def _on_thumb(self, payload: tuple[int, int, Any]) -> None:
        token, idx, pil = payload
        if token != self._load_token or idx >= len(self._tiles):
            return
        try:
            from PIL import ImageTk

            self._tiles[idx].set_image(ImageTk.PhotoImage(pil))
        except Exception:
            pass

    # ----- selection ----------------------------------------------------------

    def _toggle_tile(self, tile: _ThumbTile) -> None:
        tile.set_selected(not tile.selected)
        self._update_selection()

    def _selected(self) -> list[_ThumbTile]:
        return [t for t in self._tiles if t.selected]

    def _update_selection(self) -> None:
        count = len(self._selected())
        self._sel_var.set(f"{count} selected")
        state = tk.NORMAL if count else tk.DISABLED
        self._delete_btn.configure(state=state)
        self._reclassify_btn.configure(state=tk.NORMAL if (count and self._headstamp_names) else tk.DISABLED)

    def _update_pager(self) -> None:
        total = len(self._filtered)
        count = self._page_count()
        if total == 0:
            self._page_var.set("Page 0 of 0")
        else:
            start = self._page_index * self._page_size + 1
            end = min(total, (self._page_index + 1) * self._page_size)
            self._page_var.set(f"Page {self._page_index + 1} of {count}   ({start}–{end} of {total})")
        self._counts_var.set(f"{total} image" + ("" if total == 1 else "s"))
        at_first = self._page_index <= 0
        at_last = self._page_index >= count - 1 or count == 0
        self._first_btn.configure(state=tk.DISABLED if at_first else tk.NORMAL)
        self._prev_btn.configure(state=tk.DISABLED if at_first else tk.NORMAL)
        self._next_btn.configure(state=tk.DISABLED if at_last else tk.NORMAL)
        self._last_btn.configure(state=tk.DISABLED if at_last else tk.NORMAL)

    # ----- mutations ----------------------------------------------------------

    def _reclassify_selected(self) -> None:
        target = self._target_var.get()
        selected = self._selected()
        if not target or not selected:
            return
        for tile in selected:
            image_store.reclassify(tile.path, target)
        self._reload()

    def _delete_selected(self) -> None:
        selected = self._selected()
        if not selected:
            return
        if not messagebox.askyesno(
            "Delete images",
            f"Delete {len(selected)} image(s)? This cannot be undone.",
            parent=self,
        ):
            return
        for tile in selected:
            image_store.delete(tile.path)
        self._reload()

    def _open_preview(self, tile: _ThumbTile) -> None:
        ImagePreviewDialog(
            self,
            tile.path,
            self._headstamp_names,
            tile.headstamp,
            on_changed=lambda _change: self._reload(),
        )

    def _show_context(self, tile: _ThumbTile, event: tk.Event) -> None:
        self._context_tile = tile
        menu = tk.Menu(
            self,
            tearoff=0,
            bg=PALETTE["bg_card"],
            fg=PALETTE["text"],
            activebackground=PALETTE["accent"],
            activeforeground=PALETTE["text_inverse"],
        )
        menu.add_command(label="Preview", command=lambda: self._open_preview(tile))
        sub = tk.Menu(
            menu,
            tearoff=0,
            bg=PALETTE["bg_card"],
            fg=PALETTE["text"],
            activebackground=PALETTE["accent"],
            activeforeground=PALETTE["text_inverse"],
        )
        for name in self._headstamp_names:
            sub.add_command(label=name, command=lambda n=name: self._reclassify_one(tile, n))
        menu.add_cascade(label="Reclassify to", menu=sub, state=tk.NORMAL if self._headstamp_names else tk.DISABLED)
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self._delete_one(tile))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _reclassify_one(self, tile: _ThumbTile, name: str) -> None:
        image_store.reclassify(tile.path, name)
        self._reload()

    def _delete_one(self, tile: _ThumbTile) -> None:
        if not messagebox.askyesno(
            "Delete image",
            f"Delete this image?\n\n{Path(tile.path).name}",
            parent=self,
        ):
            return
        image_store.delete(tile.path)
        self._reload()

    def _open_folder(self) -> None:
        if not sysutil.open_path(self.images_dir):
            messagebox.showinfo("Open folder", str(self.images_dir), parent=self)

    # ----- lifecycle ----------------------------------------------------------

    def _close(self) -> None:
        self._load_token += 1  # abandon any in-flight decode
        try:
            self.app.bus.unsubscribe(self._thumb_topic, self._on_thumb)
        except Exception:
            pass
        self.destroy()
