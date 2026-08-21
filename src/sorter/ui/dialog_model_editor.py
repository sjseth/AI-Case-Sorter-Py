"""Create / edit a model.

Name, cartridge, model type (the ConvNeXt variant) and the two primer-mask
settings, persisted through ``ModelRepo.create``/``update``. Training
hyperparameters live in their own dialog (increment 6).

The **Community feedback loop** box is built only for a model that is community-linked — a UID stamped on it, or an install
marked ``CommunityManaged`` — which by construction means an *existing* model.
The confidence floor is the publisher's and is shown read-only; the user owns
the opt-in and the upload mode.

``notify`` is an instance attribute so a test can drive ``save()`` without a
modal opening.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..data.db import Database
from ..data.models import (
    FEEDBACK_UPLOAD_MODES,
    MODEL_MODES,
    AIModelConfig,
    ImageProcessingConfig,
    Model,
    TrainingConfig,
)
from ..data.repository import CartridgeRepo, ModelRepo

# Persisted value <-> label shown in the combo box.
FEEDBACK_MODE_LABELS = {
    "Instant": "Instant",
    "OnRunComplete": "On run complete",
    "Manual": "Manual",
}
_FEEDBACK_MODE_BY_LABEL = {label: value for value, label in FEEDBACK_MODE_LABELS.items()}

DEFAULT_PRIMER_SIZE = 135


def is_community_model(model: Model | None) -> bool:
    """Community-*linked*, which is not the same as foreign (see CLAUDE.md §5).

    Sharing your own model stamps a UID onto your copy, so this is true for
    models you still own — which is right here: the feedback-loop box is about
    a model that exists in the community, not about who may train it.
    """
    return bool(model is not None and (model.community_model_uid or model.model_type == "CommunityManaged"))


class ModelEditorDialog(QDialog):
    """``saved_id`` holds the model id once ``save()`` succeeds."""

    def __init__(
        self,
        db: Database,
        *,
        existing: Model | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.existing = existing
        self.saved_id: int | None = None
        self.cartridge_repo = CartridgeRepo(db)
        self.model_repo = ModelRepo(db)
        # Attribute, not method: a test replaces it so nothing modal opens.
        self.notify: Callable[[str, str], None] = self._warn

        self.setWindowTitle("Edit Model" if existing else "New Model")
        self.setMinimumWidth(400)
        column = QVBoxLayout(self)

        form = QFormLayout()
        self.name_edit = QLineEdit(existing.name if existing else "", self)
        form.addRow("Name", self.name_edit)

        self.cartridge_combo = QComboBox(self)
        self._cartridges = self.cartridge_repo.list()
        for cartridge in self._cartridges:
            self.cartridge_combo.addItem(cartridge.name, cartridge.id)
        if existing is not None:
            index = self.cartridge_combo.findData(existing.cartridge_id)
            if index >= 0:
                self.cartridge_combo.setCurrentIndex(index)
        form.addRow("Cartridge", self.cartridge_combo)

        self.mode_combo = QComboBox(self)
        # MODEL_MODES, not SUPPORTED_MODEL_MODES: "openai" is a legal mode —
        # a model that classifies over an HTTP server (its settings live on
        # the AI Config page while it is active) — it just isn't trainable.
        self.mode_combo.addItems(list(MODEL_MODES))
        if existing is not None and existing.model_mode in MODEL_MODES:
            self.mode_combo.setCurrentText(existing.model_mode)
        form.addRow("Model type", self.mode_combo)

        self.primer_spin = QSpinBox(self)
        self.primer_spin.setRange(0, 512)
        self.primer_spin.setValue(int(existing.primer_mask_size) if existing else DEFAULT_PRIMER_SIZE)
        form.addRow("Primer mask size (px)", self.primer_spin)
        column.addLayout(form)

        self.hide_primer_check = QCheckBox("Hide primer in cropped image", self)
        self.hide_primer_check.setChecked(bool(existing.hide_primer) if existing else True)
        column.addWidget(self.hide_primer_check)

        self.is_community = is_community_model(existing)
        # `is_community` implies `existing is not None`; the re-check narrows
        # the type as well, rather than asserting it.
        if self.is_community and existing is not None:
            column.addWidget(self._build_feedback_group(existing))

        buttons = QDialogButtonBox(self)
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("action")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.save)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)
        self.name_edit.returnPressed.connect(self.save)

    # ----- feedback loop ------------------------------------------------------

    def _build_feedback_group(self, model: Model) -> QGroupBox:
        box = QGroupBox("Community feedback loop", self)
        column = QVBoxLayout(box)

        self.fb_enabled_check = QCheckBox("Participate in feedback loop", box)
        self.fb_enabled_check.setChecked(bool(model.feedback_loop_enabled))
        self.fb_enabled_check.toggled.connect(self._on_feedback_toggled)
        column.addWidget(self.fb_enabled_check)

        form = QFormLayout()
        self.fb_mode_combo = QComboBox(box)
        self.fb_mode_combo.addItems([FEEDBACK_MODE_LABELS[m] for m in FEEDBACK_UPLOAD_MODES])
        self.fb_mode_combo.setCurrentText(
            FEEDBACK_MODE_LABELS.get(model.feedback_loop_upload_mode, FEEDBACK_MODE_LABELS["Instant"])
        )
        form.addRow("Upload mode", self.fb_mode_combo)

        floor = QLabel(f"{int(model.feedback_loop_confidence_floor)}%  (set by publisher)", box)
        floor.setObjectName("rowHint")
        form.addRow("Confidence floor", floor)
        column.addLayout(form)

        self._on_feedback_toggled(self.fb_enabled_check.isChecked())
        return box

    def _on_feedback_toggled(self, enabled: bool) -> None:
        self.fb_mode_combo.setEnabled(bool(enabled))

    # ----- persistence --------------------------------------------------------

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.notify("Missing name", "Please enter a model name.")
            return
        cartridge_id = self.cartridge_combo.currentData()
        if cartridge_id is None:
            self.notify("Missing cartridge", "Pick a cartridge first.")
            return
        mode = self.mode_combo.currentText()
        if mode not in MODEL_MODES:
            self.notify("Invalid model type", f"Unknown model: {mode}")
            return

        try:
            self.saved_id = self._persist(name, int(cartridge_id), mode)
        except Exception as exc:
            self.notify("Save failed", str(exc))
            return
        self.accept()

    def _persist(self, name: str, cartridge_id: int, mode: str) -> int:
        if self.existing is None:
            model = Model(
                name=name,
                cartridge_id=cartridge_id,
                model_mode=mode,
                hide_primer=self.hide_primer_check.isChecked(),
                primer_mask_size=int(self.primer_spin.value()),
                image_processing=ImageProcessingConfig(),
                training_config=TrainingConfig(model_name=mode),
                ai_model_config=AIModelConfig(),
            )
            with self.db.transaction():
                return self.model_repo.create(model).id

        existing = self.existing
        existing.name = name
        existing.cartridge_id = cartridge_id
        existing.model_mode = mode
        existing.hide_primer = self.hide_primer_check.isChecked()
        existing.primer_mask_size = int(self.primer_spin.value())
        existing.training_config.model_name = mode
        if self.is_community:
            # Editable: the opt-in and the upload mode. Never the floor.
            existing.feedback_loop_enabled = self.fb_enabled_check.isChecked()
            existing.feedback_loop_upload_mode = _FEEDBACK_MODE_BY_LABEL.get(
                self.fb_mode_combo.currentText(),
                existing.feedback_loop_upload_mode,
            )
        with self.db.transaction():
            self.model_repo.update(existing)
        return existing.id


def build_model_editor(db: Database, *, existing: Any = None, parent: QWidget | None = None) -> ModelEditorDialog:
    return ModelEditorDialog(db, existing=existing, parent=parent)
