"""New / Edit dialogs for the Sort dashboard's sorting templates.

Both are thin shells over ``Config``'s template API — creating, renaming and
deleting a layout, plus the two rules the API enforces and the UI has to
report: names are unique per scope, and the last template in a scope can't be
deleted.

``notify`` and ``confirm`` are instance attributes rather than direct
``QMessageBox`` calls so a test can drive Create/Save/Delete without a modal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


def _mode_noun(mode: str) -> str:
    return "package-mode " if mode == "package" else ""


class _TemplateDialog(QDialog):
    """Shared chrome: a name field and the two message hooks."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(340)
        self.column = QVBoxLayout(self)
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("Template name")
        self.column.addWidget(QLabel("Template name", self))
        self.column.addWidget(self.name_edit)
        # Attributes, not methods: a test replaces them so nothing modal opens.
        self.notify: Callable[[str, str], None] = self._warn
        self.confirm: Callable[[str, str], bool] = self._ask

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _hint(self, text: str) -> None:
        label = QLabel(text, self)
        label.setObjectName("dialogHint")
        label.setWordWrap(True)
        self.column.addWidget(label)


class NewTemplateDialog(_TemplateDialog):
    """Create a sorting template, optionally seeded from the current layout."""

    def __init__(
        self,
        config: Any,
        mode: str,
        current_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("New Sorting Template", parent)
        self.config = config
        self.mode = mode
        self.created: Any | None = None

        self.copy_radio = QRadioButton(f"Copy the slot assignments from “{current_name}”", self)
        self.copy_radio.setChecked(True)
        self.blank_radio = QRadioButton("Start fresh with no slot assignments", self)
        self.column.addWidget(self.copy_radio)
        self.column.addWidget(self.blank_radio)
        self._hint(
            f"Saved as a {_mode_noun(mode)}template for the active model. "
            "Switching templates swaps the whole slot layout."
        )

        buttons = QDialogButtonBox(self)
        create = buttons.addButton("Create", QDialogButtonBox.ButtonRole.AcceptRole)
        create.setObjectName("action")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        create.clicked.connect(self.create_template)
        buttons.rejected.connect(self.reject)
        self.column.addWidget(buttons)
        self.name_edit.returnPressed.connect(self.create_template)

    def create_template(self) -> None:
        try:
            self.created = self.config.create_slot_template(
                self.name_edit.text(),
                copy_current=self.copy_radio.isChecked(),
                mode=self.mode,
            )
        except ValueError as exc:
            self.notify("Can't create template", str(exc))
            return
        self.accept()


class EditTemplateDialog(_TemplateDialog):
    """Rename or delete an existing sorting template."""

    def __init__(
        self,
        config: Any,
        template: Any,
        *,
        can_delete: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Edit Sorting Template", parent)
        self.config = config
        self.template = template
        # The template left active once the dialog closes (a delete loads
        # another one), or None while nothing has been done.
        self.active: Any | None = None

        self.name_edit.setText(template.name)
        self.name_edit.selectAll()
        self._hint(
            f"A {_mode_noun(template.mode)}template for the active model. "
            + (
                "Deleting one drops its slot layout — the headstamps themselves are untouched."
                if can_delete
                else "This is the only one, so it can't be deleted."
            )
        )

        buttons = QDialogButtonBox(self)
        if can_delete:
            delete = buttons.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
            delete.setObjectName("danger")
            delete.clicked.connect(self.delete_template)
            self.delete_button: QPushButton | None = delete
        else:
            self.delete_button = None
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("action")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.rename_template)
        buttons.rejected.connect(self.reject)
        self.column.addWidget(buttons)
        self.name_edit.returnPressed.connect(self.rename_template)

    def rename_template(self) -> None:
        try:
            self.config.rename_slot_template(self.template.id, self.name_edit.text())
        except ValueError as exc:
            self.notify("Can't rename template", str(exc))
            return
        self.active = self.template
        self.accept()

    def delete_template(self) -> None:
        if not self.confirm(
            "Delete template",
            f"Delete the sorting template “{self.template.name}”?\n\n"
            "Its slot assignments are lost; the headstamps themselves are untouched.",
        ):
            return
        try:
            self.active = self.config.delete_slot_template(self.template.id)
        except ValueError as exc:
            self.notify("Can't delete template", str(exc))
            return
        self.accept()
