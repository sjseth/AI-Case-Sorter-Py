"""Theme editor — build your own palette from one of the shipped themes.

Themes persist to the ``ui.custom_themes`` settings row. The editor starts
from the theme the window is currently showing, because a theme is always a
*full* palette (``palettes.normalize_palette``) and a user only wants to
change a few roles.

On a saved theme, **Save & apply** writes back to it — renaming included, which
moves the theme rather than copying it — and leaves the dialog open (Seth):
saving is the iteration step, not the exit. **Create new…** always makes a
separate theme, so a built-in is never the thing being written to.

The comic-ink and halftone options are edited and stored faithfully but render
flat (known gap): nothing paints them yet.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .palettes import (
    BUILTIN_THEMES,
    CUSTOM_THEME_VERSION,
    DERIVED_ROLES,
    HALFTONE_INK,
    INK_OUTLINE,
    MAX_THEME_NAME,
    SETTING_CUSTOM_THEMES,
    THEMES,
    custom_theme_payload,
    custom_themes_payload,
    editable_roles,
    is_custom_theme,
    normalize_palette,
    register_custom_theme,
    rename_custom_theme,
    resolve_theme,
    theme_names,
    unique_theme_name,
    unregister_custom_theme,
)
from .theme import build_stylesheet

# Role → the words a user thinks in, grouped the way the UI reads: surfaces
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

# Written with the pattern repeated outside the parentheses: GNOME's file
# chooser strips the "(*.json)" part, leaving an unreadable label otherwise.
FILE_FILTER = "Case Sorter theme — *.json (*.json)"

DERIVED_NOTE = "Success mirrors Primary and Error mirrors Danger — they follow along, so they aren't listed."
OPTIONS_NOTE = "Ink outlines and halftone dots are saved with the theme but render in the classic UI only."

PREVIEW_WIDTH = 300
LIST_MIN_WIDTH = 380


def grouped_roles() -> list[tuple[str, list[tuple[str, str]]]]:
    """The groups above, plus any palette role they forgot to mention.

    A new key in the palette should show up in the editor even if whoever added
    it didn't come here — better an ugly "Other" row than an unreachable colour.
    """
    listed = {role for _, rows in _GROUPS for role, _ in rows}
    extra = [(role, role.replace("_", " ").capitalize()) for role in editable_roles() if role not in listed]
    return _GROUPS + ([("Other", extra)] if extra else [])


def describe_bad_theme(payload: Any) -> str | None:
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


def preview_stylesheet(values: dict[str, str]) -> str:
    """QSS for the miniature app: the real stylesheet plus the roles it omits.

    ``build_stylesheet`` is the app's own sheet, so the preview shows what the
    theme will actually look like. The extra block below carries the handful of
    roles no Qt widget currently uses (the title-bar gradient, the accent
    hover/press pair, the status colours) — without it those swatches would edit
    something the user can't see.
    """
    # Normalized so a gap can't KeyError mid-sheet, and so the preview shows
    # the derived roles as they will actually be stored.
    v = normalize_palette(values)
    extra = f"""
QLabel#themePreviewTitle {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {v["bg_gradient_a"]}, stop:1 {v["bg_gradient_b"]});
    color: {v["text_highlight"]};
    padding: 6px 8px;
    font-weight: bold;
}}
QFrame#themePreviewPanel {{
    background-color: {v["bg_surface"]};
    border: 1px solid {v["border"]};
    border-radius: 4px;
}}
QFrame#themePreviewCardSel {{
    background-color: {v["bg_card_sel"]};
    border: 1px solid {v["border"]};
    border-radius: 6px;
}}
QFrame#themePreviewCardSel QLabel {{ background: transparent; }}
QLabel#themePreviewAccent {{ color: {v["accent"]}; font-weight: bold; }}
QLabel#themePreviewWarning {{ color: {v["warning"]}; }}
QLabel#themePreviewOk {{ color: {v["success"]}; }}
QLabel#themePreviewBad {{ color: {v["error"]}; }}
QLabel#themePreviewFill {{
    background-color: {v["success_dim"]};
    color: {v["text"]};
    padding: 2px 6px;
    border-radius: 3px;
}}
QPushButton#themePreviewSecondary:hover {{ background-color: {v["accent_hover"]}; color: {v["text_inverse"]}; }}
QPushButton#themePreviewSecondary:pressed {{ background-color: {v["accent_press"]}; color: {v["text_inverse"]}; }}
"""
    return build_stylesheet(values) + extra


class _PreviewPane(QFrame):
    """A miniature of the app, restyled from the working palette on every edit."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(PREVIEW_WIDTH)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        title = QLabel("AI Case Sorter", self)
        title.setObjectName("themePreviewTitle")
        column.addWidget(title)

        panel = QFrame(self)
        panel.setObjectName("themePreviewPanel")
        body = QVBoxLayout(panel)
        body.setContentsMargins(10, 8, 10, 10)
        body.setSpacing(8)
        heading = QLabel("Slots", panel)
        heading.setObjectName("themePreviewAccent")
        body.addWidget(heading)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        cards.addWidget(self._card(panel, "Slot #1", "slotCard"))
        cards.addWidget(self._card(panel, "Slot #2", "themePreviewCardSel"))
        body.addLayout(cards)

        field = QLineEdit("Confidence floor", panel)
        field.setReadOnly(True)
        body.addWidget(field)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for name, text in (("action", "Start"), ("update", "Update"), ("danger", "Stop")):
            button = QPushButton(text, panel)
            button.setObjectName(name)
            buttons.addWidget(button)
        body.addLayout(buttons)

        secondary = QHBoxLayout()
        secondary.setSpacing(6)
        plain = QPushButton("Secondary", panel)
        plain.setObjectName("themePreviewSecondary")
        secondary.addWidget(plain)
        warning = QLabel("Warning", panel)
        warning.setObjectName("themePreviewWarning")
        secondary.addWidget(warning)
        fill = QLabel("Applied", panel)
        fill.setObjectName("themePreviewFill")
        secondary.addWidget(fill)
        secondary.addStretch(1)
        body.addLayout(secondary)

        for object_name, text in (
            ("themePreviewOk", "●  connected"),
            ("themePreviewBad", "●  disconnected"),
        ):
            dot = QLabel(text, panel)
            dot.setObjectName(object_name)
            body.addWidget(dot)

        self.name_label = QLabel("", panel)
        self.name_label.setObjectName("mutedLabel")
        body.addWidget(self.name_label)
        body.addStretch(1)
        column.addWidget(panel, 1)

    def _card(self, parent: QWidget, title: str, object_name: str) -> QFrame:
        card = QFrame(parent)
        card.setObjectName(object_name)
        column = QVBoxLayout(card)
        column.setContentsMargins(8, 6, 8, 6)
        column.setSpacing(2)
        name = QLabel(title, card)
        name.setObjectName("slotTitle")
        count = QLabel("128", card)
        count.setObjectName("slotCount")
        names = QLabel("(no headstamps)", card)
        names.setObjectName("slotNames")
        for label in (name, count, names):
            column.addWidget(label)
        return card

    def apply_values(self, values: dict[str, str], theme_name: str) -> None:
        self.name_label.setText(theme_name or "Untitled")
        self.setStyleSheet(preview_stylesheet(values))


class ThemeEditorDialog(QDialog):
    """Create or edit a user theme, with a live preview of the result."""

    def __init__(self, parent: QWidget | None, win: Any) -> None:
        super().__init__(parent)
        self._win = win
        # Qt never calls ``theme.set_palette``, so ``current_theme()`` would
        # report Dark whatever the window is showing — the window's own name is
        # the authority here.
        self.base = resolve_theme(getattr(win, "theme_name", None))
        self.editing = is_custom_theme(self.base)

        # Working copy — nothing here touches the live registry until Save.
        self.values: dict[str, str] = dict(THEMES[self.base])
        self.halftone_ink: str = HALFTONE_INK.get(self.base) or self.values["border"]

        self.setWindowTitle("Edit Theme" if self.editing else "New Theme")
        self.setMinimumSize(LIST_MIN_WIDTH + PREVIEW_WIDTH + 60, 520)

        # Attributes, not methods: a test drives every path without a modal.
        self.notify: Callable[[str, str], None] = self._warn
        self.confirm: Callable[[str, str], bool] = self._ask
        self.pick_color: Callable[[str, str], str | None] = self._pick_color
        self.ask_name: Callable[[str], str | None] = self._ask_name
        self.ask_open_path: Callable[[], str | None] = self._ask_open_path
        self.ask_save_path: Callable[[str], str | None] = self._ask_save_path

        self._swatches: dict[str, QPushButton] = {}
        self._hex_edits: dict[str, QLineEdit] = {}
        # Set while a hex field is being written to from code, so the edit
        # signal doesn't bounce back as a user edit.
        self._syncing = False

        column = QVBoxLayout(self)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(8)
        self._build_header(column)
        columns = QHBoxLayout()
        columns.setSpacing(12)
        columns.addWidget(self._build_color_list(), 1)
        columns.addWidget(self._build_preview(), 0)
        column.addLayout(columns, 1)
        self._build_buttons(column)
        self.render_preview()

    # ----- construction -------------------------------------------------------

    def _build_header(self, column: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Name", self))
        self.name_edit = QLineEdit(self.base if self.editing else unique_theme_name("My theme"), self)
        self.name_edit.setMaxLength(MAX_THEME_NAME)
        self.name_edit.setMaximumWidth(200)
        self.name_edit.textChanged.connect(lambda _text: self.render_preview())
        row.addWidget(self.name_edit)

        self.outline_check = QCheckBox("Comic ink outlines", self)
        self.outline_check.setChecked(bool(INK_OUTLINE.get(self.base)))
        row.addWidget(self.outline_check)
        self.halftone_check = QCheckBox("Halftone dots", self)
        self.halftone_check.setChecked(bool(HALFTONE_INK.get(self.base)))
        row.addWidget(self.halftone_check)
        self.halftone_swatch = self._swatch(self.halftone_ink, "Halftone dot colour")
        self.halftone_swatch.clicked.connect(self.pick_halftone)
        row.addWidget(self.halftone_swatch)
        row.addStretch(1)
        column.addLayout(row)

        self.mode_hint = self._hint(
            f"Editing “{self.base}” — Save & apply writes back to it, including a rename."
            if self.editing
            else f"Starting from the built-in “{self.base}”, which can't be changed. "
            "Save & apply keeps this as a new theme."
        )
        column.addWidget(self.mode_hint)
        column.addWidget(self._hint(OPTIONS_NOTE))

    def _build_color_list(self) -> QWidget:
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setMinimumWidth(LIST_MIN_WIDTH)
        holder = QWidget(area)
        column = QVBoxLayout(holder)
        column.setContentsMargins(2, 2, 8, 2)
        column.setSpacing(4)
        for title, rows in grouped_roles():
            heading = QLabel(title, holder)
            # Bold in code rather than by objectName: the group headings are
            # local to this dialog, not a palette role the app-wide QSS knows.
            font = QFont(heading.font())
            font.setBold(True)
            heading.setFont(font)
            column.addWidget(heading)
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(2)
            for index, (role, label) in enumerate(rows):
                self._build_color_row(holder, grid, role, label, index)
            grid.setColumnStretch(1, 1)
            column.addLayout(grid)
        column.addWidget(self._hint(DERIVED_NOTE))
        column.addStretch(1)
        area.setWidget(holder)
        return area

    def _build_color_row(self, parent: QWidget, grid: QGridLayout, role: str, label: str, row: int) -> None:
        swatch = self._swatch(self.values[role], role)
        swatch.clicked.connect(lambda _checked=False, r=role: self.pick(r))
        self._swatches[role] = swatch
        grid.addWidget(swatch, row, 0)
        grid.addWidget(QLabel(label, parent), row, 1)

        edit = QLineEdit(self.values[role], parent)
        edit.setMaxLength(7)
        edit.setFixedWidth(84)
        edit.textChanged.connect(lambda _text, r=role: self._on_hex_typed(r))
        self._hex_edits[role] = edit
        grid.addWidget(edit, row, 2)

    def _swatch(self, color: str, tooltip: str) -> QPushButton:
        button = QPushButton(self)
        button.setFixedSize(30, 20)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._paint_swatch(button, color)
        return button

    @staticmethod
    def _paint_swatch(button: QPushButton, color: str) -> None:
        button.setStyleSheet(f"background-color: {color}; border: 1px solid palette(mid); border-radius: 2px;")

    def _build_preview(self) -> QWidget:
        side = QWidget(self)
        column = QVBoxLayout(side)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(QLabel("Preview", side))
        self.preview = _PreviewPane(side)
        column.addWidget(self.preview)
        column.addStretch(1)
        return side

    def _build_buttons(self, column: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        if self.editing:
            delete = QPushButton("Delete", self)
            delete.setObjectName("danger")
            delete.clicked.connect(self.delete_theme)
            row.addWidget(delete)
            self.delete_button: QPushButton | None = delete
        else:
            self.delete_button = None
        for text, slot in (("Import…", self.import_theme), ("Export…", self.export_theme)):
            button = QPushButton(text, self)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)

        create = QPushButton("Create new…", self)
        create.clicked.connect(self.create_new)
        row.addWidget(create)
        save = QPushButton("Save & apply", self)
        save.setObjectName("action")
        save.clicked.connect(self.save)
        row.addWidget(save)
        # "Close", not "Cancel": Save & apply keeps the dialog open (Seth) and
        # applies immediately, so by the time this is clicked there may be
        # nothing left to cancel.
        cancel = QPushButton("Close", self)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        column.addLayout(row)

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("dialogHint")
        label.setWordWrap(True)
        return label

    # ----- editing ------------------------------------------------------------

    def pick(self, role: str) -> None:
        chosen = self.pick_color(f"Colour — {role}", self.values[role])
        if chosen:
            self.set_color(role, chosen)

    def pick_halftone(self) -> None:
        chosen = self.pick_color("Colour — halftone dots", self.halftone_ink)
        if not chosen:
            return
        self.halftone_ink = chosen.lower()
        self._paint_swatch(self.halftone_swatch, self.halftone_ink)
        self.halftone_check.setChecked(True)

    def set_color(self, role: str, color: str) -> None:
        """Set one role, keeping the derived roles in step with it.

        ``normalize_palette`` owns that invariant at the boundary; applying it
        here as well is what keeps the preview honest mid-edit.
        """
        color = color.lower()
        self.values[role] = color
        for derived, source in DERIVED_ROLES.items():
            self.values[derived] = self.values[source]
        self._paint_swatch(self._swatches[role], color)
        edit = self._hex_edits[role]
        if edit.text().lower() != color:
            self._syncing = True
            edit.setText(color)
            self._syncing = False
        self.render_preview()

    def _on_hex_typed(self, role: str) -> None:
        """Accept a hand-typed colour once it's complete, ignore it until then."""
        if self._syncing:
            return
        text = self._hex_edits[role].text().strip()
        if not text.startswith("#"):
            text = "#" + text
        if len(text) != 7:
            return
        try:
            int(text[1:], 16)
        except ValueError:
            return
        if text.lower() != self.values[role]:
            self.set_color(role, text)

    def render_preview(self) -> None:
        # The colour rows are built before the preview is; a hex field typed
        # into during construction would otherwise reach a missing widget.
        if not hasattr(self, "preview"):
            return
        self.preview.apply_values(self.values, self.name_edit.text().strip())

    # ----- save / delete ------------------------------------------------------

    def payload(self, name: str | None = None) -> dict:
        return {
            "version": CUSTOM_THEME_VERSION,
            "name": (name if name is not None else self.name_edit.text()).strip(),
            "based_on": self.base,
            "halftone": self.halftone_ink if self.halftone_check.isChecked() else None,
            "outline": 2 if self.outline_check.isChecked() else 0,
            "palette": normalize_palette(self.values),
        }

    def save(self) -> None:
        """Write the edits back to the theme being edited, or create it.

        On a built-in there is nothing to write back to, so this creates a theme
        under the name in the box (that's what the box is prefilled with). On a
        saved theme it updates it in place — a rename included, which keeps the
        theme rather than leaving the old name behind.
        """
        name = self.name_edit.text().strip()
        if not self._name_is_usable(name):
            return
        # A saved theme keeps the origin it already records: writing back to it
        # (a rename above all) must not restate it as having been made from
        # itself, which is what the delete fallback would then land on.
        origin = custom_theme_payload(self.base).get("based_on") if self.editing else self.base
        if self.editing and name != self.base:
            try:
                rename_custom_theme(self.base, name)
            except ValueError as exc:
                self.notify("Can't rename", str(exc))
                return
            self.base = name
        elif not self.editing and name in THEMES and not self._confirm_replace(name):
            return
        payload = self.payload(name)
        payload["based_on"] = origin or self.base
        self._register_and_apply(payload)
        # Stay open (Seth): iterating on a theme is the editor's whole point,
        # and Save & apply is the iteration step. A first save of a
        # built-in-based theme turns the session into an edit of the new one.
        self.editing = True
        self.base = name
        self.setWindowTitle("Edit Theme")
        self.mode_hint.setText(f"Saved — editing “{self.base}”. Save & apply writes back to it, including a rename.")

    def create_new(self) -> None:
        """Save these colours as a separate theme, under a name of its own."""
        asked = self.ask_name(unique_theme_name(self.name_edit.text() or self.base))
        if asked is None:
            return
        name = asked.strip()
        if not self._name_is_usable(name):
            return
        if name in THEMES and not self._confirm_replace(name):
            return
        self._register_and_apply(self.payload(name))
        self.accept()

    def _confirm_replace(self, name: str) -> bool:
        return self.confirm("Replace theme", f"Replace the saved theme “{name}”?")

    def _name_is_usable(self, name: str) -> bool:
        """Complain (and return False) if `name` can't be a theme."""
        if not name:
            self.notify("Name needed", "Give the theme a name.")
            return False
        if len(name) > MAX_THEME_NAME:
            self.notify("Name too long", f"Theme names are at most {MAX_THEME_NAME} characters.")
            return False
        if name in BUILTIN_THEMES:
            self.notify("Built-in theme", f"“{name}” is a built-in theme. Pick a different name.")
            return False
        return True

    def _register_and_apply(self, payload: dict) -> None:
        register_custom_theme(
            payload["name"],
            payload["palette"],
            halftone=payload.get("halftone"),
            outline=payload.get("outline", 0),
            base=payload.get("based_on"),
        )
        self._persist()
        self._win.set_theme(payload["name"])
        self._refresh_picker()

    def delete_theme(self) -> None:
        if self.confirm("Delete theme", f"Delete the theme “{self.base}”?"):
            self.delete_confirmed()

    def delete_confirmed(self) -> None:
        # Fall back to whatever this theme was made from; that may itself have
        # been deleted since, in which case `set_theme` resolves to the default.
        fallback = getattr(self._win, "theme_name", "")
        if fallback == self.base:
            fallback = custom_theme_payload(self.base).get("based_on") or ""
        unregister_custom_theme(self.base)
        self._persist()
        self._win.set_theme(fallback)
        self._refresh_picker()
        self.accept()

    def _persist(self) -> None:
        """Store every custom theme to the settings row."""
        saver = getattr(self._win, "save_custom_themes", None)
        if callable(saver):
            saver()
            return
        save_setting = getattr(self._win, "_save_setting", None)
        if callable(save_setting):
            save_setting(SETTING_CUSTOM_THEMES, custom_themes_payload())

    def _refresh_picker(self) -> None:
        """Re-read the theme list into Settings → Theme's combo."""
        refresh = getattr(self._win, "refresh_theme_picker", None)
        if callable(refresh):
            refresh()
            return
        combo = getattr(self._win, "theme_combo", None)
        if combo is None:
            return
        # Repopulating fires currentTextChanged, which is wired to set_theme.
        blocked = combo.blockSignals(True)
        combo.clear()
        combo.addItems(theme_names())
        combo.setCurrentText(getattr(self._win, "theme_name", "") or "")
        combo.blockSignals(blocked)

    # ----- files --------------------------------------------------------------

    def export_theme(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.notify("Name needed", "Give the theme a name before exporting.")
            return
        path = self.ask_save_path(f"{name}.json")
        if not path:
            return
        try:
            self.write_theme_file(Path(path))
        except OSError as exc:
            self.notify("Export failed", str(exc))
            return
        self.notify("Theme exported", f"Saved to {path}")

    def write_theme_file(self, path: Path) -> dict:
        """Write the theme as it stands in the editor — unsaved edits included."""
        payload = self.payload()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def import_theme(self) -> None:
        path = self.ask_open_path()
        if not path:
            return
        try:
            self.read_theme_file(Path(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.notify("Import failed", str(exc))
            return
        self.accept()

    def read_theme_file(self, path: Path) -> None:
        """Register and apply the theme in `path`; raises ValueError if it isn't one.

        An imported theme never replaces an existing one — it comes in under a
        free name, so importing the file you exported leaves you with both.
        """
        payload = json.loads(path.read_text(encoding="utf-8"))
        problem = describe_bad_theme(payload)
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

    # ----- dialog hooks -------------------------------------------------------

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _pick_color(self, title: str, initial: str) -> str | None:
        chosen = QColorDialog.getColor(QColor(initial), self, title)
        return chosen.name() if chosen.isValid() else None

    def _ask_name(self, initial: str) -> str | None:
        text, ok = QInputDialog.getText(
            self,
            "New Theme",
            f"Name for the new theme (up to {MAX_THEME_NAME} characters)",
            QLineEdit.EchoMode.Normal,
            initial,
        )
        return text if ok else None

    def _ask_open_path(self) -> str | None:
        path, _filter = QFileDialog.getOpenFileName(self, "Import theme", "", f"{FILE_FILTER};;All files (*)")
        return path or None

    def _ask_save_path(self, default_name: str) -> str | None:
        path, _filter = QFileDialog.getSaveFileName(self, "Export theme", default_name, FILE_FILTER)
        return path or None
