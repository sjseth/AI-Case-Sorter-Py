"""Models tab — card-style list for cartridge / model / headstamp management.

Reuses the SlotCard/FlowGrid styling patterns from `tab_run.py` so the tab
fits the dark-blue theme. Each model is one full-width `ModelRowCard` with
per-row action buttons (Activate, Edit, Headstamps, Export, Delete).

A synthetic "Use AI Config" sentinel card always sits at the top of the
list. Activating it switches the app into AI Config mode (no
`default_model_id` setting → AI Config tab visible, classify routes to HTTP).
Activating any other card switches to local-model mode and hides the
AI Config tab.

The `mode/changed` bus topic is posted on every Activate so `app.py` can
re-evaluate AI Config tab visibility.
"""

from __future__ import annotations

import re
import shutil
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from .. import paths
from ..control.events import EventBus
from ..data.config import Config
from ..data.db import Database
from ..data.model_io import ExportMode, export_model, find_update_target, import_model
from ..data.models import Headstamp, HeadstampParent, Model, is_foreign_model
from ..data.repository import (
    CartridgeRepo,
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)
from .dialog_model_editor import ModelEditorDialog
from .dialog_model_evaluator import ModelEvaluatorDialog
from .dialog_model_images import ModelImagesDialog
from .theme import PALETTE, row_style

# Characters disallowed in headstamp / parent names — they would collide with
# the training-image filename convention or the filesystem.
_INVALID_NAME_CHARS = re.compile(r"""[#<>:'"/\\|*?,]""")


FILTER_TYPE_ALL = "All"
FILTER_TYPE_STANDARD = "Standard"
FILTER_TYPE_COMMUNITY = "Community"
FILTER_TYPE_READONLY = "Read-only"

# Sentinel id reserved for the synthetic AI Config row. Picked far below any
# real auto-increment id so it cannot collide.
AI_CONFIG_SENTINEL_ID = -1


class ModelRowCard(ttk.Frame):
    """Wide card showing a model's metadata + inline action buttons.

    Click anywhere outside the action buttons selects the row; the buttons
    themselves fire their own commands. Active rows render with the
    `CardSel.*` style family so the highlight is theme-consistent.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        subtitle: str,
        info_pairs: list[tuple[str, str]],
        actions: list[tuple[str, Callable[[], None]]],
        active: bool,
        on_select: Callable[[], None] | None = None,
        primary_action: str | None = "Activate",
    ) -> None:
        super().__init__(parent, style="Card.TFrame", padding=12)
        self._active = active
        self._on_select = on_select

        # Left column — title, subtitle, info grid.
        left = ttk.Frame(self, style="CardRow.TFrame")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        title_row = ttk.Frame(left, style="CardRow.TFrame")
        title_row.pack(side=tk.TOP, fill=tk.X)
        self._title_lbl = ttk.Label(
            title_row,
            text=title,
            style="CardTitle.TLabel",
        )
        self._title_lbl.pack(side=tk.LEFT)
        if active:
            self._active_dot = ttk.Label(
                title_row,
                text="  ●  ACTIVE",
                style="Accent.TLabel",
            )
            self._active_dot.pack(side=tk.LEFT, padx=(8, 0))

        if subtitle:
            self._subtitle_lbl = ttk.Label(
                left,
                text=subtitle,
                style="CardMuted.TLabel",
            )
            self._subtitle_lbl.pack(side=tk.TOP, anchor="w", pady=(2, 6))

        if info_pairs:
            grid = ttk.Frame(left, style="CardRow.TFrame")
            grid.pack(side=tk.TOP, fill=tk.X, pady=(0, 2))
            for i, (label, value) in enumerate(info_pairs):
                col = i % 3
                row = i // 3
                cell = ttk.Frame(grid, style="CardRow.TFrame")
                cell.grid(row=row, column=col, sticky="w", padx=(0, 18), pady=1)
                ttk.Label(cell, text=f"{label}:", style="CardSubtle.TLabel").pack(side=tk.LEFT)
                ttk.Label(cell, text=value, style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(4, 0))

        # Right column — action buttons.
        right = ttk.Frame(self, style="CardRow.TFrame")
        right.pack(side=tk.RIGHT)
        for label, cmd in actions:
            style = (
                "Accent.TButton" if label == primary_action else ("Danger.TButton" if label == "Delete" else "TButton")
            )
            ttk.Button(right, text=label, command=cmd, style=style).pack(
                side=tk.LEFT,
                padx=(0, 6),
            )

        if on_select is not None:
            # Pass the callback through explicitly rather than reading
            # `self._on_select` from the closures below — that attribute is
            # `Callable[[], None] | None`, but this whole call is already
            # gated on `on_select is not None`.
            self._track_clicks(self, on_select)
        self._apply_card_style()

    def _track_clicks(self, widget: tk.Misc, on_select: Callable[[], None]) -> None:
        """Bind click handlers on the card frame + non-Button children."""
        for w in widget.winfo_children():
            if isinstance(w, ttk.Button):
                continue
            w.bind("<Button-1>", lambda _e: on_select())
            self._track_clicks(w, on_select)
        widget.bind("<Button-1>", lambda _e: on_select(), add="+")

    def _apply_card_style(self) -> None:
        frame_style = "CardSel.TFrame" if self._active else "Card.TFrame"
        title_style = "CardSelTitle.TLabel" if self._active else "CardTitle.TLabel"
        muted_style = "CardSelMuted.TLabel" if self._active else "CardMuted.TLabel"
        subtle_style = "CardSelSubtle.TLabel" if self._active else "CardSubtle.TLabel"
        self.configure(style=frame_style)
        self._title_lbl.configure(style=title_style)

        def _walk(w: tk.Misc) -> None:
            for child in w.winfo_children():
                if isinstance(child, ttk.Frame):
                    child.configure(style=row_style(frame_style))
                    _walk(child)
                elif isinstance(child, ttk.Label):
                    # Don't restyle the accent active dot.
                    if child.cget("style") == "Accent.TLabel":
                        continue
                    cur = child.cget("style")
                    if "Title" in cur:
                        child.configure(style=title_style)
                    elif "Subtle" in cur:
                        child.configure(style=subtle_style)
                    else:
                        child.configure(style=muted_style)

        _walk(self)


class ModelsTab(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        config: Config,
        bus: EventBus,
        app: Any,
    ) -> None:
        super().__init__(parent)
        # Not `self.config` — that collides with ttk.Widget.config().
        self.cfg = config
        self.bus = bus
        self.app = app
        self.db: Database = app.db
        self.cartridges = CartridgeRepo(self.db)
        self.models = ModelRepo(self.db)
        self.headstamps = HeadstampRepo(self.db)
        self.settings = SettingsRepo(self.db)

        self._build_filter_bar()
        self._build_toolbar()
        self._build_list()
        self.refresh()

    # ----- UI construction ----------------------------------------------------

    def _build_filter_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(bar, text="Cartridge:").pack(side=tk.LEFT)
        self.cart_var = tk.StringVar(value=FILTER_TYPE_ALL)
        self.cart_combo = ttk.Combobox(bar, state="readonly", width=14, textvariable=self.cart_var)
        self.cart_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.cart_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Label(bar, text="Type:").pack(side=tk.LEFT)
        self.type_var = tk.StringVar(value=FILTER_TYPE_ALL)
        self.type_combo = ttk.Combobox(
            bar,
            state="readonly",
            width=14,
            textvariable=self.type_var,
            values=[FILTER_TYPE_ALL, FILTER_TYPE_STANDARD, FILTER_TYPE_COMMUNITY, FILTER_TYPE_READONLY],
        )
        self.type_combo.current(0)
        self.type_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.type_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Label(bar, text="Search:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.search_var, width=24)
        entry.pack(side=tk.LEFT, padx=4)
        entry.bind("<KeyRelease>", lambda _e: self.refresh())

    def _build_toolbar(self) -> None:
        row = ttk.Frame(self)
        row.pack(fill=tk.X, padx=8, pady=(0, 4))
        ttk.Frame(row).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="New cartridge", command=self._new_cartridge).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="New model", command=self._new_model).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="Import…", command=self._import_zip).pack(side=tk.LEFT)

    def _build_list(self) -> None:
        # The whole tab is already hosted in a ScrollableFrame (see
        # app._add_scrolled), so the model list lives in a plain frame.
        # A second ScrollableFrame here would render a redundant inner
        # scrollbar alongside the tab's outer one.
        self._list_body = ttk.Frame(self)
        self._list_body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ----- data + render ------------------------------------------------------

    def refresh(self) -> None:
        carts = self.cartridges.list()
        values = [FILTER_TYPE_ALL] + [c.name for c in carts]
        self.cart_combo["values"] = values
        if self.cart_var.get() not in values:
            self.cart_var.set(FILTER_TYPE_ALL)

        # Tear down old cards.
        body = self._list_body
        for child in list(body.winfo_children()):
            child.destroy()

        active_id = self.settings.get_active_model_id()
        cart_by_id = {c.id: c.name for c in carts}
        image_counts = self._image_counts()

        # Synthetic "Use AI Config" sentinel — only shown when no type filter
        # excludes it (it isn't Standard/Community/ReadOnly so it appears in
        # "All" only).
        if self.type_var.get() == FILTER_TYPE_ALL and (
            not self.search_var.get().strip()
            or "ai" in self.search_var.get().strip().lower()
            or "config" in self.search_var.get().strip().lower()
        ):
            ModelRowCard(
                body,
                title="Use AI Config",
                subtitle="Route classification through the AI Config tab (HTTP server).",
                info_pairs=[],
                actions=[("Activate", self._activate_ai_config)] if active_id is not None else [],
                active=active_id is None,
                on_select=None,
            ).pack(side=tk.TOP, fill=tk.X, pady=(0, 8))

        for m in self._apply_filters(self.models.list(), cart_by_id):
            cart_name = cart_by_id.get(m.cartridge_id, "—")
            type_label = self._describe_type(m)
            last_trained = m.last_training_date or "—"
            info = [
                ("Cartridge", cart_name),
                ("Type", type_label),
                ("Mode", m.model_mode),
                ("Images", str(image_counts.get(m.id, m.trained_image_count))),
                ("Last trained", last_trained),
                ("Trained", "yes" if m.model_path else "no"),
            ]
            is_active = m.id == active_id
            actions: list[tuple[str, Callable[[], None]]] = []
            if not is_active:
                actions.append(("Activate", lambda mid=m.id: self._activate(mid)))
            actions.append(("Edit", lambda mid=m.id: self._edit(mid)))
            actions.append(("Headstamps", lambda mid=m.id: self._edit_headstamps(mid)))
            actions.append(("Images", lambda mid=m.id: self._images(mid)))
            # Evaluate needs a trained checkpoint to run inference against.
            if m.model_path:
                actions.append(("Evaluate", lambda mid=m.id: self._evaluate(mid)))
            actions.append(("Export", lambda mid=m.id: self._export(mid)))
            actions.append(("Delete", lambda mid=m.id: self._delete(mid)))
            ModelRowCard(
                body,
                title=m.name,
                subtitle=f"{cart_name} • {m.model_mode}",
                info_pairs=info,
                actions=actions,
                active=is_active,
                on_select=None,
            ).pack(side=tk.TOP, fill=tk.X, pady=(0, 8))

        self._refresh_status_bar()

    def _apply_filters(self, models: list[Model], cart_by_id: dict[int, str]) -> list[Model]:
        cart_filter = self.cart_var.get()
        type_filter = self.type_var.get()
        search = self.search_var.get().strip().lower()

        def match_type(m: Model) -> bool:
            is_community = bool(m.community_model_uid)
            is_readonly = is_foreign_model(m)
            if type_filter == FILTER_TYPE_COMMUNITY:
                return is_community
            if type_filter == FILTER_TYPE_READONLY:
                return is_readonly and not is_community
            if type_filter == FILTER_TYPE_STANDARD:
                return not is_community and not is_readonly
            return True

        out: list[Model] = []
        for m in models:
            if cart_filter != FILTER_TYPE_ALL and cart_by_id.get(m.cartridge_id, "") != cart_filter:
                continue
            if not match_type(m):
                continue
            if search and search not in (m.name or "").lower():
                continue
            out.append(m)
        out.sort(key=lambda m: (cart_by_id.get(m.cartridge_id, ""), m.name.lower()))
        return out

    def _describe_type(self, m: Model) -> str:
        # A community *download* can't be trained here (it's the publisher's
        # model), so say "read-only" — that's the user's only clue as to why
        # the Train tab disappears when they activate it. A model the user
        # shared themselves has a UID but is still theirs, and reads
        # "Community".
        is_community = bool(m.community_model_uid)
        is_readonly = is_foreign_model(m)
        if is_community and is_readonly:
            return "Community (read-only)"
        if is_community:
            return "Community"
        if is_readonly:
            return "Read-only"
        return "Standard"

    def _image_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        base = paths.models_dir()
        if not base.exists():
            return counts
        for entry in base.iterdir():
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            images_dir = entry / "images"
            if not images_dir.exists():
                counts[int(entry.name)] = 0
                continue
            counts[int(entry.name)] = sum(
                1 for f in images_dir.iterdir() if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")
            )
        return counts

    def _refresh_status_bar(self) -> None:
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            self.bus.post("status", "Active mode: AI Config.")
            return
        m = self.models.get(active_id)
        if m is None:
            return
        self.bus.post("status", f"Active model: {m.name} ({m.model_mode}).")

    # ----- actions ------------------------------------------------------------

    def _activate(self, model_id: int) -> None:
        self.settings.set_active_model_id(model_id)
        self.cfg.reload_headstamps_for_active_model()
        self.bus.post("mode/changed", {"active_model_id": model_id})
        self.refresh()

    def _activate_ai_config(self) -> None:
        self.settings.clear_active_model()
        self.cfg.reload_headstamps_for_active_model()
        self.bus.post("mode/changed", {"active_model_id": None})
        self.refresh()

    def _new_cartridge(self) -> None:
        name = simpledialog.askstring("New cartridge", "Cartridge name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if self.cartridges.find_by_name(name) is not None:
            messagebox.showwarning("Duplicate", f"Cartridge '{name}' already exists.", parent=self)
            return
        try:
            with self.db.transaction():
                self.cartridges.create(name)
        except Exception as exc:
            messagebox.showerror("Failed", str(exc), parent=self)
            return
        self.refresh()

    def _new_model(self) -> None:
        if not self.cartridges.list():
            messagebox.showinfo(
                "Add a cartridge first",
                "Create a cartridge before creating a model.",
                parent=self,
            )
            return
        ModelEditorDialog(self, self.db, on_saved=lambda _mid: self.refresh())

    def _edit(self, model_id: int) -> None:
        m = self.models.get(model_id)
        if m is None:
            return
        ModelEditorDialog(self, self.db, existing=m, on_saved=lambda _mid: self.refresh())

    def _delete(self, model_id: int) -> None:
        m = self.models.get(model_id)
        if m is None:
            return
        if not messagebox.askyesno(
            "Confirm delete",
            f"Delete model '{m.name}' and all associated headstamps and training images?",
            parent=self,
        ):
            return
        try:
            self.models.delete(model_id)
        except ValueError as exc:
            messagebox.showwarning("Cannot delete", str(exc), parent=self)
            return
        # Whole `<model_id>` subtree goes — images, feedback_images, checkpoint.
        per_model = paths.model_dir(model_id)
        if per_model.exists():
            shutil.rmtree(per_model, ignore_errors=True)
        self.refresh()

    def _edit_headstamps(self, model_id: int) -> None:
        m = self.models.get(model_id)
        if m is None:
            return

        def _saved() -> None:
            # Refresh our own row counts and broadcast so the Train tab's
            # label combobox / counts column pick up the change.
            self.refresh()
            self.bus.post("mode/changed", {"reason": "headstamps", "model_id": model_id})

        HeadstampManagerDialog(self, self.db, m, on_saved=_saved)

    def _evaluate(self, model_id: int) -> None:
        m = self.models.get(model_id)
        if m is None:
            return
        ModelEvaluatorDialog(self, self.db, m, app=self.app)

    def _images(self, model_id: int) -> None:
        m = self.models.get(model_id)
        if m is None:
            return
        ModelImagesDialog(self, self.db, m, app=self.app)

    def _import_zip(self) -> None:
        zip_path = filedialog.askopenfilename(
            title="Import model archive",
            filetypes=[("Model archives", "*.zip"), ("All files", "*.*")],
            parent=self,
        )
        if not zip_path:
            return
        if not messagebox.askyesno(
            "Import model — security notice",
            "A model archive contains a serialized neural network that is loaded "
            "with PyTorch. Loading a model can execute code embedded in the file, "
            "so only import models from sources you trust.\n\n"
            "Import this archive?",
            icon="warning",
            default="no",
            parent=self,
        ):
            return
        # An archive of a community model that's already installed updates it
        # in place rather than adding a duplicate. Say so before we do it —
        # it overwrites the installed checkpoint.
        try:
            target = find_update_target(zip_path, db=self.db)
        except Exception:
            target = None
        if target is not None and not messagebox.askyesno(
            "Update installed model?",
            f'This archive is a copy of "{target.name}", which is already '
            "installed.\n\nIt will be updated in place: its trained model is "
            "replaced, and its slot assignments and sorting templates are kept.",
            parent=self,
        ):
            return
        try:
            _cart_id, model_id = import_model(zip_path, db=self.db)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Import complete",
            f'Updated "{target.name}".' if target is not None else f"Imported model #{model_id}.",
            parent=self,
        )
        self.refresh()

    def _export(self, model_id: int) -> None:
        m = self.models.get(model_id)
        if m is None:
            return
        cart = self.cartridges.get(m.cartridge_id)
        if cart is None:
            return
        default_name = f"{m.name}.zip".replace(" ", "_")
        dest = filedialog.asksaveasfilename(
            title="Export model",
            defaultextension=".zip",
            initialfile=default_name,
            filetypes=[("Model archives", "*.zip")],
            parent=self,
        )
        if not dest:
            return
        headstamps = self.headstamps.list_for_model(m.id)
        try:
            export_model(
                dest,
                m,
                cartridge_name=cart.name,
                headstamps=headstamps,
                mode=ExportMode.MODEL_AND_IMAGES,
                model_file=Path(m.model_path) if m.model_path else None,
                images_dir=paths.model_images_dir(m.id),
            )
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        messagebox.showinfo("Export complete", f"Wrote {dest}", parent=self)


class _ParentEditorCombobox(ttk.Combobox):
    """A `ttk.Combobox` that remembers which tree row it's editing.

    Plain `ttk.Combobox` doesn't declare `_iid`, so bolting it on with a bare
    attribute assignment worked at runtime but had no type; this thin
    subclass gives the attribute a real home.
    """

    _iid: str


class HeadstampManagerDialog(tk.Toplevel):
    """Manage a model's headstamps and their parent classifications.

    Left:  the parent list with add / rename / delete / auto-suggest.
    Right: the headstamp grid (name + assigned parent) with an inline parent
           picker, plus add / rename / delete and a Save button.

    Parent and headstamp CRUD commit immediately; parent *assignments* are
    held in memory and written
    only on Save, so auto-suggested groupings can be reviewed first.
    """

    NONE_LABEL = "(none)"

    def __init__(
        self,
        parent: tk.Misc,
        db: Database,
        model: Model,
        *,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(f"Headstamp Manager — {model.name}")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.db = db
        self.model = model
        self.headstamps = HeadstampRepo(db)
        self.parents = HeadstampParentRepo(db)
        self.on_saved = on_saved
        self.resizable(True, True)
        self.minsize(760, 460)
        self.configure(bg=PALETTE["bg_window"])

        # In-memory parent assignments: headstamp_id -> parent_id | None.
        self._assignments: dict[int, int | None] = {}
        # Headstamp ids whose current assignment came from auto-suggest (green).
        self._suggested: set[int] = set()
        self._dirty = False
        self._editor: _ParentEditorCombobox | None = None
        self._status_after: str | None = None
        self._tree_cursor = ""
        self._hs_by_iid: dict[str, Headstamp] = {}
        self._parents_cache: list[HeadstampParent] = []

        self._build_ui()
        self._load_assignments()
        self._refresh_parents()
        self._refresh_headstamps()
        self._update_save_status()

        self.bind("<Escape>", lambda _e: self._close())
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ---- Left: parents --------------------------------------------------
        left = ttk.LabelFrame(paned, text="Parents", padding=8)
        paned.add(left, weight=1)

        plist_wrap = ttk.Frame(left)
        plist_wrap.pack(fill=tk.BOTH, expand=True)
        pscroll = ttk.Scrollbar(plist_wrap, orient=tk.VERTICAL)
        self.parent_list = tk.Listbox(
            plist_wrap,
            exportselection=False,
            activestyle="none",
            yscrollcommand=pscroll.set,
            width=22,
        )
        pscroll.config(command=self.parent_list.yview)
        pscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.parent_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.parent_list.bind("<<ListboxSelect>>", lambda _e: self._update_parent_buttons())

        add_p = ttk.Frame(left)
        add_p.pack(fill=tk.X, pady=(8, 0))
        self.new_parent_var = tk.StringVar()
        pe = ttk.Entry(add_p, textvariable=self.new_parent_var)
        pe.pack(side=tk.LEFT, fill=tk.X, expand=True)
        pe.bind("<Return>", lambda _e: self._add_parent())
        ttk.Button(add_p, text="Add", command=self._add_parent).pack(side=tk.LEFT, padx=(6, 0))

        pbtns = ttk.Frame(left)
        pbtns.pack(fill=tk.X, pady=(6, 0))
        self.btn_rename_parent = ttk.Button(
            pbtns,
            text="Rename",
            command=self._rename_parent,
            state=tk.DISABLED,
        )
        self.btn_rename_parent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        self.btn_delete_parent = ttk.Button(
            pbtns,
            text="Delete",
            command=self._delete_parent,
            state=tk.DISABLED,
            style="Danger.TButton",
        )
        self.btn_delete_parent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        ttk.Button(left, text="Auto-Suggest Parents", command=self._auto_suggest).pack(fill=tk.X, pady=(6, 0))

        # ---- Right: headstamps ---------------------------------------------
        right = ttk.LabelFrame(paned, text="Headstamps", padding=8)
        paned.add(right, weight=3)

        tree_wrap = ttk.Frame(right)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        tscroll = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("name", "parent"),
            show="headings",
            selectmode="browse",
            yscrollcommand=tscroll.set,
        )
        tscroll.config(command=self.tree.yview)
        self.tree.heading("name", text="Headstamp")
        self.tree.heading("parent", text="Parent")
        self.tree.column("name", width=280, stretch=True, anchor=tk.W)
        self.tree.column("parent", width=200, stretch=False, anchor=tk.W)
        self.tree.tag_configure(
            "suggested",
            background=PALETTE["success_dim"],
            foreground=PALETTE["success"],
        )
        tscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_hs_buttons())
        self.tree.bind("<Delete>", lambda _e: self._delete_headstamp())
        # Pointer cursor over the Parent column advertises the inline picker.
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda _e: self._set_tree_cursor(""))
        # Dismiss the inline editor if the grid scrolls or resizes under it.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Configure>"):
            self.tree.bind(seq, lambda _e: self._commit_editor(), add="+")

        hint = ttk.Label(
            right,
            text="Click a row's Parent cell (▾) to assign it to a group.",
            style="Subtle.TLabel",
        )
        hint.pack(fill=tk.X, pady=(6, 0))

        # Add-headstamp row — the entry flexes so the button never crowds it.
        add_hs = ttk.Frame(right)
        add_hs.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(add_hs, text="New:").pack(side=tk.LEFT)
        self.new_hs_var = tk.StringVar()
        he = ttk.Entry(add_hs, textvariable=self.new_hs_var)
        he.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        he.bind("<Return>", lambda _e: self._add_headstamp())
        ttk.Button(
            add_hs,
            text="Add Headstamp",
            command=self._add_headstamp,
            style="Accent.TButton",
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Selected-row actions on the left, Save on the right.
        action_row = ttk.Frame(right)
        action_row.pack(fill=tk.X, pady=(8, 0))
        self.btn_rename_hs = ttk.Button(
            action_row,
            text="Rename",
            command=self._rename_headstamp,
            state=tk.DISABLED,
        )
        self.btn_rename_hs.pack(side=tk.LEFT)
        self.btn_delete_hs = ttk.Button(
            action_row,
            text="Delete",
            command=self._delete_headstamp,
            state=tk.DISABLED,
            style="Danger.TButton",
        )
        self.btn_delete_hs.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(action_row, text="Save", command=self._save, style="Accent.TButton").pack(side=tk.RIGHT)

        # Save / suggestion status — its own full-width line so the (sometimes
        # long) message never crowds the buttons.
        self.save_status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(right, textvariable=self.save_status_var)
        self.status_label.configure(foreground=PALETTE["success"])
        self.status_label.pack(fill=tk.X, pady=(6, 0))

        # ---- Bottom bar -----------------------------------------------------
        bottom = ttk.Frame(self, style="Window.TFrame")
        bottom.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(bottom, text="Close", command=self._close).pack(side=tk.RIGHT)

    # ----- name helpers -------------------------------------------------------

    @staticmethod
    def _clean_name(name: str) -> str:
        cleaned = _INVALID_NAME_CHARS.sub("-", name or "")
        return cleaned.replace("__", "_").strip()

    def _confirm_clean(self, raw: str | None) -> str | None:
        """Return a cleaned name, or None if empty or the user declined."""
        if raw is None:
            return None
        name = raw.strip()
        cleaned = self._clean_name(name)
        if not cleaned:
            return None
        if cleaned != name and not messagebox.askyesno(
            "Clean name",
            f"The name contains invalid characters and will be changed to:\n\n{cleaned}\n\nContinue?",
            parent=self,
        ):
            return None
        return cleaned

    def _parent_name(self, parent_id: int | None) -> str | None:
        if parent_id is None:
            return None
        for p in self._parents_cache:
            if p.id == parent_id:
                return p.name
        return None

    def _parent_id_by_name(self, name: str) -> int | None:
        for p in self._parents_cache:
            if p.name == name:
                return p.id
        return None

    def _parent_cell_text(self, parent_id: int | None) -> str:
        """Parent-column text: always a value plus a dropdown chevron, so every
        row advertises the inline picker."""
        return f"{self._parent_name(parent_id) or self.NONE_LABEL}   ▾"

    # ----- data refresh -------------------------------------------------------

    def _load_assignments(self) -> None:
        self._assignments.clear()
        self._suggested.clear()
        for hs in self.headstamps.list_for_model(self.model.id):
            self._assignments[hs.id] = hs.parent_id
        self._dirty = False

    def _refresh_parents(self) -> None:
        self._parents_cache = self.parents.list_for_model(self.model.id)
        self.parent_list.delete(0, tk.END)
        for p in self._parents_cache:
            self.parent_list.insert(tk.END, p.name)
        self._update_parent_buttons()

    def _refresh_headstamps(self) -> None:
        self._commit_editor()
        self.tree.delete(*self.tree.get_children())
        self._hs_by_iid.clear()
        for hs in self.headstamps.list_for_model(self.model.id):
            iid = str(hs.id)
            self._hs_by_iid[iid] = hs
            # Preserve unsaved edits; only seed from the DB for new rows.
            if hs.id not in self._assignments:
                self._assignments[hs.id] = hs.parent_id
            pcell = self._parent_cell_text(self._assignments.get(hs.id))
            tags = ("suggested",) if hs.id in self._suggested else ()
            self.tree.insert("", "end", iid=iid, values=(hs.name, pcell), tags=tags)
        self._update_hs_buttons()

    def _update_row(self, iid: str, hs: Headstamp) -> None:
        pcell = self._parent_cell_text(self._assignments.get(hs.id))
        tags = ("suggested",) if hs.id in self._suggested else ()
        self.tree.item(iid, values=(hs.name, pcell), tags=tags)

    def _update_parent_buttons(self) -> None:
        state = tk.NORMAL if self.parent_list.curselection() else tk.DISABLED
        self.btn_rename_parent.configure(state=state)
        self.btn_delete_parent.configure(state=state)

    def _update_hs_buttons(self) -> None:
        state = tk.NORMAL if self.tree.selection() else tk.DISABLED
        self.btn_rename_hs.configure(state=state)
        self.btn_delete_hs.configure(state=state)

    def _update_save_status(self) -> None:
        self._status_after = None
        n = len(self._suggested)
        if n:
            self.save_status_var.set(f"{n} suggestion{'' if n == 1 else 's'} applied — review and Save")
        else:
            self.save_status_var.set("")

    def _show_save_success(self) -> None:
        self.save_status_var.set("Save successful")
        if self._status_after is not None:
            self.after_cancel(self._status_after)
        self._status_after = self.after(3000, self._update_save_status)

    # ----- selection helpers --------------------------------------------------

    def _selected_parent(self) -> HeadstampParent | None:
        sel = self.parent_list.curselection()
        if not sel:
            return None
        idx = sel[0]
        if 0 <= idx < len(self._parents_cache):
            return self._parents_cache[idx]
        return None

    def _selected_headstamp(self) -> Headstamp | None:
        sel = self.tree.selection()
        return self._hs_by_iid.get(sel[0]) if sel else None

    def _set_tree_cursor(self, cursor: str) -> None:
        if cursor != self._tree_cursor:
            self._tree_cursor = cursor
            self.tree.configure(cursor=cursor)

    def _on_tree_motion(self, event: tk.Event) -> None:
        over_parent = (
            self.tree.identify("region", event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#2"
        )
        self._set_tree_cursor("hand2" if over_parent else "")

    # ----- inline parent editor ----------------------------------------------

    def _on_tree_click(self, event: tk.Event) -> str | None:
        self._commit_editor()
        if self.tree.identify("region", event.x, event.y) != "cell":
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        col = self.tree.identify_column(event.x)
        self.tree.selection_set(row)
        self.tree.focus(row)
        self._update_hs_buttons()
        if col == "#2":  # Parent column → open the inline picker.
            self._open_parent_editor(row)
            return "break"  # suppress default focus handling that would close it
        return None

    def _on_tree_double(self, event: tk.Event) -> str | None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            return None
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        if self.tree.identify_column(event.x) == "#1":  # Name → rename
            self.tree.selection_set(row)
            self._rename_headstamp()
            return "break"
        return None

    def _open_parent_editor(self, iid: str) -> None:
        hs = self._hs_by_iid.get(iid)
        if hs is None:
            return
        bbox = self.tree.bbox(iid, "parent")
        if not bbox:
            return
        x, y, w, h = bbox
        combo = _ParentEditorCombobox(
            self.tree,
            state="readonly",
            values=[self.NONE_LABEL] + [p.name for p in self._parents_cache],
        )
        combo.set(self._parent_name(self._assignments.get(hs.id)) or self.NONE_LABEL)
        combo.place(x=x, y=y, width=w, height=h)
        combo._iid = iid
        combo.bind("<<ComboboxSelected>>", lambda _e: self._commit_editor())
        combo.bind("<FocusOut>", lambda _e: self._commit_editor())
        combo.bind("<Escape>", lambda _e: self._destroy_editor())
        self._editor = combo
        combo.focus_set()
        # Re-assert focus after the click settles so a stray focus change
        # doesn't dismiss the editor the instant it appears.
        self.after_idle(lambda: combo.focus_set() if self._editor is combo else None)

    def _commit_editor(self) -> None:
        combo = self._editor
        if combo is None:
            return
        self._editor = None
        iid = getattr(combo, "_iid", None)
        try:
            value = combo.get()
        except tk.TclError:
            value = None
        try:
            combo.destroy()
        except tk.TclError:
            pass
        if iid is None or value is None:
            return
        hs = self._hs_by_iid.get(iid)
        if hs is None:
            return
        new_pid = None if value == self.NONE_LABEL else self._parent_id_by_name(value)
        if new_pid == self._assignments.get(hs.id):
            return
        self._assignments[hs.id] = new_pid
        self._dirty = True
        self._suggested.discard(hs.id)
        self._update_row(iid, hs)
        self._update_save_status()

    def _destroy_editor(self) -> None:
        combo = self._editor
        if combo is None:
            return
        self._editor = None
        try:
            combo.destroy()
        except tk.TclError:
            pass

    # ----- parent CRUD --------------------------------------------------------

    def _add_parent(self) -> None:
        name = self._confirm_clean(self.new_parent_var.get())
        if not name:
            return
        if self.parents.find_by_name(self.model.id, name) is not None:
            messagebox.showwarning(
                "Duplicate",
                "A parent with that name already exists.",
                parent=self,
            )
            return
        with self.db.transaction():
            self.parents.add(self.model.id, name)
        self.new_parent_var.set("")
        self._refresh_parents()

    def _rename_parent(self) -> None:
        p = self._selected_parent()
        if p is None:
            return
        name = self._confirm_clean(
            simpledialog.askstring(
                "Rename parent",
                "New name:",
                initialvalue=p.name,
                parent=self,
            )
        )
        if not name or name == p.name:
            return
        other = self.parents.find_by_name(self.model.id, name)
        if other is not None and other.id != p.id:
            messagebox.showwarning(
                "Duplicate",
                "A parent with that name already exists.",
                parent=self,
            )
            return
        with self.db.transaction():
            self.parents.rename(p.id, name)
        self._refresh_parents()
        self._refresh_headstamps()

    def _delete_parent(self) -> None:
        p = self._selected_parent()
        if p is None:
            return
        if not messagebox.askyesno(
            "Delete parent",
            f'Delete parent "{p.name}"? Its child headstamps will be unlinked but not deleted.',
            parent=self,
        ):
            return
        # Drop in-memory assignments that point at the parent we're removing.
        for hid, pid in list(self._assignments.items()):
            if pid == p.id:
                self._assignments[hid] = None
        self._suggested.clear()
        with self.db.transaction():
            self.parents.delete(p.id)
        self._refresh_parents()
        self._refresh_headstamps()
        self._update_save_status()

    # ----- headstamp CRUD -----------------------------------------------------

    def _add_headstamp(self) -> None:
        name = self._confirm_clean(self.new_hs_var.get())
        if not name:
            return
        existing = {h.name.casefold() for h in self.headstamps.list_for_model(self.model.id)}
        if name.casefold() in existing:
            messagebox.showwarning(
                "Duplicate",
                "A headstamp with that name already exists.",
                parent=self,
            )
            return
        try:
            with self.db.transaction():
                hs = self.headstamps.add(self.model.id, name)
        except Exception as exc:
            messagebox.showerror("Add failed", str(exc), parent=self)
            return
        self._assignments[hs.id] = None
        self.new_hs_var.set("")
        self._refresh_headstamps()
        # Select and reveal the new row.
        iid = str(hs.id)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _rename_headstamp(self) -> None:
        hs = self._selected_headstamp()
        if hs is None:
            return
        name = self._confirm_clean(
            simpledialog.askstring(
                "Rename headstamp",
                "New name:",
                initialvalue=hs.name,
                parent=self,
            )
        )
        if not name or name == hs.name:
            return
        existing = {h.name.casefold(): h.id for h in self.headstamps.list_for_model(self.model.id)}
        if existing.get(name.casefold(), hs.id) != hs.id:
            messagebox.showwarning(
                "Duplicate",
                "A headstamp with that name already exists.",
                parent=self,
            )
            return
        # Keep training images in sync with the new label.
        self._rename_headstamp_images(hs.name, name)
        with self.db.transaction():
            self.headstamps.rename(hs.id, name)
        self._refresh_headstamps()

    def _delete_headstamp(self) -> None:
        hs = self._selected_headstamp()
        if hs is None:
            return
        if not messagebox.askyesno(
            "Delete headstamp",
            f'Delete headstamp "{hs.name}"?',
            parent=self,
        ):
            return
        with self.db.transaction():
            self.headstamps.delete(hs.id)
        self._assignments.pop(hs.id, None)
        self._suggested.discard(hs.id)
        self._refresh_headstamps()
        self._update_save_status()

    def _rename_headstamp_images(self, old_name: str, new_name: str) -> None:
        """Rename ``{old}__*`` training images to ``{new}__*`` on disk.

        Best-effort: training images are optional and may live elsewhere, so
        any failure is swallowed rather than blocking the rename.
        """
        images_dir = paths.model_images_dir(self.model.id)
        if not images_dir.exists():
            return
        prefix = f"{old_name}__"
        for path in list(images_dir.iterdir()):
            if not path.is_file() or not path.name.startswith(prefix):
                continue
            dest = images_dir / (new_name + path.name[len(old_name) :])
            if dest.exists():
                continue
            try:
                path.rename(dest)
            except OSError:
                pass

    # ----- auto-suggest -------------------------------------------------------

    def _auto_suggest(self) -> None:
        rows = self.headstamps.list_for_model(self.model.id)
        if len(rows) < 2:
            messagebox.showinfo(
                "Auto-suggest",
                "Not enough headstamps to suggest parents.",
                parent=self,
            )
            return
        groups = self._build_prefix_groups(sorted(r.name for r in rows))
        if not groups:
            messagebox.showinfo(
                "Auto-suggest",
                "No common prefixes found to suggest parent groups.",
                parent=self,
            )
            return
        hs_by_name = {r.name: r for r in rows}
        applied = 0
        with self.db.transaction():
            for prefix, members in groups.items():
                parent = self.parents.find_by_name(self.model.id, prefix)
                if parent is None:
                    parent = self.parents.add(self.model.id, prefix)
                for child_name in members:
                    hs = hs_by_name.get(child_name)
                    if hs is None or self._assignments.get(hs.id) is not None:
                        continue  # never override an existing assignment
                    self._assignments[hs.id] = parent.id
                    self._suggested.add(hs.id)
                    applied += 1
        self._refresh_parents()
        self._refresh_headstamps()
        self._update_save_status()
        if applied:
            self._dirty = True
        else:
            messagebox.showinfo(
                "Auto-suggest",
                "All headstamps already have parents assigned.",
                parent=self,
            )

    @staticmethod
    def _build_prefix_groups(names: list[str]) -> dict[str, list[str]]:
        """Group names by leading alpha prefix; keep groups of two or more."""
        groups: dict[str, list[str]] = {}
        for name in names:
            m = re.match(r"[A-Za-z]+", name)
            if not m:
                continue
            groups.setdefault(m.group(0).upper(), []).append(name)
        return {k: v for k, v in groups.items() if len(v) >= 2}

    # ----- save / close -------------------------------------------------------

    def _save(self) -> None:
        self._commit_editor()
        valid_pids = {p.id for p in self._parents_cache}
        with self.db.transaction():
            for hid in list(self._assignments):
                pid = self._assignments[hid]
                if pid is not None and pid not in valid_pids:
                    pid = None
                    self._assignments[hid] = None
                self.headstamps.set_parent(hid, pid)
        self._suggested.clear()
        self._dirty = False
        self._refresh_headstamps()
        self._show_save_success()

    def _close(self) -> None:
        self._destroy_editor()
        if self._dirty and not messagebox.askyesno(
            "Unsaved changes",
            "You have unsaved parent assignments. Close without saving?",
            parent=self,
        ):
            return
        if callable(self.on_saved):
            self.on_saved()
        self.destroy()
