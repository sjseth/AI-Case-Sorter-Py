"""Modal dialogs for the Run tab's sorting templates.

Two small dialogs, both driven by ``Config``'s template API:

  * ``NewSlotTemplateDialog``  — name + "copy current layout" / "start empty"
  * ``EditSlotTemplateDialog`` — rename, or delete (never the last one)

They deliberately stay tiny: the template list itself lives in the Run tab's
toolbar, so these only handle the two operations that need typing.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from ..data.config import Config
from ..data.models import SlotTemplate


def _mode_noun(mode: str) -> str:
    """Wording that reminds the user which template list they're editing."""
    return "package-mode " if mode == "package" else ""


class _TemplateDialog(tk.Toplevel):
    """Shared chrome: transient, non-resizable, Return/Escape bindings."""

    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent)
        self.title(title)
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        self.body = ttk.Frame(self, padding=(14, 12, 14, 8))
        self.body.pack(fill=tk.BOTH, expand=True)

        self.buttons = ttk.Frame(self, padding=(14, 0, 14, 12))
        self.buttons.pack(fill=tk.X)

        self.bind("<Escape>", lambda _e: self.destroy())

    def _name_entry(self, variable: tk.StringVar) -> ttk.Entry:
        ttk.Label(self.body, text="Template name", style="Muted.TLabel").pack(anchor=tk.W)
        entry = ttk.Entry(self.body, textvariable=variable, width=34)
        entry.pack(fill=tk.X, pady=(3, 0))
        entry.focus_set()
        return entry


class NewSlotTemplateDialog(_TemplateDialog):
    """Create a sorting template, optionally seeded from the current layout."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        config: Config,
        mode: str,
        current_name: str,
        on_created: Callable[[SlotTemplate], None] | None = None,
    ) -> None:
        super().__init__(parent, "New Sorting Template")
        self.config_obj = config
        self.mode = mode
        self.on_created = on_created

        self.name_var = tk.StringVar()
        entry = self._name_entry(self.name_var)

        ttk.Separator(self.body, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 8))

        self.copy_var = tk.BooleanVar(value=True)
        ttk.Radiobutton(
            self.body,
            text=f"Copy the slot assignments from “{current_name}”",
            variable=self.copy_var,
            value=True,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            self.body,
            text="Start fresh with no slot assignments",
            variable=self.copy_var,
            value=False,
        ).pack(anchor=tk.W, pady=(2, 0))

        ttk.Label(
            self.body,
            text=(
                f"Saved as a {_mode_noun(mode)}template for the active model. "
                "Switching templates swaps the whole slot layout."
            ),
            style="Subtle.TLabel",
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))

        ttk.Button(self.buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            self.buttons,
            text="Create",
            command=self._create,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT)

        entry.bind("<Return>", lambda _e: self._create())

    def _create(self) -> None:
        try:
            template = self.config_obj.create_slot_template(
                self.name_var.get(),
                copy_current=self.copy_var.get(),
                mode=self.mode,
            )
        except ValueError as exc:
            messagebox.showwarning("Can't create template", str(exc), parent=self)
            return
        if self.on_created is not None:
            self.on_created(template)
        self.destroy()


class EditSlotTemplateDialog(_TemplateDialog):
    """Rename or delete an existing sorting template."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        config: Config,
        template: SlotTemplate,
        can_delete: bool,
        on_changed: Callable[[SlotTemplate | None], None] | None = None,
    ) -> None:
        super().__init__(parent, "Edit Sorting Template")
        self.config_obj = config
        self.template = template
        self.on_changed = on_changed

        self.name_var = tk.StringVar(value=template.name)
        entry = self._name_entry(self.name_var)
        entry.selection_range(0, tk.END)

        ttk.Label(
            self.body,
            text=(
                f"A {_mode_noun(template.mode)}template for the active model. "
                + (
                    "Deleting one drops its slot layout — the headstamps themselves are untouched."
                    if can_delete
                    else "This is the only one, so it can't be deleted."
                )
            ),
            style="Subtle.TLabel",
            wraplength=330,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 2))

        if can_delete:
            ttk.Button(
                self.buttons,
                text="Delete",
                command=self._delete,
                style="Danger.TButton",
            ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(self.buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            self.buttons,
            text="Save",
            command=self._save,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT)

        entry.bind("<Return>", lambda _e: self._save())

    def _save(self) -> None:
        try:
            self.config_obj.rename_slot_template(self.template.id, self.name_var.get())
        except ValueError as exc:
            messagebox.showwarning("Can't rename template", str(exc), parent=self)
            return
        if self.on_changed is not None:
            self.on_changed(self.template)
        self.destroy()

    def _delete(self) -> None:
        if not messagebox.askyesno(
            "Delete template",
            f"Delete the sorting template “{self.template.name}”?\n\n"
            "Its slot assignments are lost; the headstamps themselves are untouched.",
            parent=self,
        ):
            return
        try:
            active = self.config_obj.delete_slot_template(self.template.id)
        except ValueError as exc:
            messagebox.showwarning("Can't delete template", str(exc), parent=self)
            return
        if self.on_changed is not None:
            self.on_changed(active)
        self.destroy()
