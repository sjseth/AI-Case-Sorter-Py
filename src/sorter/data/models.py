"""Typed dataclasses for SQLite-backed entities.

These mirror the legacy app's model / cartridge / headstamp / training-config
shapes closely enough to make ZIP import/export and community download
round-trips lossless. JSON-blob columns (`image_processing_json`,
`training_config_json`, `ai_model_config_json`) on the `models` table hold the
nested sub-objects so the SQL schema does not have to churn when those sub-
objects gain fields.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

SUPPORTED_MODEL_MODES = (
    "convnext_tiny",
    "convnext_small",
    "convnext_base",
    "convnext_large",
)

# A model that classifies over an OpenAI-compatible HTTP server instead of a
# local checkpoint. The legacy app treats "OpenAI API" as a Training Mode peer
# of the ConvNeXt sizes — several such models can coexist, each with its own
# cartridge, headstamp list and `AIModelConfig` (endpoint/key/model/prompt) —
# and this app mirrors that (PR #125 review). Deliberately NOT added to
# `SUPPORTED_MODEL_MODES`: that tuple doubles as the list of trainable
# backbones (`train_page` assigns `model_mode` straight into
# `training_config.model_name`), and an openai model has nothing to train.
OPENAI_MODEL_MODE = "openai"

# Every mode a model row may persist: the trainable backbones plus openai.
# `ModelRepo` validates against this, not `SUPPORTED_MODEL_MODES`.
MODEL_MODES = (*SUPPORTED_MODEL_MODES, OPENAI_MODEL_MODE)

MODEL_TYPES = ("Standard", "ReadOnly", "CommunityManaged")
FEEDBACK_UPLOAD_MODES = ("Instant", "OnRunComplete", "Manual")

# Slot templates are stored per run mode: package mode has its own set because
# it allows a headstamp in several slots (see SlotTemplate).
SLOT_TEMPLATE_MODES = ("standard", "package")


def normalize_upload_mode(raw: Any, *, feedback_enabled: bool) -> str:
    """Coerce a feedback upload-mode value to a canonical name string.

    Accepts the canonical name (any case), the legacy enum int
    (``Instant=0, OnRunComplete=1, Manual=2``), or a stringified int such as
    ``"0"``. Unrecognized/missing values fall back to the publisher's usual
    default (``Instant``) for a feedback-enabled model, else ``Manual``.

    Applied on read (``Model.from_row``) and on import so legacy rows that
    stored the raw enum int self-heal to the canonical string the upload-mode
    comparisons expect.
    """
    if isinstance(raw, bool):
        raw = None  # bool is an int subclass — never a valid mode
    if isinstance(raw, str):
        s = raw.strip()
        for mode in FEEDBACK_UPLOAD_MODES:
            if s.lower() == mode.lower():
                return mode
        if s.isdigit():
            raw = int(s)  # "0" -> 0, handled by the int branch below
    if isinstance(raw, int) and not isinstance(raw, bool):
        if 0 <= raw < len(FEEDBACK_UPLOAD_MODES):
            return FEEDBACK_UPLOAD_MODES[raw]
    return "Instant" if feedback_enabled else "Manual"


# Row ids are ``int``, not ``int | None``: SQLite rowids start at 1, so **0 is
# the "not persisted yet" sentinel** for every row dataclass below. The optional
# spelling was only load-bearing at the handful of sites that build a row before
# inserting it, while forcing the ~150 places that pass a *loaded* row's id to a
# repo method to re-prove it wasn't None. Genuinely nullable columns
# (``SlotTemplate.model_id`` = AI Config mode, ``Headstamp.parent_id`` =
# unassigned) keep their ``| None``.
@dataclass
class Cartridge:
    id: int = 0
    name: str = ""

    @classmethod
    def from_row(cls, row: Any) -> Cartridge:
        return cls(id=row["id"], name=row["name"])


@dataclass
class Headstamp:
    id: int = 0
    name: str = ""
    model_id: int = 0
    slot: int = 0
    # Parent classification grouping. None = unassigned (no parent). One parent
    # per headstamp.
    parent_id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> Headstamp:
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            model_id=row["model_id"],
            slot=row["slot"] if "slot" in keys else 0,
            parent_id=row["parent_id"] if "parent_id" in keys else None,
        )


@dataclass
class HeadstampParent:
    """A parent classification: a named group child headstamps roll up into.

    Scoped to a single model so two models can reuse the same parent name
    without collision. ``slot`` is
    the physical bin this parent routes to when the model runs in parent-
    classification mode (analogous to ``Headstamp.slot`` for child routing).
    """

    id: int = 0
    name: str = ""
    model_id: int = 0
    slot: int = 0

    @classmethod
    def from_row(cls, row: Any) -> HeadstampParent:
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            model_id=row["model_id"],
            slot=row["slot"] if "slot" in keys else 0,
        )


@dataclass
class SlotTemplate:
    """A named snapshot of the Run tab's slot assignments.

    Templates let one model carry several bin layouts (e.g. "Range brass" vs
    "Match prep") and switch between them without re-ticking every headstamp.

    Scoping: templates belong to a model (``model_id is None`` = AI Config
    mode) *and* to a ``mode``. Standard and package mode keep separate template
    lists because their assignment rules differ — standard mode routes a
    headstamp to exactly one slot, package mode allows the same headstamp in
    several slots at once, so a layout from one is meaningless in the other.

    ``assignments`` is the persisted payload, shaped per mode:
      standard: ``{"headstamps": {name: slot}, "parents": {name: slot}}``
      package:  ``{"slots": {"<slot>": [name, ...]}}``
    Names, not row ids, so a template survives a headstamp being deleted and
    re-added (e.g. a re-import). Unknown names are ignored when applied.
    """

    id: int = 0
    model_id: int | None = None
    mode: str = "standard"
    name: str = ""
    assignments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Any) -> SlotTemplate:
        import json

        try:
            assignments = json.loads(row["assignments_json"] or "{}")
        except (TypeError, ValueError):
            assignments = {}
        if not isinstance(assignments, dict):
            assignments = {}
        return cls(
            id=row["id"],
            model_id=row["model_id"],
            mode=row["mode"],
            name=row["name"],
            assignments=assignments,
        )


@dataclass
class ImageProcessingConfig:
    """Per-model image-processing settings.

    Distinct from the app-level `image_proc` settings: the app-level ones tune
    the default pipeline, this one overrides for a specific model when
    `Model.enable_image_processing` is true.
    """

    strategy: str = "hough"
    primer_mode: str = "hide"
    primer_radius: int = 135
    hough: dict[str, Any] = field(
        default_factory=lambda: {
            "dp": 2.0,
            "min_dist": 500,
            "param1": 100,
            "param2": 60,
            "min_radius": 150,
            "max_radius": 250,
        }
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ImageProcessingConfig:
        if not data:
            return cls()
        return cls(
            strategy=data.get("strategy", "hough"),
            primer_mode=data.get("primer_mode", "hide"),
            primer_radius=int(data.get("primer_radius", 135)),
            hough=dict(data.get("hough", cls().hough)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckpointEnv:
    """The library versions a model's checkpoint was built with (issue #77).

    PyTorch guarantees that a *newer* torch reads an *older* checkpoint, never
    the reverse — so what a model imposes on a machine is a **floor**, not a
    match, and this is the only place that floor is written down. Empty
    strings mean "not recorded": every checkpoint trained before this shipped,
    and everything the wider ecosystem produces.

    Recorded at training time and carried through the export manifest, so the
    floor survives a ZIP round trip and can be read without unpickling a
    checkpoint — which would need the very torch that may be too old.
    """

    torch: str = ""
    torchvision: str = ""
    numpy: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CheckpointEnv:
        """Read either spelling.

        A checkpoint payload spells these `torch_version` etc. — flat keys
        beside `classes` and `image_size` — while our own JSON nests them under
        their bare names. Both arrive here.
        """
        if not data:
            return cls()

        def _pick(name: str) -> str:
            return str(data.get(name) or data.get(f"{name}_version") or "")

        return cls(torch=_pick("torch"), torchvision=_pick("torchvision"), numpy=_pick("numpy"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_empty(self) -> bool:
        return not (self.torch or self.torchvision or self.numpy)


@dataclass
class AIModelConfig:
    """OpenAI-compatible HTTP endpoint settings, persisted per-model."""

    endpoint_url: str = ""
    api_key: str = ""
    model: str = ""
    prompt: str = ""
    image_quality: int = 100
    image_scale: int = 100

    # The legacy app's AI model config uses OpenAI_* keys; accept those
    # alongside our snake_case ones so a community-imported model picks up the
    # endpoint/prompt/quality settings on first activate.
    _WINFORMS_ALIASES = {
        "OpenAI_EndpointUrl": "endpoint_url",
        "OpenAI_APIKey": "api_key",
        "OpenAI_Model": "model",
        "OpenAI_SystemPrompt": "prompt",
        "ImageQuality": "image_quality",
        "ImageScale": "image_scale",
    }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AIModelConfig:
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        normalised: dict[str, Any] = {}
        for k, v in data.items():
            if k in known:
                normalised[k] = v
            elif k in cls._WINFORMS_ALIASES:
                normalised[cls._WINFORMS_ALIASES[k]] = v
        return cls(**normalised)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrainingConfig:
    """27 fields mirroring the legacy app's training config.

    Defaults follow the legacy defaults verbatim so an exported community
    model is round-trippable without coercion.
    """

    model_name: str = "convnext_tiny"
    image_directory: str = ""
    output_model_path: str = ""

    epochs: int = 10
    learning_rate: float = 1e-4
    batch_size: int = 32
    weight_decay: float = 1e-4
    val_split: float = 0.2
    dropout_rate: float = 0.0
    freeze_backbone: bool = False
    use_workspace: bool = False
    allow_gpu: bool = True
    max_workers: int = -1
    image_size: int = 232
    train_all: bool = False

    use_swa: bool = False
    swa_start: float = 0.75
    swa_mode: str = "scheduled"
    swa_acc_threshold: float = 0.96
    swa_patience: int = 5
    swa_min_epoch: int = 10

    use_focal_loss: bool = False
    focal_gamma: float = 1.0
    stochastic_depth_prob: float = -1.0

    use_parent_classifications: bool = False

    # Legacy PascalCase → our snake_case field name. Anything not in
    # this map can still come in via snake_case and will be matched directly.
    _WINFORMS_ALIASES = {
        "ModelName": "model_name",
        "ImageDirectory": "image_directory",
        "OutputModelPath": "output_model_path",
        "Epochs": "epochs",
        "LearningRate": "learning_rate",
        "BatchSize": "batch_size",
        "WeightDecay": "weight_decay",
        "ValSplit": "val_split",
        "DropoutRate": "dropout_rate",
        "FreezeBackbone": "freeze_backbone",
        "UseWorkspace": "use_workspace",
        "AllowGPU": "allow_gpu",
        "MaxWorkers": "max_workers",
        "ImageSize": "image_size",
        "TrainAll": "train_all",
        "UseSWA": "use_swa",
        "SWAStart": "swa_start",
        "SWAMode": "swa_mode",
        "SWAAccThreshold": "swa_acc_threshold",
        "SWAPatience": "swa_patience",
        "SWAMinEpoch": "swa_min_epoch",
        "UseFocalLoss": "use_focal_loss",
        "FocalGamma": "focal_gamma",
        "StochasticDepthProb": "stochastic_depth_prob",
        "UseParentClassifications": "use_parent_classifications",
    }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TrainingConfig:
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        normalised: dict[str, Any] = {}
        for k, v in data.items():
            if k in known:
                normalised[k] = v
            elif k in cls._WINFORMS_ALIASES:
                normalised[cls._WINFORMS_ALIASES[k]] = v
        return cls(**normalised)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Model:
    id: int = 0
    name: str = ""
    cartridge_id: int = 0
    model_mode: str = "convnext_tiny"
    model_type: str = "Standard"
    community_model_uid: str | None = None
    model_version: int = 1
    enable_image_processing: bool = True
    image_processing: ImageProcessingConfig = field(default_factory=ImageProcessingConfig)
    training_config: TrainingConfig = field(default_factory=TrainingConfig)
    ai_model_config: AIModelConfig = field(default_factory=AIModelConfig)
    use_primer_mask: bool = False
    hide_primer: bool = True
    primer_mask_size: int = 135
    last_training_date: str | None = None
    last_training_duration: int = 0
    trained_image_count: int = 0
    training_confusion_table: str | None = None
    feedback_loop_enabled: bool = False
    feedback_loop_confidence_floor: int = 95
    feedback_loop_upload_mode: str = "Manual"
    model_path: str | None = None
    checkpoint_env: CheckpointEnv = field(default_factory=CheckpointEnv)

    @classmethod
    def from_row(cls, row: Any) -> Model:
        import json

        def _parse(s: str | None) -> dict[str, Any] | None:
            if not s:
                return None
            try:
                return json.loads(s)
            except (TypeError, ValueError):
                return None

        def _optional(name: str) -> str | None:
            """A column that may predate the caller's schema.

            A `sqlite3.Row` raises IndexError for a column it doesn't carry,
            and a row can reach here from a query written before a migration
            added one.
            """
            try:
                return row[name]
            except (IndexError, KeyError):
                return None

        return cls(
            id=row["id"],
            name=row["name"],
            cartridge_id=row["cartridge_id"],
            model_mode=row["model_mode"],
            model_type=row["model_type"],
            community_model_uid=row["community_model_uid"],
            model_version=row["model_version"],
            enable_image_processing=bool(row["enable_image_processing"]),
            image_processing=ImageProcessingConfig.from_dict(_parse(row["image_processing_json"])),
            training_config=TrainingConfig.from_dict(_parse(row["training_config_json"])),
            ai_model_config=AIModelConfig.from_dict(_parse(row["ai_model_config_json"])),
            use_primer_mask=bool(row["use_primer_mask"]),
            hide_primer=bool(row["hide_primer"]),
            primer_mask_size=row["primer_mask_size"],
            last_training_date=row["last_training_date"],
            last_training_duration=row["last_training_duration"],
            trained_image_count=row["trained_image_count"],
            training_confusion_table=row["training_confusion_table"],
            feedback_loop_enabled=bool(row["feedback_loop_enabled"]),
            feedback_loop_confidence_floor=row["feedback_loop_confidence_floor"],
            feedback_loop_upload_mode=normalize_upload_mode(
                row["feedback_loop_upload_mode"],
                feedback_enabled=bool(row["feedback_loop_enabled"]),
            ),
            model_path=row["model_path"],
            # Added by migration 0006, so a row read through an older shape
            # simply has none — the same "not recorded" the dataclass means.
            checkpoint_env=CheckpointEnv.from_dict(_parse(_optional("checkpoint_env_json"))),
        )


# `model_type` values that mean "this model belongs to someone else". A
# download from the Community tab is stamped `CommunityManaged` by
# `model_io.import_model(..., community_download=True)`; `ReadOnly` is the
# legacy app's equivalent marker and is honoured the same way.
#
# NOTE: `community_model_uid` is deliberately NOT part of this test. Sharing
# your own model stamps a UID onto your local copy (`dialog_share_model`), so
# a UID means "this model exists in the community", not "this model is not
# yours". Ownership is decided by how the model arrived on *this* machine.
FOREIGN_MODEL_TYPES = frozenset({"CommunityManaged", "ReadOnly"})


def is_foreign_model(model: Model | None) -> bool:
    """True when `model` was installed from the community rather than authored here."""
    return bool(model is not None and model.model_type in FOREIGN_MODEL_TYPES)


def is_openai_model(model: Model | None) -> bool:
    """True when `model` classifies over an OpenAI-compatible HTTP server.

    Such a model has no checkpoint, needs no PyTorch, and carries its server
    settings in its own `ai_model_config` — the AI Config page edits them
    while the model is active.
    """
    return bool(model is not None and model.model_mode == OPENAI_MODEL_MODE)


def is_trainable(model: Model | None) -> bool:
    """Can this model be trained (and have training images added) locally?

    False for community downloads. The local checkpoint is a copy of the
    publisher's: retraining it forks it away from the version they keep
    updating, the archive usually ships without the training images the
    model was built from, and the next published update would overwrite the
    result anyway. Users who want to build on someone else's model export it
    and import it back as their own.

    Also False for an openai-mode model, whatever its ownership: there is no
    local checkpoint to train — the "model" is an HTTP server configuration.
    """
    return model is not None and not is_foreign_model(model) and not is_openai_model(model)
