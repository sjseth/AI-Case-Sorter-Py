"""Dispatcher: pick local PyTorch inference or HTTP classify based on active model.

Called from `RunController` so the run loop doesn't need to know which backend
is active. **The active model alone decides the backend:**
  - A model is active → local inference, always
  - AI Config mode (no active model) → HTTP via `api_client.classify`

A local model whose checkpoint is missing raises `NoLocalCheckpointError`. It
does **not** quietly become an HTTP classification. That fallback used to
exist, and it was a trap: renaming the data folder (or an images-only
community share) left `model_path` unusable, and the app silently started
POSTing case images to whatever the AI Config tab last pointed at. If that
endpoint happened to answer, the user got confident-looking labels from a
model they never chose; when it didn't, the only symptom was a connection
error naming a host they weren't knowingly using. Neither is something to
infer on the user's behalf — the switch is theirs to make on the Models tab.
Note the AI Config tab is hidden while a local model is active, so its
endpoint isn't even reachable to configure in that state.

`active_model` / `uses_local_inference` / `has_local_checkpoint` expose the
routing decision on its own so the UI can answer "is this about to need
PyTorch?" and "can this model actually classify?" *before* starting a run.
Keep them in lock-step with `classify_active`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from . import api_client, local_inference, paths
from .db import Database
from .models import Model
from .repository import ModelRepo, SettingsRepo


class NoLocalCheckpointError(Exception):
    """The active model can't classify: its trained checkpoint is missing.

    Raised instead of silently classifying over HTTP — see the module
    docstring.
    """


def has_local_checkpoint(model: Model | None) -> bool:
    """True when `model` has a trained checkpoint present on disk."""
    return bool(model is not None and model.model_path and Path(model.model_path).exists())


def active_model(db: Database | None) -> Model | None:
    """The active model, or None for AI Config mode (classify over HTTP).

    None also covers `db is None` (tests that don't need a database) and a
    `default_model_id` pointing at a row that no longer exists.
    """
    if db is None:
        return None
    active_id = SettingsRepo(db).get_active_model_id()
    if active_id is None:
        return None
    return ModelRepo(db).get(active_id)


def uses_local_inference(db: Database | None) -> bool:
    """True when classification will run locally — i.e. will need PyTorch.

    This is "a local model is active", not "a local model can currently
    classify": a model with a missing checkpoint still routes locally, it
    just fails loudly. Callers that need to know whether it will actually
    work should ask `checkpoint_problem` first.
    """
    return active_model(db) is not None


def checkpoint_problem(db: Database | None) -> str | None:
    """A user-facing explanation of why the active model can't classify.

    None when there's nothing wrong — AI Config mode, or a local model whose
    checkpoint is present. The UI uses this to refuse *before* the machine
    feeds a case; `classify_active` raises the same text as a backstop for a
    checkpoint that disappears mid-run.
    """
    model = active_model(db)
    if model is None or has_local_checkpoint(model):
        return None
    return _checkpoint_detail(model)


def _checkpoint_summary(model: Model) -> str:
    """One-line form, for `NoLocalCheckpointError` and the run status bar.

    `_checkpoint_detail` is the multi-paragraph version the dialogs show;
    the status bar renders a single line, so it needs its own wording rather
    than a truncated one.
    """
    name = model.name or f"model #{model.id}"
    if not model.model_path:
        return f"“{name}” has no trained model file — train it or re-download it before sorting."
    return f"“{name}”: trained model file is missing — {model.model_path}"


def _checkpoint_detail(model: Model) -> str:
    name = model.name or f"model #{model.id}"
    if not model.model_path:
        detail = f"“{name}” has no trained model file yet.\n\nExpected it in:\n{paths.model_trained_dir(model.id)}"
        hint = (
            "Train the model on the Train tab, or — if it came from the "
            "Community tab — re-download it, since an images-only share "
            "carries no model file."
        )
    else:
        detail = f"“{name}” points at a trained model file that isn't there:\n\n{model.model_path}"
        hint = (
            "This usually means the data folder was moved or renamed. Put it "
            "back, re-download the model, or re-train it."
        )
    return (
        f"{detail}\n\n{hint}\n\n"
        "Sorting is stopped rather than falling back to the AI Config server "
        "— switch to “Use AI Config” on the Models tab if that's what you want."
    )


def classify_active(
    image_bgr: np.ndarray,
    headstamps: list[str],
    api_cfg: dict[str, Any],
    db: Database | None,
) -> tuple[str, float]:
    """Classify `image_bgr` using whichever backend the active model selects.

    Raises `NoLocalCheckpointError` when a local model is active but its
    checkpoint is missing. Uses HTTP only in AI Config mode (which includes
    `db is None`, for tests that don't need a database).
    """
    model = active_model(db)
    if model is None:
        return api_client.classify(image_bgr, headstamps, api_cfg)

    # `not model.model_path` is folded into the guard (redundant with
    # `has_local_checkpoint`'s own check) so the type checker can narrow
    # `model.model_path` from `str | None` to `str` below.
    if not has_local_checkpoint(model) or not model.model_path:
        raise NoLocalCheckpointError(_checkpoint_summary(model))

    # Pass the trained image size from the model record so imported community
    # models (often trained at 480) get the right resolution at inference.
    image_size = int(model.training_config.image_size) if model.training_config else None
    return local_inference.classify(
        image_bgr,
        model.model_path,
        image_size=image_size,
    )
