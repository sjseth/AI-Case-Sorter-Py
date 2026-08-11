"""Theme editor — build your own palette from one of the shipped themes.

Opened by the gear beside the title-bar theme picker. It starts from the
active theme, so a user only has to change the handful of colours they care
about; everything else keeps working because a theme is always a *full*
palette (see ``theme.normalize_palette``).

On a saved theme, **Save & apply** writes back to it — renaming included.
**Create new…** always makes a separate theme, so a built-in is never the
thing being written to.

Saved themes live in the ``ui.custom_themes`` setting and are registered into
``theme.THEMES`` at startup, which is all it takes for them to appear in the
picker — nothing downstream knows a palette was made by a user. The same
payload is what Export writes and Import reads, so a theme travels as one
small JSON file.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

from .theme import (
    BUILTIN_THEMES,
    CUSTOM_THEME_VERSION,
    HALFTONE_INK,
    INK_OUTLINE,
    MAX_THEME_NAME,
    PALETTE,
    THEMES,
    current_theme,
    custom_theme_payload,
    editable_roles,
    is_custom_theme,
    normalize_palette,
    paint_gradient,
    paint_halftone,
    register_custom_theme,
    rename_custom_theme,
    unique_theme_name,
    unregister_custom_theme,
)
from .widgets import ScrollableFrame

# Role → the words a user thinks in. Grouped the way the UI reads: surfaces
# first, then the ink on them, then the things that mean something.
_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Surfaces",
        [
            ("bg_window", "Window"),
            ("bg_surface", "Panel"),
            ("bg_card", "Card"),
            ("bg_card_hover", "Card — hover"),
            ("bg_card_sel", "Card — selected"),
            ("bg_input", "Input field"),
            ("bg_gradient_a", "Title bar — left"),
            ("bg_gradient_b", "Title bar — right"),
        ],
    ),
    (
        "Lines",
        [
            ("border", "Border"),
            ("border_focus", "Focus ring"),
        ],
    ),
    (
        "Text",
        [
            ("text", "Text"),
            ("text_highlight", "Text — emphasis"),
            ("text_muted", "Text — muted"),
            ("text_subtle", "Text — subtle"),
            ("text_inverse", "Text on a colour"),
        ],
    ),
    (
        "Accent",
        [
            ("accent", "Accent"),
            ("accent_hover", "Accent — hover"),
            ("accent_press", "Accent — pressed"),
            ("accent_dim", "Secondary button"),
        ],
    ),
    (
        "Primary button",
        [
            ("action", "Primary"),
            ("action_hover", "Primary — hover"),
            ("action_press", "Primary — pressed"),
        ],
    ),
    (
        "Update button",
        [
            ("update", "Update"),
            ("update_hover", "Update — hover"),
            ("update_press", "Update — pressed"),
        ],
    ),
    (
        "Stop / delete button",
        [
            ("danger", "Danger"),
            ("danger_hover", "Danger — hover"),
            ("danger_press", "Danger — pressed"),
        ],
    ),
    (
        "Status",
        [
            ("warning", "Warning text"),
            ("success_dim", "Success fill"),
        ],
    ),
]

_FILE_TYPES = [("Case Sorter theme", "*.json"), ("All files", "*.*")]

PREVIEW_W = 300
PREVIEW_H = 330
# The colour list scrolls; this is how much of it is on screen at once.
LIST_W = 360
LIST_H = 470


def _grouped_roles() -> list[tuple[str, list[tuple[str, str]]]]:
    """The groups above, plus any palette role they forgot to mention.

    A new key in the palette should show up in the editor even if whoever
    added it didn't come here — better an ugly "Other" row than a colour the
    user can't reach.
    """
    listed = {role for _, rows in _GROUPS for role, _ in rows}
    extra = [(role, role.replace("_", " ").capitalize()) for role in editable_roles() if role not in listed]
    return _GROUPS + ([("Other", extra)] if extra else [])


class ThemeEditorDialog(tk.Toplevel):
    """Create or edit a user theme, with a live preview of the result."""

    def __init__(self, parent: tk.Misc, *, app) -> None:
        super().__init__(parent)
        self.app = app
        self.base = current_theme()
        self.editing = is_custom_theme(self.base)

        self.title("Edit Theme" if self.editing else "New Theme")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.minsize(LIST_W + PREVIEW_W + 70, 420)
        self.configure(bg=PALETTE["bg_surface"])

        # Working copy — nothing here touches the live palette until Save.
        self.values: dict[str, str] = dict(THEMES[self.base])
        # A new theme gets a short, free name rather than "<base> copy" —
        # names are capped for the picker's sake, and the header line below
        # already says which theme this started from.
        self.name_var = tk.StringVar(value=self.base if self.editing else unique_theme_name("My theme"))
        self.outline_var = tk.BooleanVar(value=bool(INK_OUTLINE.get(self.base)))
        self.halftone_var = tk.BooleanVar(value=bool(HALFTONE_INK.get(self.base)))
        self.halftone_ink = HALFTONE_INK.get(self.base) or self.values["border"]
        self._swatches: dict[str, tk.Label] = {}
        self._hex_vars: dict[str, tk.StringVar] = {}

        wrap = ttk.Frame(self, padding=(12, 10, 12, 8))
        wrap.pack(fill=tk.BOTH, expand=True)
        self._build_header(wrap)

        columns = ttk.Frame(wrap)
        columns.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._build_color_list(columns)
        self._build_preview(columns)

        self._build_buttons(ttk.Frame(self, padding=(12, 0, 12, 12)))
        self.name_var.trace_add("write", lambda *_a: self._render_preview())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._render_preview()

    # ----- construction -------------------------------------------------------

    def _build_header(self, parent: tk.Misc) -> None:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Name", style="Subtitle.TLabel").pack(side=tk.LEFT)
        entry = ttk.Entry(
            row,
            textvariable=self.name_var,
            width=MAX_THEME_NAME + 2,
            validate="key",
            validatecommand=(self.register(_within_limit), "%P"),
        )
        entry.pack(side=tk.LEFT, padx=(8, 16))
        entry.focus_set()

        ttk.Checkbutton(
            row,
            text="Comic ink outlines",
            variable=self.outline_var,
            command=self._render_preview,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            row,
            text="Halftone dots",
            variable=self.halftone_var,
            command=self._render_preview,
        ).pack(side=tk.LEFT, padx=(12, 4))
        self._halftone_swatch = self._swatch(row, self.halftone_ink)
        self._halftone_swatch.pack(side=tk.LEFT)
        self._halftone_swatch.bind("<Button-1>", lambda _e: self._pick_halftone())

        ttk.Label(
            parent,
            text=(
                f"Editing “{self.base}” — Save & apply writes back to it, including a rename."
                if self.editing
                else f"Starting from the built-in “{self.base}”, which can't be "
                "changed. Save & apply keeps this as a new theme."
            ),
            style="Subtle.TLabel",
        ).pack(anchor=tk.W, pady=(6, 0))

    def _build_color_list(self, parent: tk.Misc) -> None:
        holder = ScrollableFrame(parent, viewport=(LIST_W, LIST_H))
        holder.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = holder.body

        for title, rows in _grouped_roles():
            ttk.Label(body, text=title, style="Header.TLabel").pack(anchor=tk.W, pady=(8, 2))
            grid = ttk.Frame(body)
            grid.pack(fill=tk.X)
            for i, (role, label) in enumerate(rows):
                self._build_color_row(grid, role, label, i)

    def _build_color_row(self, parent: tk.Misc, role: str, label: str, row: int) -> None:
        swatch = self._swatch(parent, self.values[role])
        swatch.grid(row=row, column=0, sticky="w", pady=1)
        swatch.bind("<Button-1>", lambda _e, r=role: self._pick(r))
        self._swatches[role] = swatch

        ttk.Label(parent, text=label).grid(row=row, column=1, sticky="w", padx=8)
        parent.columnconfigure(1, minsize=150)

        var = tk.StringVar(value=self.values[role])
        var.trace_add("write", lambda *_a, r=role: self._on_hex_typed(r))
        self._hex_vars[role] = var
        ttk.Entry(parent, textvariable=var, width=9).grid(row=row, column=2, sticky="w", padx=(8, 0))

    def _swatch(self, parent: tk.Misc, color: str) -> tk.Label:
        return tk.Label(
            parent,
            background=color,
            width=4,
            cursor="hand2",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
        )

    def _build_preview(self, parent: tk.Misc) -> None:
        side = ttk.Frame(parent)
        side.pack(side=tk.LEFT, fill=tk.Y, anchor=tk.N, padx=(12, 0))
        ttk.Label(side, text="Preview", style="Subtitle.TLabel").pack(anchor=tk.W)
        self.preview = tk.Canvas(
            side,
            width=PREVIEW_W,
            height=PREVIEW_H,
            highlightthickness=1,
            highlightbackground=PALETTE["border"],
            borderwidth=0,
        )
        self.preview.pack(pady=(3, 0))
        # The gradient helper measures the canvas, so the first useful paint
        # can only happen once Tk has laid it out.
        self.preview.bind("<Configure>", lambda _e: self._render_preview())

    def _build_buttons(self, bar: ttk.Frame) -> None:
        bar.pack(fill=tk.X)
        if self.editing:
            ttk.Button(
                bar,
                text="Delete",
                style="Danger.TButton",
                command=self._delete,
            ).pack(side=tk.LEFT)
        ttk.Button(bar, text="Import…", command=self._import).pack(side=tk.LEFT, padx=(12 if self.editing else 0, 6))
        ttk.Button(bar, text="Export…", command=self._export).pack(side=tk.LEFT)

        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            bar,
            text="Save & apply",
            style="Accent.TButton",
            command=self._save,
        ).pack(side=tk.RIGHT)
        ttk.Button(bar, text="Create new…", command=self._create_new).pack(side=tk.RIGHT, padx=(0, 8))

    # ----- editing ------------------------------------------------------------

    def _pick(self, role: str) -> None:
        chosen = colorchooser.askcolor(
            color=self.values[role],
            parent=self,
            title=f"Colour — {role}",
        )
        if chosen and chosen[1]:
            self._set_color(role, chosen[1])

    def _pick_halftone(self) -> None:
        chosen = colorchooser.askcolor(
            color=self.halftone_ink,
            parent=self,
            title="Colour — halftone dots",
        )
        if chosen and chosen[1]:
            self.halftone_ink = chosen[1].lower()
            self._halftone_swatch.configure(background=self.halftone_ink)
            self.halftone_var.set(True)
            self._render_preview()

    def _set_color(self, role: str, color: str) -> None:
        color = color.lower()
        self.values[role] = color
        self._swatches[role].configure(background=color)
        if self._hex_vars[role].get().lower() != color:
            self._hex_vars[role].set(color)
        self._render_preview()

    def _on_hex_typed(self, role: str) -> None:
        """Accept a hand-typed colour once it's complete, ignore it until then."""
        text = self._hex_vars[role].get().strip()
        if not text.startswith("#"):
            text = "#" + text
        if len(text) != 7:
            return
        try:
            int(text[1:], 16)
        except ValueError:
            return
        if text.lower() != self.values[role]:
            self._set_color(role, text)

    # ----- preview ------------------------------------------------------------

    def _render_preview(self) -> None:
        """Draw a miniature of the app from the working palette.

        Canvas shapes rather than real widgets: the preview has to show a
        palette that isn't loaded, and ttk styles are global.
        """
        if not hasattr(self, "preview"):
            return
        c, v = self.preview, self.values
        c.delete("all")
        c.configure(background=v["bg_window"])
        ink = v["border"] if self.outline_var.get() else ""
        outline_w = 2 if self.outline_var.get() else 1

        # Title bar, with the halftone screening in from the right.
        c.create_rectangle(0, 0, PREVIEW_W, 34, fill=v["bg_gradient_a"], outline="")
        paint_gradient(
            c,
            color_a=v["bg_gradient_a"],
            color_b=v["bg_gradient_b"],
            direction="horizontal",
        )
        c.create_rectangle(0, 34, PREVIEW_W, PREVIEW_H, fill=v["bg_window"], outline="")
        if self.halftone_var.get():
            paint_halftone(
                c,
                color=self.halftone_ink,
                box=(0, 0, PREVIEW_W, 34),
                fade_from="right",
                fade_span=PREVIEW_W * 0.55,
                spacing=6,
                radius=1.7,
            )
        c.create_text(
            10,
            17,
            anchor=tk.W,
            text="AI Case Sorter",
            fill=v["text"],
            font=("TkDefaultFont", 11, "bold"),
        )

        # Panel with a section heading.
        c.create_rectangle(
            8,
            44,
            PREVIEW_W - 8,
            PREVIEW_H - 40,
            fill=v["bg_surface"],
            outline=ink,
            width=outline_w,
        )
        c.create_text(
            18,
            58,
            anchor=tk.W,
            text="Slots",
            fill=v["accent"],
            font=("TkDefaultFont", 9, "bold"),
        )

        # A plain card and a selected one.
        for x0, fill, title in (
            (18, v["bg_card"], "Slot #1"),
            (160, v["bg_card_sel"], "Slot #2"),
        ):
            c.create_rectangle(
                x0,
                70,
                x0 + 122,
                128,
                fill=fill,
                outline=ink,
                width=outline_w,
            )
            c.create_text(
                x0 + 10,
                84,
                anchor=tk.W,
                text=title,
                fill=v["text"],
                font=("TkDefaultFont", 9, "bold"),
            )
            c.create_text(
                x0 + 10,
                102,
                anchor=tk.W,
                text="(no headstamps)",
                fill=v["text_muted"],
                font=("TkDefaultFont", 8),
            )

        # An input field.
        c.create_rectangle(
            18,
            140,
            PREVIEW_W - 18,
            164,
            fill=v["bg_input"],
            outline=v["border"],
            width=outline_w,
        )
        c.create_text(
            26,
            152,
            anchor=tk.W,
            text="Confidence floor",
            fill=v["text_subtle"],
            font=("TkDefaultFont", 8),
        )

        # The three coloured buttons and the secondary one.
        buttons = (
            (v["action"], "Start"),
            (v["update"], "Update"),
            (v["danger"], "Stop"),
        )
        x = 18
        for fill, label in buttons:
            c.create_rectangle(
                x,
                176,
                x + 84,
                202,
                fill=fill,
                outline=ink,
                width=outline_w,
            )
            c.create_text(
                x + 42,
                189,
                text=label,
                fill=v["text_inverse"],
                font=("TkDefaultFont", 8, "bold"),
            )
            x += 89
        c.create_rectangle(
            18,
            210,
            102,
            236,
            fill=v["accent_dim"],
            outline=ink,
            width=outline_w,
        )
        c.create_text(
            60,
            223,
            text="Secondary",
            fill=v["text"],
            font=("TkDefaultFont", 8),
        )

        # Status text and the two connection dots.
        c.create_text(
            116,
            223,
            anchor=tk.W,
            text="Warning",
            fill=v["warning"],
            font=("TkDefaultFont", 8),
        )
        c.create_text(
            18,
            252,
            anchor=tk.W,
            text="●  connected",
            fill=v["action"],
            font=("TkDefaultFont", 8),
        )
        c.create_text(
            18,
            268,
            anchor=tk.W,
            text="●  disconnected",
            fill=v["danger"],
            font=("TkDefaultFont", 8),
        )
        c.create_text(
            18,
            PREVIEW_H - 22,
            anchor=tk.W,
            text=self.name_var.get() or "Untitled",
            fill=v["text_muted"],
            font=("TkDefaultFont", 8),
        )

    # ----- save / delete ------------------------------------------------------

    def _payload_named(self, name: str) -> dict:
        payload = self._payload()
        payload["name"] = name
        return payload

    def _payload(self) -> dict:
        return {
            "version": CUSTOM_THEME_VERSION,
            "name": self.name_var.get().strip(),
            "based_on": self.base,
            "halftone": self.halftone_ink if self.halftone_var.get() else None,
            "outline": 2 if self.outline_var.get() else 0,
            "palette": normalize_palette(self.values),
        }

    def _save(self) -> None:
        """Write the edits back to the theme being edited, or create it.

        On a built-in there is nothing to write back to, so this creates a
        theme under the name in the box (that's what the box is prefilled
        with). On a saved theme it updates it in place — including a rename,
        which keeps the theme rather than leaving the old name behind.
        """
        name = self.name_var.get().strip()
        if not self._name_is_usable(name):
            return
        if self.editing and name != self.base:
            try:
                rename_custom_theme(self.base, name)
            except ValueError as exc:
                messagebox.showwarning("Can't rename", str(exc), parent=self)
                return
            self.base = name
        elif (
            not self.editing
            and name in THEMES
            and not messagebox.askyesno(
                "Replace theme",
                f"Replace the saved theme “{name}”?",
                parent=self,
            )
        ):
            return
        self._register_and_apply(self._payload_named(name))
        self.destroy()

    def _create_new(self) -> None:
        """Save these colours as a separate theme, under a name of its own."""
        name = self._prompt_name()
        if name is None:
            return
        name = name.strip()
        if not self._name_is_usable(name) or (
            name in THEMES
            and not messagebox.askyesno(
                "Replace theme",
                f"Replace the saved theme “{name}”?",
                parent=self,
            )
        ):
            return
        self._register_and_apply(self._payload_named(name))
        self.destroy()

    def _name_is_usable(self, name: str) -> bool:
        """Complain (and return False) if `name` can't be a theme."""
        if not name:
            messagebox.showwarning("Name needed", "Give the theme a name.", parent=self)
            return False
        if len(name) > MAX_THEME_NAME:
            messagebox.showwarning(
                "Name too long",
                f"Theme names are at most {MAX_THEME_NAME} characters.",
                parent=self,
            )
            return False
        if name in BUILTIN_THEMES:
            messagebox.showwarning(
                "Built-in theme",
                f"“{name}” is a built-in theme. Pick a different name.",
                parent=self,
            )
            return False
        return True

    def _prompt_name(self) -> str | None:
        """Ask for a name for a new theme. None if the user backs out."""
        return _NameDialog(
            self,
            initial=unique_theme_name(self.name_var.get() or self.base),
        ).result

    def _register_and_apply(self, payload: dict) -> None:
        register_custom_theme(
            payload["name"],
            payload["palette"],
            halftone=payload.get("halftone"),
            outline=payload.get("outline", 0),
            base=payload.get("based_on"),
        )
        self.app.save_custom_themes()
        self.app.set_theme(payload["name"])
        self.app.refresh_theme_picker()

    def _delete(self) -> None:
        if messagebox.askyesno(
            "Delete theme",
            f"Delete the theme “{self.base}”?",
            parent=self,
        ):
            self._delete_confirmed()

    def _delete_confirmed(self) -> None:
        # Fall back to whatever this theme was made from; that may itself have
        # been deleted since, in which case `set_theme` resolves to the default.
        fallback = self.app.theme_name
        if fallback == self.base:
            fallback = custom_theme_payload(self.base).get("based_on") or ""
        unregister_custom_theme(self.base)
        self.app.save_custom_themes()
        self.app.set_theme(fallback)
        self.app.refresh_theme_picker()
        self.destroy()

    # ----- files --------------------------------------------------------------

    def _export(self) -> None:
        if not self.name_var.get().strip():
            messagebox.showwarning(
                "Name needed",
                "Give the theme a name before exporting.",
                parent=self,
            )
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export theme",
            defaultextension=".json",
            initialfile=f"{self.name_var.get().strip()}.json",
            filetypes=_FILE_TYPES,
        )
        if not path:
            return
        try:
            self._write_theme_file(Path(path))
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        messagebox.showinfo("Theme exported", f"Saved to {path}", parent=self)

    def _write_theme_file(self, path: Path) -> dict:
        """Write the theme as it stands in the editor — unsaved edits included."""
        payload = self._payload()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def _import(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Import theme",
            filetypes=_FILE_TYPES,
        )
        if not path:
            return
        try:
            self._read_theme_file(Path(path))
        except (OSError, ValueError) as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return
        self.destroy()

    def _read_theme_file(self, path: Path) -> None:
        """Register and apply the theme in `path`; raises ValueError if it isn't one.

        An imported theme never replaces an existing one — it comes in under a
        free name, so importing the file you exported leaves you with both.
        """
        payload = json.loads(path.read_text(encoding="utf-8"))
        problem = _describe_bad_theme(payload)
        if problem:
            raise ValueError(problem)
        self._register_and_apply(
            {
                "version": CUSTOM_THEME_VERSION,
                "name": unique_theme_name(str(payload.get("name") or path.stem)),
                "based_on": payload.get("based_on"),
                "halftone": payload.get("halftone"),
                "outline": payload.get("outline", 0),
                "palette": normalize_palette(payload.get("palette")),
            }
        )


def _within_limit(proposed: str) -> bool:
    """Entry validator: keep theme names inside the picker's width."""
    return len(proposed) <= MAX_THEME_NAME


class _NameDialog(tk.Toplevel):
    """Ask for a new theme's name. `result` is None if the user backs out."""

    def __init__(self, parent: tk.Misc, *, initial: str) -> None:
        super().__init__(parent)
        self.result: str | None = None
        self.title("New Theme")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        body = ttk.Frame(self, padding=(14, 12, 14, 8))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Name for the new theme", style="Muted.TLabel").pack(anchor=tk.W)
        self._var = tk.StringVar(value=initial)
        entry = ttk.Entry(
            body,
            textvariable=self._var,
            width=MAX_THEME_NAME + 4,
            validate="key",
            validatecommand=(self.register(_within_limit), "%P"),
        )
        entry.pack(fill=tk.X, pady=(3, 0))
        entry.selection_range(0, tk.END)
        entry.focus_set()
        ttk.Label(
            body,
            text=f"Up to {MAX_THEME_NAME} characters.",
            style="Subtle.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            buttons,
            text="Create",
            style="Accent.TButton",
            command=self._accept,
        ).pack(side=tk.RIGHT)

        entry.bind("<Return>", lambda _e: self._accept())
        self.bind("<Escape>", lambda _e: self.destroy())
        # Modal: the caller reads `result` as soon as this returns.
        self.grab_set()
        self.wait_window(self)

    def _accept(self) -> None:
        self.result = self._var.get()
        self.destroy()


def _describe_bad_theme(payload) -> str | None:
    """Why this file can't be a theme, or None if it can."""
    if not isinstance(payload, dict):
        return "That file doesn't contain a theme."
    if not isinstance(payload.get("palette"), dict):
        return "That file has no theme colours in it."
    try:
        version = int(payload.get("version", CUSTOM_THEME_VERSION))
    except (TypeError, ValueError):
        return "That theme file's version isn't readable."
    if version > CUSTOM_THEME_VERSION:
        return "That theme was made by a newer version of the app. Update, then import it again."
    return None
