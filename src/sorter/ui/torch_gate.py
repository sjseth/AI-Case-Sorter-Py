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

A second rule sits on top of presence, and it is a security floor rather than a
capability one: **a model that came from somewhere else may not be loaded by a
torch below** ``local_inference.MIN_TORCH_VERSION``. CVE-2026-24747 lets a
crafted ``.pth`` defeat the ``weights_only=True`` unpickler that ``_load``
relies on to make an untrusted checkpoint safe, so for a community download, a
ZIP import or a WinForms import the gate blocks until the user upgrades. For a
checkpoint trained here the upgrade is *offered* and declining is remembered
for the session — their own checkpoint is not an attack, and a multi-GB
download should not stand between them and sorting their own brass.

That asymmetry is why callers pass ``model``: the gate decides block-vs-offer
from ``models.needs_torch_floor`` — where the *checkpoint* came from, not who
may train the model — so no call site has to encode the policy. The gate runs
when an action starts; ``classifier.classify_active`` repeats the check on
every case, which is what covers a model activated mid-run.

Main thread only — it opens a modal. Never call it from a worker.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..data.models import Model, needs_torch_floor
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
        # Set when the user declines the *optional* upgrade offer, so their own
        # models' workflow is interrupted once per session rather than on every
        # Start. Never consulted for a foreign model — that path has no
        # "not now".
        self._upgrade_declined = False

    def __call__(
        self,
        proceed: Callable[[], None],
        *,
        reason: str | None = None,
        on_cancel: Callable[[], None] | None = None,
        model: Model | None = None,
    ) -> bool:
        """True when torch is already there and the caller should carry on.

        False when it isn't: the install dialog is now open, and ``proceed``
        runs if and only if the install succeeds. The caller must return
        immediately and do nothing else — the user may still cancel.

        ``model`` is the model the action will load; it selects the policy for
        an installed-but-outdated torch (see the module docstring). Omitting it
        is the conservative choice — unknown provenance is treated as foreign.

        Uses ``is_installed()`` (a ``find_spec`` probe) and distribution
        metadata, not ``is_available()``, so this never imports torch on the UI
        thread.
        """
        if not local_inference.is_installed():
            self._open(proceed, reason=reason, on_cancel=on_cancel)
            return False
        return self._version_ok(proceed, on_cancel=on_cancel, model=model)

    def offer(
        self,
        proceed: Callable[[], None],
        *,
        reason: str | None = None,
        key: str = "default",
        model: Model | None = None,
    ) -> bool:
        """Offer the install without making it a precondition.

        Returns True when the caller should carry on now — torch is present and
        new enough, or the user already answered this session. When it does open
        the dialog, ``proceed`` is re-entered on *both* success and cancel, which
        is what makes declining cost only what torch would have added.
        """
        if local_inference.is_installed():
            return self._version_ok(proceed, on_cancel=proceed, model=model)
        if key in self._offered:
            return True
        self._offered.add(key)
        self._open(proceed, reason=reason, on_cancel=proceed)
        return False

    def _version_ok(
        self,
        proceed: Callable[[], None],
        *,
        on_cancel: Callable[[], None] | None,
        model: Model | None,
    ) -> bool:
        """Presence is settled; decide whether *this* torch may load ``model``.

        Blocks for a checkpoint that is someone else's, offers for one the user
        trained here. Both open the same install dialog — the difference is
        only what a cancel does.
        """
        if local_inference.meets_min_version():
            return True
        have = local_inference.installed_version() or "an older version"
        floor = local_inference.MIN_TORCH_VERSION
        if needs_torch_floor(model):
            self._open(
                proceed,
                reason=(
                    f"Update PyTorch to use this model — you have {have}, and loading a "
                    f"downloaded or imported model needs {floor} or newer (security fix)"
                ),
                on_cancel=on_cancel,
            )
            return False
        if self._upgrade_declined:
            return True

        def _decline() -> None:
            self._upgrade_declined = True
            (on_cancel or proceed)()

        self._open(
            proceed,
            reason=(
                f"A PyTorch update is available — you have {have}. {floor} or newer is "
                "required before loading models from the community, and fixes a security issue"
            ),
            on_cancel=_decline,
        )
        return False

    def _open(
        self,
        proceed: Callable[[], None],
        *,
        reason: str | None,
        on_cancel: Callable[[], None] | None,
    ) -> Any:
        def _installed() -> None:
            # Any completed install clears the decline: whatever the user was
            # putting off, they have now done it, and leaving the flag set
            # would outlive the condition it describes.
            self._upgrade_declined = False
            proceed()

        dialog = self.dialog_factory(
            self.parent,
            on_success=_installed,
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
    model: Model | None = None,
) -> bool:
    """One-shot form, for a call site that holds no gate.

    A fresh gate has no memory, so a declined upgrade offer does not stick
    here — hold a ``TorchGate`` if that matters.
    """
    return TorchGate(parent)(proceed, reason=reason, on_cancel=on_cancel, model=model)
