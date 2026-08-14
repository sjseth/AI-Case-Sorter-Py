"""Per-model training hyperparameters.

The dialog edits the ``TrainingConfig`` instance it was handed, in place, and
calls ``on_saved`` with it, so the caller only has to persist the model row.

Every value here reaches ``train_convnext.py``'s argv, the three SWA tuning
knobs (``swa_acc_threshold``, ``swa_patience``, ``swa_min_epoch``) included.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..data.models import TrainingConfig

SWA_MODES = ("scheduled", "adaptive")

# (key, label, minimum, maximum, step, decimals) — decimals 0 means an int field.
NUMERIC_FIELDS: tuple[tuple[str, str, float, float, float, int], ...] = (
    ("epochs", "Epochs", 1, 200, 1, 0),
    ("batch_size", "Batch size", 1, 1024, 1, 0),
    ("learning_rate", "Learning rate", 1e-6, 1.0, 1e-5, 6),
    ("weight_decay", "Weight decay", 0.0, 1.0, 1e-5, 6),
    ("dropout_rate", "Dropout", 0.0, 1.0, 0.05, 2),
    ("val_split", "Validation split", 0.0, 0.99, 0.05, 2),
    ("image_size", "Image size", 64, 512, 1, 0),
    ("max_workers", "Max workers (-1 = auto)", -1, 64, 1, 0),
    (
        "stochastic_depth_prob",
        "Stochastic depth prob (-1 = default)",
        -1.0,
        0.5,
        0.05,
        2,
    ),
)

FOCAL_FIELDS: tuple[tuple[str, str, float, float, float, int], ...] = (
    ("focal_gamma", "Focal gamma", 0.1, 10.0, 0.1, 1),
)

SWA_FIELDS: tuple[tuple[str, str, float, float, float, int], ...] = (
    ("swa_start", "SWA start (fraction of epochs)", 0.0, 1.0, 0.05, 2),
    ("swa_acc_threshold", "SWA accuracy threshold", 0.0, 1.0, 0.01, 2),
    ("swa_patience", "SWA patience (epochs)", 1, 100, 1, 0),
    ("swa_min_epoch", "SWA minimum epoch", 0, 200, 1, 0),
)

BOOL_FIELDS: tuple[tuple[str, str], ...] = (
    ("train_all", "Train on full dataset (override val split)"),
    ("freeze_backbone", "Freeze backbone (only train classifier)"),
)

_COERCE: dict[str, Any] = {
    key: (int if decimals == 0 else float)
    for key, _label, _lo, _hi, _step, decimals in (*NUMERIC_FIELDS, *FOCAL_FIELDS, *SWA_FIELDS)
}
_COERCE.update({key: bool for key, _label in BOOL_FIELDS})
_COERCE.update({"use_focal_loss": bool, "use_swa": bool, "swa_mode": str})


class TrainingConfigDialog(QDialog):
    """Edits a ``TrainingConfig`` in place; ``saved`` is True once accepted."""

    def __init__(
        self,
        config: TrainingConfig,
        *,
        on_saved: Callable[[TrainingConfig], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._on_saved = on_saved
        self.saved = False
        # key -> the widget holding it; save() reads the whole dict back.
        self.fields: dict[str, Any] = {}

        self.setWindowTitle("Training settings")
        self.setMinimumWidth(420)
        column = QVBoxLayout(self)

        form = QFormLayout()
        for spec in NUMERIC_FIELDS:
            self._add_numeric(form, spec)
        column.addLayout(form)

        for key, label in BOOL_FIELDS:
            check = QCheckBox(label, self)
            check.setChecked(bool(getattr(config, key)))
            self.fields[key] = check
            column.addWidget(check)

        column.addWidget(self._build_focal_group())
        column.addWidget(self._build_swa_group())

        buttons = QDialogButtonBox(self)
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("action")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self.save)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    # ----- construction -------------------------------------------------------

    def _add_numeric(self, form: QFormLayout, spec: tuple[str, str, float, float, float, int]) -> None:
        key, label, minimum, maximum, step, decimals = spec
        value = getattr(self.config, key)
        widget: Any
        if decimals == 0:
            widget = QSpinBox(self)
            widget.setRange(int(minimum), int(maximum))
            widget.setSingleStep(int(step))
            widget.setValue(int(value))
        else:
            widget = QDoubleSpinBox(self)
            widget.setDecimals(decimals)
            widget.setRange(float(minimum), float(maximum))
            widget.setSingleStep(float(step))
            widget.setValue(float(value))
        self.fields[key] = widget
        form.addRow(label, widget)

    def _build_focal_group(self) -> QGroupBox:
        box = QGroupBox("Focal loss", self)
        column = QVBoxLayout(box)
        check = QCheckBox("Use focal loss", box)
        check.setChecked(bool(self.config.use_focal_loss))
        self.fields["use_focal_loss"] = check
        column.addWidget(check)
        form = QFormLayout()
        for spec in FOCAL_FIELDS:
            self._add_numeric(form, spec)
        column.addLayout(form)
        return box

    def _build_swa_group(self) -> QGroupBox:
        box = QGroupBox("Stochastic weight averaging (SWA)", self)
        column = QVBoxLayout(box)
        check = QCheckBox("Use stochastic weight averaging", box)
        check.setChecked(bool(self.config.use_swa))
        self.fields["use_swa"] = check
        column.addWidget(check)

        form = QFormLayout()
        for spec in SWA_FIELDS:
            self._add_numeric(form, spec)
        mode = QComboBox(box)
        mode.addItems(list(SWA_MODES))
        if self.config.swa_mode in SWA_MODES:
            mode.setCurrentText(self.config.swa_mode)
        self.fields["swa_mode"] = mode
        form.addRow("SWA mode", mode)
        column.addLayout(form)
        return box

    # ----- values -------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        """What ``save`` would write, read straight off the widgets."""
        out: dict[str, Any] = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QCheckBox):
                out[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentText()
            else:
                out[key] = widget.value()
        return out

    def save(self) -> None:
        """Write the widgets onto the config object and close.

        Every widget clamps its own input, so there is nothing left to reject
        here. Types come from the field spec, not from what the attribute happens to
        hold: an int sitting in a float field must not turn the widget's value
        back into an int on the way out.
        """
        for key, value in self.values().items():
            setattr(self.config, key, _COERCE.get(key, lambda v: v)(value))
        self.saved = True
        if self._on_saved is not None:
            self._on_saved(self.config)
        self.accept()


def build_training_config_dialog(
    config: TrainingConfig,
    *,
    on_saved: Callable[[TrainingConfig], None] | None = None,
    parent: QWidget | None = None,
) -> TrainingConfigDialog:
    return TrainingConfigDialog(config, on_saved=on_saved, parent=parent)
