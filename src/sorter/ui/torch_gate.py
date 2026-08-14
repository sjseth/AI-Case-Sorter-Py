"""One place that decides whether a local-model action can proceed.

The rule: **torch is installed the first time something actually
needs it, and never before.** Downloading, importing or activating a model does
not trigger it; running, feeding, previewing a classification, evaluating and
training do. An AI Config user must never see this dialog.

The shell holds one gate on the window (``win.ensure_torch = TorchGate(win)``),
so the parent is bound once and call sites read::

    if not self.ensure_torch(self._start, reason="Sorting needs PyTorch"):
        return

Passing the caller itself as ``proceed`` is the point: the gate re-runs on the
second pass, finds torch present, and falls through to the work.

Main thread only — it opens a modal. Never call it from a worker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..ml import local_inference
from .dialog_install_torch import TorchInstallDialog

# Dialogs opened without a parent have no C++ owner; without this they can be
# garbage-collected out from under the user. A parented dialog needs none.
_ORPHANS: list[Any] = []


def _keep_alive(dialog: Any) -> None:
    _ORPHANS[:] = [d for d in _ORPHANS if _is_visible(d)]
    _ORPHANS.append(dialog)


def _is_visible(dialog: Any) -> bool:
    try:
        return bool(dialog.isVisible())
    except Exception:
        return False


class TorchGate:
    """Session-scoped gate bound to one window.

    ``__call__`` is the hard gate (Start, training, the evaluator): it asks
    every time, because the action genuinely cannot run without torch.
    ``offer`` is the soft one (Train's Feed, whose predicted label is a
    convenience): it asks at most once per session, so "no thanks" sticks.
    """

    def __init__(
        self,
        parent: Any = None,
        *,
        dialog_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.parent = parent
        # Read at construction, not bound as a default argument, so patching
        # the module name takes effect.
        self.dialog_factory = dialog_factory or TorchInstallDialog
        self.dialog: Any = None
        self._offered: set[str] = set()

    def __call__(
        self,
        proceed: Callable[[], None],
        *,
        reason: str | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> bool:
        """True when torch is already there and the caller should carry on.

        False when it isn't: the install dialog is now open, and ``proceed``
        runs if and only if the install succeeds. The caller must return
        immediately and do nothing else — the user may still cancel.

        Uses ``is_installed()`` (a ``find_spec`` probe), not ``is_available()``,
        so this never imports torch on the UI thread.
        """
        if local_inference.is_installed():
            return True
        self._open(proceed, reason=reason, on_cancel=on_cancel)
        return False

    def offer(
        self,
        proceed: Callable[[], None],
        *,
        reason: str | None = None,
        key: str = "default",
    ) -> bool:
        """Offer the install without making it a precondition.

        Returns True when the caller should carry on now — torch is present, or
        the user already answered this session. When it does open the dialog,
        ``proceed`` is re-entered on *both* success and cancel, which is what
        makes declining cost only what torch would have added.
        """
        if key in self._offered or local_inference.is_installed():
            return True
        self._offered.add(key)
        self._open(proceed, reason=reason, on_cancel=proceed)
        return False

    def _open(
        self,
        proceed: Callable[[], None],
        *,
        reason: str | None,
        on_cancel: Callable[[], None] | None,
    ) -> Any:
        dialog = self.dialog_factory(
            self.parent,
            on_success=proceed,
            on_cancel=on_cancel,
            reason=reason,
        )
        self.dialog = dialog
        if self.parent is None:
            _keep_alive(dialog)
        # open(), not exec(): modal to the window but returning immediately,
        # which is what lets the gate hand False back to its caller.
        dialog.open()
        return dialog


def ensure_torch(
    parent: Any,
    proceed: Callable[[], None],
    *,
    reason: str | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> bool:
    """One-shot form, for a call site that holds no gate."""
    return TorchGate(parent)(proceed, reason=reason, on_cancel=on_cancel)
