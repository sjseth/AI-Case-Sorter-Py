"""Sign-in modal — the Qt front of the MSAL interactive flow.

Behavior reference: ``ui/dialog_login.py``. Same one-button flow, the same
error cases (the redirect port already bound, an MSAL failure, anything else),
and the same effect: on success the caller re-gates the Community surface.

``login_interactive`` spawns a local listener and blocks until the browser
comes back, so it runs on an ``ApiWorker`` and the result crosses back as a
queued signal — the dialog itself never blocks the UI thread. The auth object
is injected (``auth=`` or ``auth_factory=``), which is what lets a test drive
the whole flow without MSAL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..community.auth import AuthError, PortInUseError
from .community_page import ApiWorker, start_worker

EXPLANATION = (
    "Sign in to browse, download and share community models.\n\n"
    "A browser window will open to complete the sign-in; come back here when it is done."
)
WORKING_STATUS = "Waiting for the browser to complete the sign-in…"
PORT_HINT = "\n\nClose any other instance of the app and try again."


class LoginDialog(QDialog):
    """Sign in / Cancel, plus the status line the worker writes into."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        auth: Any | None = None,
        auth_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._auth = auth
        self._auth_factory = auth_factory
        self._in_flight = False
        self.auth_result: Any | None = None
        # Replaceable hook — a test never opens a native dialog.
        self.notify: Callable[[str, str], None] = self._show_error

        self.setWindowTitle("Sign in")
        self.setModal(True)

        column = QVBoxLayout(self)
        column.setContentsMargins(16, 16, 16, 16)
        column.setSpacing(10)
        explanation = QLabel(EXPLANATION, self)
        explanation.setWordWrap(True)
        column.addWidget(explanation)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("dialogHint")
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        self.sign_in_button = QPushButton("Sign in", self)
        self.sign_in_button.setObjectName("action")
        self.sign_in_button.setDefault(True)
        self.sign_in_button.clicked.connect(self.sign_in)
        buttons.addWidget(self.sign_in_button)
        buttons.addWidget(self.cancel_button)
        column.addLayout(buttons)

    # ----- auth seam ----------------------------------------------------------

    def auth_manager(self) -> Any:
        """The injected manager, or one built on first use by the factory."""
        if self._auth is None:
            if self._auth_factory is None:
                raise AuthError("No authentication manager available.")
            self._auth = self._auth_factory()
        return self._auth

    # ----- flow ---------------------------------------------------------------

    def sign_in(self) -> None:
        if self._in_flight:
            return
        try:
            auth = self.auth_manager()
        except Exception as exc:
            self._fail(exc)
            return
        self._set_in_flight(True)
        self.status_label.setText(WORKING_STATUS)

        def work(report: Callable[[str], None]) -> Any:
            report(WORKING_STATUS)
            return auth.login_interactive()

        worker = ApiWorker(None, work)
        worker.progress.connect(self.status_label.setText)
        worker.done.connect(self._succeed)
        worker.failed.connect(self._fail)
        start_worker(worker)

    def _succeed(self, result: Any) -> None:
        """Signed in. The visible change is the caller's — sidebar and status."""
        self.auth_result = result
        self._set_in_flight(False)
        self.status_label.setText("Signed in.")
        self.accept()

    def _fail(self, exc: Any) -> None:
        self._set_in_flight(False)
        title, text = describe_login_error(exc)
        self.status_label.setText(text)
        self.notify(title, text)

    def _set_in_flight(self, busy: bool) -> None:
        self._in_flight = busy
        self.sign_in_button.setEnabled(not busy)
        self.sign_in_button.setText("Signing in…" if busy else "Sign in")

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    def keyPressEvent(self, event: Any) -> None:
        """Escape must not close the window out from under a running sign-in."""
        if event.key() == Qt.Key.Key_Escape and self._in_flight:
            event.ignore()
            return
        super().keyPressEvent(event)


def describe_login_error(exc: Any) -> tuple[str, str]:
    """(title, message) for a failed sign-in — the Tk dialog's three cases."""
    if isinstance(exc, PortInUseError):
        return "Port in use", f"{exc}{PORT_HINT}"
    if isinstance(exc, AuthError):
        return "Sign-in failed", str(exc)
    return "Sign-in failed", repr(exc)
