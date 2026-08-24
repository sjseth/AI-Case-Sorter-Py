"""Dispatcher: pick local PyTorch inference or HTTP classify based on active model.

Called from `RunController` so the run loop doesn't need to know which backend
is active. **The active model alone decides the backend:**
  - A ConvNeXt model is active → local inference, always
  - An openai-mode model is active → HTTP via `api_client.classify`, using
    **that model's own** `ai_model_config` — never the app-level AI Config
  - AI Config mode (no active model) → HTTP with the app-level config

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

from .. import paths
from ..data.db import Database
from ..data.models import Model, is_openai_model
from ..data.repository import ModelRepo, SettingsRepo
from . import api_client, local_inference


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

    False for an openai-mode model: it classifies over HTTP, so it needs no
    PyTorch (the torch gate keys off this) and no inference device.
    """
    model = active_model(db)
    return model is not None and not is_openai_model(model)


def checkpoint_problem(db: Database | None) -> str | None:
    """A user-facing explanation of why the active model can't classify.

    None when there's nothing wrong — AI Config mode, an openai-mode model
    (no checkpoint to miss), or a local model whose checkpoint is both present
    and loadable by the installed PyTorch. The UI uses this to refuse *before*
    the machine feeds a case; `classify_active` raises the same text as a
    backstop for a checkpoint that disappears mid-run.
    """
    model = active_model(db)
    # An openai-mode model classifies over HTTP: no checkpoint to find, and
    # no local torch to meet a floor with, so neither check below applies.
    if model is None or is_openai_model(model):
        return None
    if not has_local_checkpoint(model):
        return _checkpoint_detail(model)
    return torch_floor_problem(model)


def torch_floor_problem(model: Model | None) -> str | None:
    """Why the installed PyTorch can't load `model`, if it can't (issue #77).

    A checkpoint is readable by its own torch or a newer one, never an older
    one, so a model that records what it was built with imposes a floor. This
    is the pre-flight form of that: it answers before a case is fed, from the
    model row alone, where `local_inference._load` can only answer after the
    file has already failed to open.

    Silent — returns None — whenever it cannot be sure: no recorded floor
    (everything trained before #77), no torch installed (the install gate owns
    that), or a version either side that won't parse. A missing answer here
    costs a clearer message; a wrong one costs a run that was fine.
    """
    required = model.checkpoint_env.torch if model is not None else ""
    if not required:
        return None
    have = local_inference.installed_version()
    if have is None:
        return None
    from packaging.version import InvalidVersion, Version

    try:
        if Version(have) >= Version(required):
            return None
    except InvalidVersion:
        return None
    name = (model.name if model is not None else "") or "This model"
    return (
        f"“{name}” was trained with PyTorch {required}, and this machine has {have}.\n\n"
        "PyTorch reads a checkpoint written by its own version or an older one, never a "
        "newer one, so this model cannot be loaded until PyTorch is updated.\n\n"
        "Update it from the PyTorch prompt the app shows when a local model needs it, or "
        "switch to a different model on the Models page."
    )


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
    checkpoint is missing. Uses HTTP in AI Config mode (which includes
    `db is None`, for tests that don't need a database) with the app-level
    `api_cfg`, and for an active openai-mode model with **its own** config —
    the passed `api_cfg` is deliberately ignored there, so the app-level AI
    Config can never leak into a model that carries its own server settings.
    """
    model = active_model(db)
    if model is None:
        return api_client.classify(image_bgr, headstamps, api_cfg)
    if is_openai_model(model):
        return api_client.classify(image_bgr, headstamps, model.ai_model_config.to_dict())

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
