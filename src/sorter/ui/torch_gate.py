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

A second rule sits on top of presence, and it is a security floor rather than
a capability one: **a model that came from somewhere else may not be loaded by
a torch below `local_inference.MIN_TORCH_VERSION`.** CVE-2026-24747 lets a
crafted `.pth` defeat the `weights_only=True` unpickler that `_load` relies on
to make an untrusted checkpoint safe, so for a community download or a ZIP
import the gate blocks until the user upgrades. For a model the user trained
here the upgrade is *offered* and declining is remembered for the session —
their own checkpoint is not an attack, and a multi-GB download should not
stand between them and sorting their own brass.

That asymmetry is why callers pass `model`: the gate decides block-vs-offer
from `models.is_foreign_model`, so no call site has to encode the policy.

Must be called on the Tk main thread (it may open a modal) — so from button
handlers and event callbacks, never from inside `run_worker`.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from ..data.models import Model, is_foreign_model
from ..ml import local_inference
from .dialog_install_torch import TorchInstallDialog

# Set when the user declines the *optional* upgrade offer, so their own-model
# workflow is interrupted once per session rather than on every Start. Never
# consulted for a foreign model -- that path has no "not now".
_upgrade_declined = False


def reset_upgrade_prompt() -> None:
    """Forget a declined upgrade offer (test seam; also correct after an install)."""
    global _upgrade_declined
    _upgrade_declined = False


def ensure_torch(
    parent: tk.Misc,
    proceed: Callable[[], None],
    *,
    reason: str | None = None,
    on_cancel: Callable[[], None] | None = None,
    model: Model | None = None,
) -> bool:
    """Gate a local-model action on PyTorch being installed and safe to use.

    Returns True when the caller should carry straight on.

    Returns False when it should not: the install dialog is now open, and
    `proceed` will be called if and only if the install succeeds. The caller
    must return immediately and do nothing else — the user may still cancel.

    `model` is the model the action will load, and it selects the policy for
    an installed-but-outdated torch (see the module docstring): foreign models
    block, own models get a once-per-session offer that proceeds on decline.
    Omitting it is the conservative choice — an unknown provenance is treated
    as foreign.

    Uses `is_installed()` (a `find_spec` probe) and distribution metadata, not
    `is_available()`, so this never imports torch on the UI thread.
    """
    global _upgrade_declined

    def _upgraded() -> None:
        # Any completed install clears the decline: whatever the user was
        # putting off, they have now done it. Leaving the flag set would
        # outlive the condition it describes.
        reset_upgrade_prompt()
        proceed()

    if not local_inference.is_installed():
        TorchInstallDialog(
            parent,
            on_success=_upgraded,
            on_cancel=on_cancel,
            reason=reason,
        )
        return False

    if local_inference.meets_min_version():
        return True

    have = local_inference.installed_version() or "an older version"
    if model is None or is_foreign_model(model):
        # Blocking: this checkpoint is someone else's, and on this torch
        # weights_only=True is not the protection the loader assumes it is.
        TorchInstallDialog(
            parent,
            on_success=_upgraded,
            on_cancel=on_cancel,
            reason=(
                f"Update PyTorch to use this model — you have {have}, and "
                f"loading a downloaded or imported model needs "
                f"{local_inference.MIN_TORCH_VERSION} or newer (security fix)"
            ),
        )
        return False

    if _upgrade_declined:
        return True

    def _decline() -> None:
        global _upgrade_declined
        _upgrade_declined = True
        if on_cancel is not None:
            on_cancel()
        else:
            proceed()

    TorchInstallDialog(
        parent,
        on_success=_upgraded,
        on_cancel=_decline,
        reason=(
            f"A PyTorch update is available — you have {have}. "
            f"{local_inference.MIN_TORCH_VERSION} or newer is required before "
            f"loading models from the community, and fixes a security issue"
        ),
    )
    return False
