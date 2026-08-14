"""AI Config activity — server settings, headstamp manager, single-shot test.

Request reference: ``sorter/ml/api_client.py``.

Two things worth knowing, both noted where they happen:

* The test shot captures straight off the camera instead of feeding a case
  through the board — a config surface should be testable without hardware.
* Rename is assembled from ``remove`` + ``add`` — ``Config`` has no rename API
  and AI Config-mode headstamps carry nothing but a name and a slot.

Unlike the settings pages, this one saves on the **Save** button, not on edit:
a half-typed endpoint must not become the live one.

The page is a two-card stack, exactly as Train's is: the form when this is the
backend that classifies (no active model), or — when a local model is doing it
instead — a panel naming that model and offering the jump to the Models page.
The sidebar entry is never hidden (app.py's ``_apply_mode_visibility``), so
this page is what has to answer a click on the muted half of the pair.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..data.repository import ModelRepo
from ..hardware.image_proc import apply_primer_mask, crop_headstamp
from ..ml import api_client

CROP_SIZE = 200
NO_RESULT = "—"
# The explainer a muted AI Config entry opens onto. The sidebar keeps the entry
# at all times, so this is what a click on it has to answer: what is
# classifying instead, and how to change that.
UNAVAILABLE_TITLE = "A local model is doing the classifying"
LOCAL_MODEL_NOTICE = (
    "Classification currently uses the local model “{name}”. To classify over HTTP "
    "instead, select “Use AI Config” on the Models page."
)
LOCAL_MODEL_NOTICE_UNNAMED = (
    "A local model is active, so cases are classified on it and these server settings "
    "are unused. Select “Use AI Config” on the Models page to edit them."
)
MODELS_JUMP_TEXT = "Go to Models"
NAME_HINT = "Add creates a headstamp; Rename applies the typed name to the selected one."


def ai_config_mode(win: Any) -> bool:
    """AI Config mode = no active local model. Only then do these settings apply."""
    return win.config.settings.get_active_model_id() is None


def active_model_name(win: Any) -> str | None:
    """Read fresh — the explainer names whatever is classifying right now."""
    model_id = win.config.settings.get_active_model_id()
    if model_id is None:
        return None
    model = ModelRepo(win.config.db).get(model_id)
    return None if model is None else str(model.name)


class AiSection(QWidget):
    """Server config + headstamp manager + one test shot. State lives on ``self``."""

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        # Guards the slot spin box while it is being set to follow the selection.
        self._syncing = False
        # Attribute, not method, so tests can replace it (ty rejects
        # assigning over a method) — same pattern as the dialogs' notify.
        self.confirm: Callable[[str, str], bool] = self._ask

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        top = QHBoxLayout()
        self.server_group = self._build_server_group()
        self.headstamp_group = self._build_headstamp_group()
        top.addWidget(self.server_group, 1)
        top.addWidget(self.headstamp_group, 1)
        column.addLayout(top)

        self.test_group = self._build_test_group()
        column.addWidget(self.test_group)
        column.addStretch(1)

        self.refresh_list()

    # ----- server settings ----------------------------------------------------

    def _build_server_group(self) -> QGroupBox:
        box = QGroupBox("Server", self)
        column = QVBoxLayout(box)
        api = self._win.config.api

        form = QFormLayout()
        self.endpoint_edit = QLineEdit(str(api.get("endpoint_url", "")), box)
        self.model_edit = QLineEdit(str(api.get("model", "")), box)

        self.key_edit = QLineEdit(str(api.get("api_key", "")), box)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_toggle = QPushButton("Show", box)
        self.key_toggle.setCheckable(True)
        self.key_toggle.setMaximumWidth(70)
        self.key_toggle.toggled.connect(self._on_key_visibility_toggled)
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.key_toggle)
        key_host = QWidget(box)
        key_host.setLayout(key_row)

        form.addRow("Endpoint URL", self.endpoint_edit)
        form.addRow("API key", key_host)
        form.addRow("Model", self.model_edit)
        column.addLayout(form)

        column.addWidget(QLabel("Prompt (use {{headstamps}} to inject the list)", box))
        self.prompt_edit = QPlainTextEdit(str(api.get("prompt", "")), box)
        self.prompt_edit.setFixedHeight(80)
        column.addWidget(self.prompt_edit)

        encoding = QFormLayout()
        self.quality_spin = QSpinBox(box)
        self.quality_spin.setRange(10, 100)
        self.quality_spin.setValue(int(api.get("image_quality", 100)))
        self.scale_spin = QSpinBox(box)
        self.scale_spin.setRange(10, 200)
        self.scale_spin.setValue(int(api.get("image_scale", 100)))
        encoding.addRow("JPEG quality", self.quality_spin)
        encoding.addRow("Scale %", self.scale_spin)
        column.addLayout(encoding)

        row = QHBoxLayout()
        self.save_button = QPushButton("Save", box)
        self.save_button.setObjectName("action")
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.save_button)
        row.addStretch(1)
        column.addLayout(row)
        return box

    def _on_key_visibility_toggled(self, shown: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.key_toggle.setText("Hide" if shown else "Show")

    def _field_values(self) -> dict[str, Any]:
        """What the widgets currently hold, in ``config.api``'s shape."""
        return {
            "endpoint_url": self.endpoint_edit.text().strip(),
            "api_key": self.key_edit.text(),
            "model": self.model_edit.text().strip(),
            "prompt": self.prompt_edit.toPlainText().rstrip("\n"),
            "image_quality": int(self.quality_spin.value()),
            "image_scale": int(self.scale_spin.value()),
        }

    def save(self) -> None:
        self._win.config.api.update(self._field_values())
        self._win.config.save()
        self._win.set_status("AI settings saved.")

    # ----- headstamps ---------------------------------------------------------

    def _build_headstamp_group(self) -> QGroupBox:
        box = QGroupBox("Headstamps", self)
        column = QVBoxLayout(box)

        name_row = QHBoxLayout()
        self.name_edit = QLineEdit(box)
        self.name_edit.setPlaceholderText("Headstamp name")
        self.name_edit.returnPressed.connect(self.add_headstamp)
        self.add_button = QPushButton("Add", box)
        self.add_button.clicked.connect(self.add_headstamp)
        self.rename_button = QPushButton("Rename", box)
        self.rename_button.clicked.connect(self.rename_selected)
        name_row.addWidget(self.name_edit, 1)
        name_row.addWidget(self.add_button)
        name_row.addWidget(self.rename_button)
        column.addLayout(name_row)

        hint = QLabel(NAME_HINT, box)
        hint.setObjectName("rowHint")
        hint.setWordWrap(True)
        column.addWidget(hint)

        self.list_widget = QListWidget(box)
        self.list_widget.currentRowChanged.connect(lambda _row: self._on_selection_changed())
        column.addWidget(self.list_widget, 1)

        slot_row = QHBoxLayout()
        slot_row.addWidget(QLabel("Slot", box))
        self.slot_spin = QSpinBox(box)
        self.slot_spin.setRange(0, int(self._win.config.serial.get("slot_quantity", 8)))
        self.slot_spin.valueChanged.connect(self._on_slot_changed)
        slot_row.addWidget(self.slot_spin)
        self.remove_button = QPushButton("Remove", box)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button = QPushButton("Clear all", box)
        self.clear_button.clicked.connect(self.clear_all)
        self.load_button = QPushButton("Load from server", box)
        self.load_button.clicked.connect(self.load_from_server)
        for button in (self.remove_button, self.clear_button, self.load_button):
            slot_row.addWidget(button)
        slot_row.addStretch(1)
        column.addLayout(slot_row)

        self.count_label = QLabel("0 headstamps", box)
        self.count_label.setObjectName("rowHint")
        column.addWidget(self.count_label)
        return box

    def _entries(self) -> list[dict[str, Any]]:
        """Headstamps for the active context, read fresh and name-sorted."""
        entries = [e for e in self._win.config.headstamps if e.get("name")]
        return sorted(entries, key=lambda e: str(e["name"]).casefold())

    def refresh_list(self) -> None:
        selected = self.selected_name()
        self.list_widget.clear()
        for entry in self._entries():
            name = str(entry["name"])
            slot = int(entry.get("slot", 0))
            item = QListWidgetItem(f"{name}   → slot {slot}" if slot else name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)
            if name == selected:
                self.list_widget.setCurrentItem(item)
        count = self.list_widget.count()
        self.count_label.setText(f"{count} headstamp" + ("" if count == 1 else "s"))
        self._on_selection_changed()

    def selected_name(self) -> str | None:
        item = self.list_widget.currentItem()
        return None if item is None else str(item.data(Qt.ItemDataRole.UserRole))

    def _slot_of(self, name: str) -> int:
        return next((int(e.get("slot", 0)) for e in self._entries() if e["name"] == name), 0)

    def _on_selection_changed(self) -> None:
        name = self.selected_name()
        for button in (self.remove_button, self.rename_button):
            button.setEnabled(name is not None)
        self.slot_spin.setEnabled(name is not None)
        self._syncing = True
        self.slot_spin.setValue(self._slot_of(name) if name else 0)
        self._syncing = False

    def _on_slot_changed(self, value: int) -> None:
        name = self.selected_name()
        if self._syncing or name is None:
            return
        self._win.config.set_headstamp_slot(name, int(value))
        self._announce_assignments()
        self.refresh_list()
        self._win.set_status(f"'{name}' → slot {value}." if value else f"'{name}' unassigned.")

    def _announce_assignments(self) -> None:
        """Nudge the Sort grid — it re-reads assignments off this topic."""
        self._win.bus.post("run/assignment_changed", {"source": "ai_config"})

    def add_headstamp(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            return
        if not self._win.config.add_headstamp(name):
            self._win.set_status(f"Could not add '{name}'.")
            return
        self.name_edit.clear()
        self._announce_assignments()
        self.refresh_list()
        self._win.set_status(f"Added '{name}'.")

    def rename_selected(self) -> None:
        """remove + add, keeping the slot — ``Config`` has no rename."""
        old = self.selected_name()
        new = self.name_edit.text().strip()
        if old is None or not new or new == old:
            return
        if any(e["name"] == new for e in self._entries()):
            self._win.set_status(f"'{new}' already exists.")
            return
        slot = self._slot_of(old)
        if not self._win.config.remove_headstamp(old):
            self._win.set_status(f"Could not rename '{old}'.")
            return
        self._win.config.add_headstamp(new, slot)
        self.name_edit.clear()
        self._announce_assignments()
        self.refresh_list()
        self._win.set_status(f"Renamed '{old}' to '{new}'.")

    def remove_selected(self) -> None:
        name = self.selected_name()
        if name is None:
            return
        self._win.config.remove_headstamp(name)
        self._announce_assignments()
        self.refresh_list()
        self._win.set_status(f"Removed '{name}'.")

    def _ask(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self, title, text)
        return answer == QMessageBox.StandardButton.Yes

    def clear_all(self) -> None:
        # Confirmed: one click deletes every headstamp.
        if not self._entries():
            return
        if not self.confirm("Clear all headstamps", "Remove every headstamp from the list?"):
            return
        self._win.config.clear_headstamps()
        self._announce_assignments()
        self.refresh_list()
        self._win.set_status("Cleared all headstamps.")

    def load_from_server(self) -> None:
        values = self._field_values()
        endpoint, model = values["endpoint_url"], values["model"]
        if not endpoint or not model:
            self._win.notify("Missing values", "Endpoint URL and model are required.")
            return
        self._win.set_status("Loading headstamps from server…")
        self._win.run_worker(
            lambda: api_client.get_headstamps(endpoint, model),
            on_done=self._on_headstamps_loaded,
            on_error=lambda exc: self._fail("Server error", exc),
        )

    def _on_headstamps_loaded(self, names: Any) -> None:
        added = sum(1 for name in (names or []) if name and self._win.config.add_headstamp(str(name)))
        self._announce_assignments()
        self.refresh_list()
        self._win.set_status(f"Loaded {added} new headstamp(s) from server.")

    # ----- single-shot test ---------------------------------------------------

    def _build_test_group(self) -> QGroupBox:
        box = QGroupBox("Test (capture → crop → classify)", self)
        row = QHBoxLayout(box)

        self.crop_label = QLabel(NO_RESULT, box)
        self.crop_label.setObjectName("cropPanel")
        self.crop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_label.setFixedSize(CROP_SIZE, CROP_SIZE)
        row.addWidget(self.crop_label)

        column = QVBoxLayout()
        self.test_button = QPushButton("Test shot", box)
        self.test_button.setObjectName("action")
        self.test_button.clicked.connect(self.run_test)
        column.addWidget(self.test_button)
        results = QFormLayout()
        self.result_label = QLabel(NO_RESULT, box)
        self.result_label.setObjectName("aiResultLabel")
        self.confidence_label = QLabel(NO_RESULT, box)
        results.addRow("Label", self.result_label)
        results.addRow("Confidence", self.confidence_label)
        column.addLayout(results)
        column.addStretch(1)
        row.addLayout(column, 1)
        return box

    def run_test(self) -> None:
        """Capture one frame, crop it, and classify it against the current fields.

        A frame is all this needs, so no board is required to check the server
        settings (unlike ``RunController.test_once``, which feeds a case first).
        Everything the worker reads is snapshotted here — it must not touch
        widgets or the DB-backed Config.
        """
        values = self._field_values()
        if not values["api_key"] or not values["model"]:
            self._win.notify("AI not configured", "Endpoint, API key and model must be set above first.")
            return
        image_proc_cfg = dict(self._win.config.image_proc)
        names = [str(e["name"]) for e in self._entries()]
        camera = self._win.camera

        def _work() -> tuple[np.ndarray, str, float] | None:
            frame = camera.capture_frame()
            if frame is None:
                return None
            cropped = crop_headstamp(frame, image_proc_cfg)
            cropped = apply_primer_mask(
                cropped,
                image_proc_cfg.get("primer_mode", "none"),
                int(image_proc_cfg.get("primer_radius", 0)),
            )
            label, confidence = api_client.classify(cropped, names, values)
            return cropped, label, confidence

        self.test_button.setEnabled(False)
        self._win.set_status("Capturing & classifying…")
        self._win.run_worker(_work, on_done=self._on_test_done, on_error=self._on_test_error)

    def _on_test_done(self, result: tuple[np.ndarray, str, float] | None) -> None:
        self.test_button.setEnabled(True)
        if result is None:
            self._win.set_status("No camera frame available — check Settings → Camera.")
            return
        cropped, label, confidence = result
        self._show_crop(cropped)
        self.result_label.setText(label or "(empty)")
        # api_client reports -1 when the server sent no confidence field.
        self.confidence_label.setText(NO_RESULT if confidence < 0 else f"{confidence:.2f}%")
        self._win.set_status(f"Classified as {label or '(empty)'}.")

    def _on_test_error(self, exc: Exception) -> None:
        self.test_button.setEnabled(True)
        self._fail("Test failed", exc)

    def _fail(self, title: str, exc: Exception) -> None:
        self._win.set_status(f"{title}: {exc}")
        self._win.notify(title, str(exc))

    def _show_crop(self, frame: np.ndarray) -> None:
        pixmap = QPixmap.fromImage(self._win.frame_to_image(frame))
        self.crop_label.setPixmap(
            pixmap.scaled(
                self.crop_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class AiPage(QWidget):
    """The form, or the explainer saying why it isn't the backend right now."""

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win

        # Index 0 is the working form, 1 the "why not" panel — `refresh_mode`
        # is the only thing that switches them. Same shape as train_page.
        self.stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.addWidget(self.stack)

        self.section = AiSection(win)
        self.stack.addWidget(self.section)
        self.stack.addWidget(self._build_unavailable_panel())
        self.refresh_mode()

    def _build_unavailable_panel(self) -> QWidget:
        panel = QWidget(self.stack)
        column = QVBoxLayout(panel)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(10)

        title = QLabel(UNAVAILABLE_TITLE, panel)
        title.setObjectName("emptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)

        self.notice_label = QLabel(LOCAL_MODEL_NOTICE_UNNAMED, panel)
        self.notice_label.setObjectName("mutedLabel")
        self.notice_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice_label.setWordWrap(True)
        self.notice_label.setMaximumWidth(560)
        column.addWidget(self.notice_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.models_button = QPushButton(MODELS_JUMP_TEXT, panel)
        self.models_button.clicked.connect(self._open_models_page)
        column.addWidget(self.models_button, 0, Qt.AlignmentFlag.AlignHCenter)
        return panel

    def _open_models_page(self) -> None:
        self._win.go_to_activity("Models")

    def is_available(self) -> bool:
        """Whether the form is showing rather than the explainer."""
        return self.stack.currentIndex() == 0

    def refresh_mode(self) -> None:
        """Re-read the active model and the (model-scoped) headstamp list.

        The server fields aren't model-scoped, so they are left alone — a mode
        change must not discard an edit that hasn't been saved yet.
        """
        self.section.refresh_list()
        if ai_config_mode(self._win):
            self.stack.setCurrentIndex(0)
            return
        name = active_model_name(self._win)
        self.notice_label.setText(LOCAL_MODEL_NOTICE.format(name=name) if name else LOCAL_MODEL_NOTICE_UNNAMED)
        self.stack.setCurrentIndex(1)


def build_ai_page(win: Any) -> AiPage:
    return AiPage(win)
