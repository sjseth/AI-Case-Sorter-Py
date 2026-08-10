"""One place that decides whether a local-model action can proceed.

PyTorch is an optional ~2 GB dependency (the `[ml]` extra, deliberately not in
`requirements.txt`), so a fresh install has no torch at all. That is correct
for AI Config users, who classify over HTTP and never need it — but a user who
downloads a community model and presses Start is about to need it, and the
install must be offered *then*, not discovered when the run dies on the first
case.

The rule this module encodes: **torch is installed the first time something
actually needs it, and never before.** Downloading, importing or activating a
model does not trigger it — those are all just rows and files. Running,
feeding, previewing a classification, evaluating and training do.

Usage is always the same shape — hand `ensure_torch` the method it is gating,
and let it re-enter that method once the install finishes:

    def _start(self):
        if not ensure_torch(self, self._start, reason="Sorting needs PyTorch"):
            return
        ...the actual work...

Passing the caller itself as `proceed` is the point: the gate re-runs on the
second pass, finds torch present, and falls through to the work. Nothing has
to be factored out into a continuation.

Must be called on the Tk main thread (it may open a modal) — so from button
handlers and event callbacks, never from inside `run_worker`.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from .. import local_inference
from .dialog_install_torch import TorchInstallDialog


def ensure_torch(
    parent: tk.Misc,
    proceed: Callable[[], None],
    *,
    reason: str | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> bool:
    """Gate a local-model action on PyTorch being installed.

    Returns True when torch is already there and the caller should carry
    straight on.

    Returns False when it isn't: the install dialog is now open, and `proceed`
    will be called if and only if the install succeeds. The caller must return
    immediately and do nothing else — the user may still cancel.

    Uses `is_installed()` (a `find_spec` probe), not `is_available()`, so this
    never imports torch on the UI thread.
    """
    if local_inference.is_installed():
        return True
    TorchInstallDialog(
        parent,
        on_success=proceed,
        on_cancel=on_cancel,
        reason=reason,
    )
    return False
