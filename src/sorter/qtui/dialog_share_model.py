"""Share a model — export a local model and publish it to the community.

Behavior reference: ``ui/dialog_share_model.py``: one dialog that picks a
local model, collects the share metadata, then packages and runs the upload
sequence (FileUploadRequest → PUT zip → CompleteUpload → ManifestUploadRequest
→ PUT manifest) and stamps the returned UID / version back onto the local row.

**Sharing your own model does not make it foreign.** The stamp is the community
UID and the version, nothing else: ``model_type`` is untouched, so
``is_trainable`` stays true and the Train activity keeps working on it. A UID
means "exists in the community", not "isn't yours" (CLAUDE.md §5).

Packaging and uploading run on an ``ApiWorker``; progress crosses back as a
queued signal, so the status line updates without the UI thread ever waiting on
the network. ``notify`` / ``confirm`` are attributes so a test drives the flow
with nothing modal opening.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..community.community_api import CartridgeInfo
from ..data.model_io import ExportMode, export_for_share
from ..data.models import Model
from ..data.repository import CartridgeRepo, HeadstampRepo, ModelRepo
from .community_page import ApiWorker, start_worker

# Display label <-> ExportMode. ManifestOnly is deliberately absent — there is
# nothing useful to share without either a model or images.
EXPORT_LABELS: dict[str, ExportMode] = {
    "Model and images": ExportMode.MODEL_AND_IMAGES,
    "Model only": ExportMode.MODEL_ONLY,
    "Images only": ExportMode.IMAGES_ONLY,
}

# Server-side ModelExportMode is a numeric enum; send the integer it binds
# (ModelOnly=0, ModelAndImages=1, ImagesOnly=2, ManifestOnly=3).
EXPORT_MODE_INT: dict[ExportMode, int] = {
    ExportMode.MODEL_ONLY: 0,
    ExportMode.MODEL_AND_IMAGES: 1,
    ExportMode.IMAGES_ONLY: 2,
}

DEFAULT_FLOOR = 95
NO_MODEL_TITLE = "Pick a model"
MISSING_NAME_TITLE = "Missing name"
NO_CHECKPOINT_TITLE = "No trained model"
NO_CHECKPOINT_TEXT = 'This model has no trained file yet. Choose "Images only", or train the model first.'


def image_count(images_dir: Path) -> int:
    if not images_dir.exists():
        return 0
    return sum(1 for p in images_dir.iterdir() if p.is_file())


class ShareModelDialog(QDialog):
    """Metadata form + the packaging/upload run, in one window."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        db: Any,
        api_factory: Callable[[], Any],
        username: str = "",
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.api_factory = api_factory
        self.username = username or ""
        self.models_repo = ModelRepo(db)
        self.cartridges_repo = CartridgeRepo(db)
        self.headstamps_repo = HeadstampRepo(db)

        self._models: list[Model] = self.models_repo.list()
        self._community_cartridges: list[CartridgeInfo] = []
        self._in_flight = False
        # The UID the server accepted, once a share has completed.
        self.shared_uid: str | None = None
        # Replaceable hooks — a test never opens a native dialog.
        self.notify: Callable[[str, str], None] = self._show_message
        self.confirm: Callable[[str, str], bool] = self._ask

        self.setWindowTitle("Share a model")
        self.setModal(True)

        column = QVBoxLayout(self)
        column.setContentsMargins(14, 14, 14, 14)
        column.setSpacing(10)
        column.addLayout(self._build_form())
        column.addWidget(self._build_feedback_box())
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("dialogHint")
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)
        column.addLayout(self._build_buttons())

        self._on_model_selected()
        self.load_community_cartridges()

    # ----- construction -------------------------------------------------------

    def _build_form(self) -> QFormLayout:
        form = QFormLayout()
        self.model_combo = QComboBox(self)
        self.model_combo.addItems([m.name for m in self._models])
        self.model_combo.currentIndexChanged.connect(lambda _i: self._on_model_selected())
        form.addRow("Model", self.model_combo)

        self.name_edit = QLineEdit(self)
        form.addRow("Share name", self.name_edit)

        self.description_edit = QPlainTextEdit(self)
        self.description_edit.setFixedHeight(72)
        form.addRow("Description", self.description_edit)

        self.cartridge_combo = QComboBox(self)
        self.cartridge_combo.setMinimumWidth(240)
        form.addRow("Cartridge", self.cartridge_combo)

        self.export_combo = QComboBox(self)
        self.export_combo.addItems(list(EXPORT_LABELS))
        form.addRow("Include", self.export_combo)
        return form

    def _build_feedback_box(self) -> QGroupBox:
        box = QGroupBox("Community feedback loop", self)
        row = QHBoxLayout(box)
        self.fb_enabled_check = QCheckBox("Enable feedback loop", box)
        self.fb_enabled_check.toggled.connect(self._on_feedback_toggled)
        row.addWidget(self.fb_enabled_check)
        caption = QLabel("Confidence floor", box)
        caption.setObjectName("mutedLabel")
        row.addWidget(caption)
        self.fb_floor_spin = QSpinBox(box)
        self.fb_floor_spin.setRange(0, 100)
        self.fb_floor_spin.setValue(DEFAULT_FLOOR)
        self.fb_floor_spin.setSuffix(" %")
        self.fb_floor_spin.setEnabled(False)
        row.addWidget(self.fb_floor_spin)
        row.addStretch(1)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        self.share_button = QPushButton("Share", self)
        self.share_button.setObjectName("action")
        self.share_button.setDefault(True)
        self.share_button.clicked.connect(self.share)
        row.addWidget(self.share_button)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        return row

    # ----- form state ---------------------------------------------------------

    def selected_model(self) -> Model | None:
        index = self.model_combo.currentIndex()
        if index < 0 or index >= len(self._models):
            return None
        return self._models[index]

    def _local_cartridge_name(self, model: Model | None) -> str:
        cartridge = self.cartridges_repo.get(model.cartridge_id) if model is not None else None
        return cartridge.name if cartridge else ""

    def _on_model_selected(self) -> None:
        model = self.selected_model()
        if model is None:
            return
        self.name_edit.setText(model.name)
        self._sync_cartridge_default(model)

    def _sync_cartridge_default(self, model: Model) -> None:
        """Preselect the community cartridge matching the model's local one."""
        target = self._local_cartridge_name(model).casefold()
        for index, cartridge in enumerate(self._community_cartridges):
            if cartridge.name.casefold() == target:
                self.cartridge_combo.setCurrentIndex(index)
                return

    def _on_feedback_toggled(self, enabled: bool) -> None:
        self.fb_floor_spin.setEnabled(bool(enabled))

    def load_community_cartridges(self) -> None:
        def work(_report: Callable[[str], None]) -> Any:
            return self.api_factory().get_available_cartridges()

        worker = ApiWorker(None, work)
        # Bound methods, never lambdas/closures: a lambda's connection lives
        # as long as the WORKER, so a queued emission arriving after this
        # dialog is destroyed would run it against freed widgets (the access
        # violations Windows CI kept taking at event pumps). Qt drops a
        # bound-method connection — queued deliveries included — when its
        # receiver dies.
        worker.done.connect(self._on_cartridges_loaded)
        worker.failed.connect(self._on_cartridges_failed)
        start_worker(worker)

    def _on_cartridges_loaded(self, cartridges: Any) -> None:
        self._community_cartridges = list(cartridges or [])
        self.cartridge_combo.clear()
        self.cartridge_combo.addItems([c.name for c in self._community_cartridges])
        model = self.selected_model()
        if self._community_cartridges and model is not None:
            self.cartridge_combo.setCurrentIndex(0)
            self._sync_cartridge_default(model)

    def _on_cartridges_failed(self, exc: Any) -> None:
        self.status_label.setText(f"Could not load cartridges: {exc}")

    def selected_cartridge(self, model: Model) -> tuple[int, str]:
        """The community cartridge to publish under, else the local name with id 0."""
        index = self.cartridge_combo.currentIndex()
        if 0 <= index < len(self._community_cartridges):
            cartridge = self._community_cartridges[index]
            return cartridge.id, cartridge.name
        return 0, self._local_cartridge_name(model)

    # ----- share --------------------------------------------------------------

    def share(self) -> None:
        if self._in_flight:
            return
        model = self.selected_model()
        if model is None:
            self.notify(NO_MODEL_TITLE, "Select a model to share.")
            return
        name = self.name_edit.text().strip()
        if not name:
            self.notify(MISSING_NAME_TITLE, "Enter a share name.")
            return
        mode = EXPORT_LABELS[self.export_combo.currentText()]
        model_file = Path(model.model_path) if model.model_path and Path(model.model_path).exists() else None
        if mode in (ExportMode.MODEL_ONLY, ExportMode.MODEL_AND_IMAGES) and model_file is None:
            self.notify(NO_CHECKPOINT_TITLE, NO_CHECKPOINT_TEXT)
            return

        cartridge_id, cartridge_name = self.selected_cartridge(model)
        description = self.description_edit.toPlainText().strip()
        feedback_enabled = self.fb_enabled_check.isChecked()
        feedback_floor = int(self.fb_floor_spin.value()) if feedback_enabled else 0
        headstamps = [h.name for h in self.headstamps_repo.list_for_model(model.id)]
        images_dir = paths.model_images_dir(model.id)
        # Re-publishing keeps the UID and bumps the version; a first share mints
        # a UID (a standard GUID, with dashes) and publishes at the current one.
        uid = model.community_model_uid or str(uuid4())
        version = model.model_version + 1 if model.community_model_uid else model.model_version

        model_info = {
            "ModelName": name,
            "PublishDate": datetime.now(UTC).isoformat(),
            "CartridgeId": cartridge_id,
            "CartridgeName": cartridge_name,
            "HeadstampCount": len(headstamps),
            "ImageCount": image_count(images_dir),
            "ModelDescription": description,
            "Author": self.username,
            "ModelExportMode": EXPORT_MODE_INT.get(mode, 1),
            "FeedbackLoopEnabled": feedback_enabled,
            "FeedbackLoopConfidenceFloor": feedback_floor,
            "ModelVersion": version,
            "CommunityModelUID": uid,
        }

        self._set_in_flight(True)
        self.status_label.setText("Packaging…")

        def work(report: Callable[[str], None]) -> tuple[str, int]:
            return self._package_and_upload(
                report,
                model=model,
                model_info=model_info,
                mode=mode,
                model_file=model_file,
                images_dir=images_dir,
                headstamps=headstamps,
                cartridge_name=cartridge_name,
                uid=uid,
                version=version,
                feedback_enabled=feedback_enabled,
                feedback_floor=feedback_floor,
            )

        worker = ApiWorker(None, work)
        worker.progress.connect(self.status_label.setText)
        # Receiver-bound, not a lambda — see _on_cartridges_loaded's note.
        # The share context rides on the dialog so the slot signature stays
        # a plain bound method Qt can drop with its receiver.
        self._pending_share = (model, name)
        worker.done.connect(self._on_share_done)
        worker.failed.connect(self._on_failed)
        start_worker(worker)

    def _on_share_done(self, result: Any) -> None:
        model, name = self._pending_share
        self._on_shared(model, name, result)

    def _package_and_upload(
        self,
        report: Callable[[str], None],
        *,
        model: Model,
        model_info: dict[str, Any],
        mode: ExportMode,
        model_file: Path | None,
        images_dir: Path,
        headstamps: list[str],
        cartridge_name: str,
        uid: str,
        version: int,
        feedback_enabled: bool,
        feedback_floor: int,
    ) -> tuple[str, int]:
        """Worker body: build the archive, run the upload sequence, return (uid, version)."""
        # Scratch in the app's own data dir, not the OS temp dir, which on
        # Windows can be locked or cleaned mid-write. Removed either way.
        scratch_root = paths.export_temp_dir()
        scratch_root.mkdir(parents=True, exist_ok=True)
        tmpdir = tempfile.mkdtemp(dir=str(scratch_root))
        try:
            zip_path, manifest_path = export_for_share(
                Path(tmpdir) / f"{uid}.zip",
                model,
                cartridge_name,
                headstamps,
                mode=mode,
                model_file=model_file,
                images_dir=images_dir,
                community_uid=uid,
                feedback_enabled=feedback_enabled,
                feedback_floor=feedback_floor,
                progress=lambda step, total: report(f"Packaging {step}/{total} files…"),
            )
            last_pct = [-1]

            def upload_progress(sent: int, total: int) -> None:
                pct = int(sent * 100 / total) if total else 0
                if pct == last_pct[0]:
                    return
                last_pct[0] = pct
                report(f"Uploading {pct}% ({sent / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB)")

            final_uid = self.api_factory().share_model(
                zip_path=zip_path,
                manifest_path=manifest_path,
                model_info=model_info,
                progress=upload_progress,
            )
            return final_uid or uid, version
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _on_shared(self, model: Model, name: str, result: tuple[str, int]) -> None:
        """Stamp the community identity onto the local row — and nothing else.

        The row keeps its ``model_type``, so a model you published is still
        yours to train; only the browser's installed/up-to-date check reads
        what is written here.
        """
        final_uid, final_version = result
        self.shared_uid = final_uid
        fresh = self.models_repo.get(model.id) or model
        fresh.community_model_uid = final_uid
        fresh.model_version = final_version
        try:
            self.models_repo.update(fresh)
        except Exception as exc:
            # The upload succeeded; only the local bookkeeping failed.
            self.status_label.setText(f"Shared, but the local model could not be updated: {exc}")
            self._set_in_flight(False)
            return
        self._set_in_flight(False)
        self.status_label.setText("Shared successfully.")
        self.notify("Model shared", f"“{name}” has been uploaded to the community.")
        self.accept()

    def _on_failed(self, exc: Any) -> None:
        self._set_in_flight(False)
        self.status_label.setText(f"Share failed: {exc}")
        self.notify("Share failed", str(exc))

    def _set_in_flight(self, busy: bool) -> None:
        self._in_flight = busy
        self.share_button.setEnabled(not busy)
        self.share_button.setText("Sharing…" if busy else "Share")

    # ----- lifecycle ----------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        if self._in_flight and not self.confirm("Upload in progress", "An upload is still running. Close anyway?"):
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._in_flight and not self.confirm("Upload in progress", "An upload is still running. Close anyway?"):
            return
        super().reject()

    # ----- dialog hooks -------------------------------------------------------

    def _show_message(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes
