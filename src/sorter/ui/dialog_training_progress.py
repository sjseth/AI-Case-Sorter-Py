"""Live training console.

``TrainingManager`` already turns the subprocess's ``[PROGRESS]`` markers into
``training/*`` bus events; this window is purely a subscriber. It never touches
the process: Cancel calls the ``on_cancel`` it was handed (the manager's own
``cancel``, which owns the SIGTERM→SIGKILL escalation).

Two things it is careful about:

* **It unsubscribes on close.** A deleted Qt widget raises ``RuntimeError``
  from anywhere in the C++ half, so the handlers have to come off the bus —
  a liveness guard inside each one is not enough.
* **Closing while the run is live asks first.** The window is not the run — the
  subprocess outlives it — so closing it silently would strand a training run
  with no console and no way back to it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..control.events import EventBus

CONSOLE_LINES = 2000
STARTING_TEXT = "Starting…"
CLOSE_WHILE_RUNNING_TITLE = "Training is still running"
CLOSE_WHILE_RUNNING_TEXT = (
    "Closing this window would leave the training subprocess running with "
    "nowhere to report.\n\nCancel the run and close?"
)


class TrainingProgressDialog(QDialog):
    """Console + progress bar for one training run.

    ``final_payload`` holds the terminal event's payload once the run ends;
    the Train page reads it to decide whether a checkpoint was produced.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        on_cancel: Callable[[], None],
        on_closed: Callable[[dict[str, Any] | None], None] | None = None,
        parent: QWidget | None = None,
        title: str = "Training progress",
    ) -> None:
        super().__init__(parent)
        self.bus = bus
        self.on_cancel = on_cancel
        self.on_closed = on_closed
        self.final_payload: dict[str, Any] | None = None
        self.finished_run = False
        self._closed = False
        # Replaceable so a test can drive the close guard without a modal.
        self.confirm_close: Callable[[], bool] = self._ask_close

        self.setWindowTitle(title)
        self.resize(720, 420)

        column = QVBoxLayout(self)
        self.status_label = QLabel(STARTING_TEXT, self)
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)

        self.progress = QProgressBar(self)
        self.progress.setObjectName("trainingProgress")
        # Indeterminate until a start/epoch event says how many epochs there are.
        self.progress.setRange(0, 0)
        column.addWidget(self.progress)

        self.console = QPlainTextEdit(self)
        self.console.setObjectName("trainingConsole")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(CONSOLE_LINES)
        # Epoch lines are columns of numbers; they only line up in a fixed font.
        self.console.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        column.addWidget(self.console, 1)

        row = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel training", self)
        self.cancel_button.setObjectName("danger")
        self.cancel_button.clicked.connect(self.cancel)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        self.close_button = QPushButton("Close", self)
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        column.addLayout(row)

        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        for topic, handler in (
            ("training/start", self._on_start),
            ("training/epoch", self._on_epoch),
            ("training/log", self._on_log),
            ("training/error", self._on_error),
            ("training/done", self._on_done),
            ("training/failed", self._on_failed),
            ("training/cancelled", self._on_cancelled),
        ):
            bus.subscribe(topic, handler)
            self._subscriptions.append((topic, handler))

    # ----- console ------------------------------------------------------------

    def append(self, text: str) -> None:
        self.console.appendPlainText(text)

    def console_text(self) -> str:
        return self.console.toPlainText()

    # ----- bus handlers -------------------------------------------------------

    def _on_start(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {}
        self._set_total(data.get("epochs"))
        self.status_label.setText(
            f"Training {data.get('model', '')} — epochs={data.get('epochs')} "
            f"classes={data.get('classes')} images={data.get('images')} "
            f"device={data.get('device')}"
        )
        self.append(f"[start] {data}")

    def _on_epoch(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {}
        epoch = data.get("epoch")
        total = data.get("total")
        if total:
            self._set_total(total)
        if isinstance(epoch, (int, float)):
            self.progress.setValue(int(epoch))
        bits = [f"Epoch {epoch}/{total or self.progress.maximum() or '?'}"]
        for key, caption in (("train_loss", "train_loss"), ("train_acc", "train_acc"), ("val_acc", "val_acc")):
            value = data.get(key)
            if isinstance(value, (int, float)):
                bits.append(f"{caption}={value:.4f}")
        self.status_label.setText("  ".join(bits))
        self.append(f"[epoch] {data}")

    def _on_log(self, line: Any) -> None:
        if line:
            self.append(str(line))

    def _on_error(self, line: Any) -> None:
        if line:
            self.append(f"[err] {line}")

    def _on_done(self, payload: Any) -> None:
        self._terminal("Done", payload)

    def _on_failed(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {}
        self._terminal(f"Failed (rc={data.get('return_code')})", payload)

    def _on_cancelled(self, payload: Any) -> None:
        self._terminal("Cancelled.", payload)

    def _set_total(self, total: Any) -> None:
        if isinstance(total, (int, float)) and int(total) > 0:
            self.progress.setRange(0, int(total))

    def _terminal(self, label: str, payload: Any) -> None:
        self.final_payload = payload if isinstance(payload, dict) else None
        self.finished_run = True
        self.status_label.setText(label)
        self.append(f"[{label.lower()}] {payload}")
        if self.progress.maximum() == 0:
            # Nothing ever reported an epoch count; stop the busy animation.
            self.progress.setRange(0, 1)
        self.progress.setValue(self.progress.maximum())
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)

    # ----- buttons / lifecycle ------------------------------------------------

    def cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancel requested…")
        self.on_cancel()

    def _ask_close(self) -> bool:
        return (
            QMessageBox.question(self, CLOSE_WHILE_RUNNING_TITLE, CLOSE_WHILE_RUNNING_TEXT)
            == QMessageBox.StandardButton.Yes
        )

    def closeEvent(self, event: Any) -> None:
        """Guard the close while a run is live, then detach from the bus."""
        if not self.finished_run and not self._closed:
            if not self.confirm_close():
                event.ignore()
                return
            self.cancel()
        self._detach()
        if self.on_closed is not None:
            self.on_closed(self.final_payload)
            self.on_closed = None
        super().closeEvent(event)

    def _detach(self) -> None:
        if self._closed:
            return
        self._closed = True
        for topic, handler in self._subscriptions:
            self.bus.unsubscribe(topic, handler)
        self._subscriptions = []


def build_training_progress_dialog(
    bus: EventBus,
    *,
    on_cancel: Callable[[], None],
    on_closed: Callable[[dict[str, Any] | None], None] | None = None,
    parent: QWidget | None = None,
) -> TrainingProgressDialog:
    return TrainingProgressDialog(bus, on_cancel=on_cancel, on_closed=on_closed, parent=parent)
