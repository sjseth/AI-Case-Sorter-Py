"""Model ZIP export/import compatible with the legacy app's format.

ZIP layout (same as the legacy app's model export):

    manifest.json
    model/<filename>.pth        (optional — only when exporting model data)
    images/<label>__<ticks>.jpg (optional — only when exporting training images)

The manifest is a flat JSON document mirroring the legacy export shape closely
enough for round-trips with community downloads:

    {
      "ModelName": "9mm-default",
      "CartridgeName": "9mm",
      "Headstamps": ["WIN", "FC", "BLANK"],
      "ExportMode": "ModelAndImages",
      "ModelInfo": { ...Model.to_export_dict()... }
    }

Critical: ZIP entry names from a Windows-produced archive use backslashes
(`images\\foo.jpg`). On Linux this means a single flat filename containing
backslashes, which breaks `Path` ops. Every read normalizes to forward
slashes before any path manipulation. Path-traversal entries (``..``) are
rejected.
"""

from __future__ import annotations

import enum
import json
import os
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from .. import paths
from .models import (
    MODEL_MODES,
    OPENAI_MODEL_MODE,
    AIModelConfig,
    CheckpointEnv,
    Headstamp,
    ImageProcessingConfig,
    Model,
    TrainingConfig,
    is_foreign_model,
    normalize_upload_mode,
)
from .repository import (
    CartridgeRepo,
    HeadstampRepo,
    ModelRepo,
)


class ExportMode(str, enum.Enum):
    MODEL_ONLY = "ModelOnly"
    MODEL_AND_IMAGES = "ModelAndImages"
    IMAGES_ONLY = "ImagesOnly"


_VALID_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
# Model checkpoints are PyTorch ``.pth``/``.pt`` files. The empty string is
# tolerated because the extractor already defaults an extension-less entry to
# ``.pth`` (some exporters drop the suffix).
_VALID_MODEL_EXTS = {".pth", ".pt", ""}
# Legacy community downloads name the PyTorch checkpoint
# ``model/trainedmodel.zip``. PyTorch checkpoints are ZIP containers internally,
# and ``torch.load`` does not require a .pth suffix. We accept only this exact
# legacy basename, then normalize it to ``<model_id>.pth`` during extraction.
_LEGACY_ZIP_MODEL_NAME = "trainedmodel.zip"
# A wildly high uncompressed:compressed ratio is the signature of a zip bomb;
# real JPEGs and float32 checkpoints are already near-incompressible. Only large
# entries are checked so small, legitimately-compressible files (a tiny JSON
# manifest) never trip the guard.
_MAX_COMPRESSION_RATIO = 100
_RATIO_CHECK_MIN_BYTES = 1_000_000


def _normalize(entry_name: str) -> str:
    """Convert any backslash-separated entry name into a POSIX-style path."""
    return entry_name.replace("\\", "/").lstrip("/")


def _is_traversal(rel: str) -> bool:
    p = PurePosixPath(rel)
    return any(part == ".." for part in p.parts)


def model_to_export_dict(m: Model) -> dict[str, Any]:
    """Serialize a Model for the manifest. Strips paths & secrets."""
    d = asdict(m)
    # Strip absolute paths so importers compute their own.
    d.pop("model_path", None)
    if d.get("training_config"):
        d["training_config"].pop("image_directory", None)
        d["training_config"].pop("output_model_path", None)
    if d.get("ai_model_config"):
        d["ai_model_config"].pop("api_key", None)
    return d


# The legacy app serialises its ModelMode enum as its integer value. Map back
# to our snake_case backbone identifiers; non-ConvNeXt modes fall through to a
# ConvNeXt this app can actually run.
WINFORMS_MODELMODE_OPENAI = 2

_WINFORMS_MODELMODE_INT_TO_STR = {
    0: "convnext_tiny",  # DeepLearning (ResNet50) — can't run, fall back
    1: "convnext_tiny",  # Inception           — fall back
    # "OpenAI API" is a first-class mode here too (PR #125 review): the row
    # keeps its own AIModelConfig and classifies over HTTP when active.
    WINFORMS_MODELMODE_OPENAI: OPENAI_MODEL_MODE,
    3: "convnext_large",
    4: "convnext_tiny",  # DeeperLearning (ResNet101) — fall back
    5: "convnext_tiny",  # Custom              — fall back
    6: "convnext_base",
    7: "convnext_small",
    8: "convnext_tiny",
}


def _normalize_model_mode(raw: Any) -> str:
    """Accept the mode string or the legacy enum int.

    Every branch lands in `MODEL_MODES`: `ModelRepo` rejects anything else,
    and `winforms_import` hands the result straight to it.
    """
    if isinstance(raw, str):
        rl = raw.strip().lower()
        # Tolerate hyphenated/CamelCase variants
        # (`ConvNeXt-Tiny`, `convnext_tiny`, `ConvNeXtTiny`).
        rl = rl.replace("-", "_").replace(" ", "_")
        if rl in MODEL_MODES:
            return rl
        # ConvNeXtTiny → convnext_tiny
        if rl.startswith("convnext") and not rl.startswith("convnext_"):
            tail = rl[len("convnext") :]
            candidate = f"convnext_{tail}"
            if candidate in MODEL_MODES:
                return candidate
        return "convnext_tiny"
    if isinstance(raw, int):
        return _WINFORMS_MODELMODE_INT_TO_STR.get(raw, "convnext_tiny")
    return "convnext_tiny"


def _normalize_model_type(raw: Any) -> str:
    """ModelType is an enum in the legacy app (Standard=0, ReadOnly=1,
    CommunityManaged=2); this app persists the name. Map ints back to names."""
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, int):
        return ("Standard", "ReadOnly", "CommunityManaged")[raw] if 0 <= raw <= 2 else "Standard"
    return "Standard"


def _g(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Lookup helper that accepts multiple key spellings.

    Legacy manifests use PascalCase property names (e.g. `ModelMode`,
    `PythonTrainingConfig`); this app exports use snake_case (e.g. `model_mode`,
    `training_config`). This walks the candidates in order so callers can pass
    both spellings.
    """
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def model_from_export_dict(d: dict[str, Any]) -> Model:
    """Reverse of `model_to_export_dict`. Tolerates missing fields and
    accepts both snake_case and legacy PascalCase manifests.
    """
    if not d:
        return Model()
    image_processing = ImageProcessingConfig.from_dict(_g(d, "image_processing", "ImageProcessingConfig"))
    # The legacy app persists two training_configs: PythonTrainingConfig (for
    # the python training pipeline — what we care about) and
    # ModelTrainingConfig (for the legacy ML.NET pipeline). Prefer the
    # python one because it carries the ImageSize the model was actually
    # trained at.
    training_config = TrainingConfig.from_dict(_g(d, "training_config", "PythonTrainingConfig", "ModelTrainingConfig"))
    ai_model_config = AIModelConfig.from_dict(_g(d, "ai_model_config", "AIModelConfig"))
    fb_enabled = bool(_g(d, "feedback_loop_enabled", "FeedbackLoopEnabled", default=False))
    return Model(
        id=0,
        name=_g(d, "name", "Name", default=""),
        cartridge_id=0,
        model_mode=_normalize_model_mode(_g(d, "model_mode", "ModelMode")),
        model_type=_normalize_model_type(_g(d, "model_type", "ModelType", default="Standard")),
        community_model_uid=_g(d, "community_model_uid", "CommunityModelUID"),
        model_version=int(_g(d, "model_version", "ModelVersion", default=1)),
        enable_image_processing=bool(_g(d, "enable_image_processing", "EnableImageProcessing", default=True)),
        image_processing=image_processing,
        training_config=training_config,
        ai_model_config=ai_model_config,
        use_primer_mask=bool(_g(d, "use_primer_mask", "UsePrimerMask", default=False)),
        hide_primer=bool(_g(d, "hide_primer", "HidePrimer", default=True)),
        primer_mask_size=int(_g(d, "primer_mask_size", "PrimerMaskSize", default=135)),
        last_training_date=_g(d, "last_training_date", "LastTrainingDate"),
        last_training_duration=int(_g(d, "last_training_duration", "LastTrainingDuration", default=0)),
        trained_image_count=int(_g(d, "trained_image_count", "TrainedImageCount", default=0)),
        training_confusion_table=_g(d, "training_confusion_table", "TrainingConfusionTable"),
        feedback_loop_enabled=fb_enabled,
        feedback_loop_confidence_floor=int(
            _g(d, "feedback_loop_confidence_floor", "FeedbackLoopConfidenceFloor", default=95)
        ),
        feedback_loop_upload_mode=normalize_upload_mode(
            _g(d, "feedback_loop_upload_mode", "FeedbackLoopUploadMode"),
            feedback_enabled=fb_enabled,
        ),
        model_path=None,
        # Absent from every archive written before #77 and from anything the
        # legacy app exports, which is exactly the "not recorded" case.
        checkpoint_env=CheckpointEnv.from_dict(_g(d, "checkpoint_env", "CheckpointEnv")),
    )


def export_model(
    output_path: Path | str,
    model: Model,
    cartridge_name: str,
    headstamps: list[Headstamp] | list[str],
    *,
    mode: ExportMode = ExportMode.MODEL_AND_IMAGES,
    model_file: Path | str | None = None,
    images_dir: Path | str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Write a ZIP archive at `output_path`.

    Uses the atomic-write pattern (`.tmp` + replace) so a partial export
    cannot leave a corrupt ZIP on disk.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")

    hs_names = [h if isinstance(h, str) else h.name for h in headstamps]
    manifest = {
        "ModelName": model.name,
        "CartridgeName": cartridge_name,
        "Headstamps": hs_names,
        "ExportMode": mode.value,
        "ModelInfo": model_to_export_dict(model),
    }

    image_files: list[Path] = []
    if mode in (ExportMode.IMAGES_ONLY, ExportMode.MODEL_AND_IMAGES) and images_dir:
        d = Path(images_dir)
        if d.exists():
            image_files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _VALID_IMAGE_EXTS)

    include_model = (
        mode in (ExportMode.MODEL_ONLY, ExportMode.MODEL_AND_IMAGES)
        and model_file is not None
        and Path(model_file).exists()
    )

    total = 1 + (1 if include_model else 0) + len(image_files)
    step = 0

    def _bump() -> None:
        nonlocal step
        step += 1
        if progress is not None:
            progress(step, total)

    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            _bump()
            if include_model:
                model_arcname = f"model/{Path(model_file).name}"
                zf.write(str(model_file), arcname=model_arcname)
                _bump()
            for img in image_files:
                zf.write(str(img), arcname=f"images/{img.name}")
                _bump()
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            with _suppressed(OSError):
                tmp.unlink()
    return out


class _suppressed:
    """Inline contextlib.suppress (avoids the import for this single use)."""

    def __init__(self, *exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        return t is not None and issubclass(t, self.exc)


def read_manifest(zip_path: Path | str) -> dict[str, Any]:
    """Quick peek at a model archive's manifest.json without extracting."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        for entry in zf.infolist():
            rel = _normalize(entry.filename)
            if rel == "manifest.json":
                with zf.open(entry) as f:
                    return json.loads(f.read().decode("utf-8"))
    raise ValueError(f"{zip_path}: no manifest.json found")


def write_manifest_sidecar(zip_path: Path | str) -> Path:
    """Extract manifest.json from a model ZIP and write it as a sibling
    ``<name>.manifest.json``.

    Pulls the manifest back out of the archive and serialises it to its own
    file so it can be uploaded by itself once the model completes.
    """
    manifest = read_manifest(zip_path)
    sidecar = Path(zip_path).with_suffix(".manifest.json")
    sidecar.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return sidecar


def export_for_share(
    output_zip: Path | str,
    model: Model,
    cartridge_name: str,
    headstamps: list[Headstamp] | list[str],
    *,
    mode: ExportMode = ExportMode.MODEL_AND_IMAGES,
    model_file: Path | str | None = None,
    images_dir: Path | str | None = None,
    community_uid: str,
    feedback_enabled: bool = False,
    feedback_floor: int = 0,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, Path]:
    """Build a community-share ZIP plus its standalone manifest sidecar.

    Stamps the community fields into the manifest's ModelInfo exactly as the
    legacy community export does (CommunityModelUID = the archive's UID,
    feedback-loop enable/floor, and FeedbackLoopUploadMode=Instant when the
    publisher turns the loop on). Returns ``(zip_path, manifest_path)``.
    """
    import copy

    m = copy.deepcopy(model)
    m.community_model_uid = community_uid
    m.feedback_loop_enabled = bool(feedback_enabled)
    m.feedback_loop_confidence_floor = int(feedback_floor)
    if feedback_enabled:
        m.feedback_loop_upload_mode = "Instant"
    zip_path = export_model(
        output_zip,
        m,
        cartridge_name,
        headstamps,
        mode=mode,
        model_file=model_file,
        images_dir=images_dir,
        progress=progress,
    )
    manifest_path = write_manifest_sidecar(zip_path)
    return zip_path, manifest_path


def _unique_model_name(base: str, repo: ModelRepo) -> str:
    """Append (n) until the name is unique across all models."""
    existing = {m.name.lower() for m in repo.list()}
    if base.lower() not in existing:
        return base
    n = 2
    while f"{base} ({n})".lower() in existing:
        n += 1
    return f"{base} ({n})"


def find_update_target(zip_path: Path | str, *, db: Any) -> Model | None:
    """Return the installed model this archive would update in place.

    ``None`` when the archive isn't a community model, or is one that isn't
    installed yet — i.e. when importing it would create a new model.
    """
    info = read_manifest(zip_path).get("ModelInfo") or {}
    uid = _g(info, "community_model_uid", "CommunityModelUID")
    if not uid:
        return None
    return ModelRepo(db).find_by_community_uid(str(uid))


def _merge_onto_installed(incoming: Model, existing: Model, *, name: str) -> Model:
    """Fold an archive's metadata onto the row that's already installed.

    The archive wins for everything that describes the model itself —
    version, mode, preprocessing, training metadata — because after an
    update the model has to behave like a fresh download of the same file.
    The install wins for what belongs to this machine: the name the user
    gave it, their feedback-loop choice, and any locally-configured API
    credentials (the archive's are stripped on export, so taking them would
    wipe the user's). `cartridge_id` is resolved by the caller.

    Everything keyed off the model **id** — its ``images/``,
    ``trainedmodel/``, headstamp rows and their slots, and its sorting
    templates — survives simply because the row keeps its id.
    """
    incoming.id = existing.id
    incoming.name = name
    incoming.ai_model_config = existing.ai_model_config
    # Ownership never downgrades on update. Without this, re-importing a
    # community model from a plain ZIP (whose manifest says `Standard`, since
    # that's what the publisher's own copy is) would quietly hand the user
    # training rights on someone else's model.
    if is_foreign_model(existing):
        incoming.model_type = existing.model_type
    # Overwritten during extraction if the archive ships a checkpoint; keep
    # the installed one so an images-only archive doesn't orphan the model.
    incoming.model_path = existing.model_path
    # The env describes a checkpoint, so it follows the same rule: an archive
    # that records nothing (every pre-#77 export, and everything the legacy app
    # writes) must not erase what the installed copy already knows.
    if incoming.checkpoint_env.is_empty():
        incoming.checkpoint_env = existing.checkpoint_env
    # The publisher offers the feedback loop, the user opts in — an update
    # can't opt them back in, and can't keep it on if the publisher stopped
    # offering it.
    incoming.feedback_loop_enabled = bool(existing.feedback_loop_enabled and incoming.feedback_loop_enabled)
    if incoming.feedback_loop_enabled:
        incoming.feedback_loop_upload_mode = existing.feedback_loop_upload_mode
    return incoming


def import_model(
    zip_path: Path | str,
    *,
    db: Any,
    cartridge_name_override: str | None = None,
    model_name_override: str | None = None,
    images_target_dir: Path | str | None = None,
    models_target_dir: Path | str | None = None,
    update_existing: bool = True,
    community_download: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Import a model archive.

    Returns (cartridge_id, model_id).

    Pass ``community_download=True`` when the archive came from the Community
    tab. That marks the installed model `CommunityManaged`, which is what
    makes it read-only for training — it belongs to whoever published it.
    The default is False because a plain ZIP import is just as likely to be
    the user restoring their *own* model onto a new machine, and that model
    must stay trainable.

    An archive that carries a community UID already installed locally is an
    **update of that model**, not a new one: with ``update_existing`` (the
    default) the installed row is refreshed in place, so the library doesn't
    grow a duplicate and the user keeps their slot assignments and sorting
    templates. Pass ``update_existing=False`` to force a separate copy.
    Use `find_update_target` to know which path an archive will take before
    committing to it.

    `images_target_dir` and `models_target_dir` are optional overrides; by
    default they resolve to ``paths.model_images_dir(id)`` and
    ``paths.model_trained_dir(id)`` for the imported model.
    """
    cart_repo = CartridgeRepo(db)
    model_repo = ModelRepo(db)
    headstamp_repo = HeadstampRepo(db)

    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = list(zf.infolist())

        # Pre-scan: reject anything dangerous before we touch the FS —
        # path-traversal entries, decompression bombs, and entries whose
        # extension isn't one we expect to extract.
        for entry in entries:
            rel = _normalize(entry.filename)
            if _is_traversal(rel):
                raise ValueError(f"Refusing to import {zip_path}: traversal entry {entry.filename!r}")
            if (
                entry.file_size >= _RATIO_CHECK_MIN_BYTES
                and entry.compress_size > 0
                and entry.file_size / entry.compress_size > _MAX_COMPRESSION_RATIO
            ):
                raise ValueError(
                    f"Refusing to import {zip_path}: entry {entry.filename!r} has an "
                    f"implausible compression ratio "
                    f"({entry.file_size} / {entry.compress_size})"
                )
            posix = PurePosixPath(rel)
            if rel.endswith("/") or rel == "manifest.json":
                continue
            if posix.parts and posix.parts[0] == "images":
                if Path(posix.parts[-1]).suffix.lower() not in _VALID_IMAGE_EXTS:
                    raise ValueError(f"Refusing to import {zip_path}: unexpected image entry {entry.filename!r}")
            elif posix.parts and posix.parts[0] == "model":
                model_basename = posix.parts[-1]
                suffix = Path(model_basename).suffix.lower()
                is_legacy_zip = model_basename.lower() == _LEGACY_ZIP_MODEL_NAME
                if suffix not in _VALID_MODEL_EXTS and not is_legacy_zip:
                    raise ValueError(f"Refusing to import {zip_path}: unexpected model entry {entry.filename!r}")

        manifest_entry = next((e for e in entries if _normalize(e.filename) == "manifest.json"), None)
        if manifest_entry is None:
            raise ValueError(f"{zip_path}: missing manifest.json")
        manifest = json.loads(zf.read(manifest_entry).decode("utf-8"))

        model = model_from_export_dict(manifest.get("ModelInfo") or {})
        if model.model_mode not in MODEL_MODES:
            model.model_mode = "convnext_tiny"

        # Ownership is decided by how the archive reached this machine, not by
        # what its manifest claims. A publisher's own copy is `Standard` and
        # that's what they export, so trusting the manifest would hand every
        # downloader a trainable model.
        if community_download:
            model.model_type = "CommunityManaged"

        # Is this archive an update of something already installed?
        existing = (
            model_repo.find_by_community_uid(model.community_model_uid)
            if update_existing and model.community_model_uid
            else None
        )

        # Resolve the cartridge. An in-place update stays where the user
        # filed it, so we don't move the model in the library — and don't
        # strand a new empty cartridge either — unless the caller asked for
        # a specific one.
        if existing is not None and not cartridge_name_override:
            cart_id = existing.cartridge_id
        else:
            cart_name = cartridge_name_override or manifest.get("CartridgeName") or "Imported"
            cart_id = cart_repo.get_or_create(cart_name).id
        model.cartridge_id = cart_id

        # Reset training metadata unless we're replacing a community model.
        if not model.community_model_uid:
            model.last_training_date = None
            model.last_training_duration = 0
            model.trained_image_count = 0
            model.training_confusion_table = None

        if existing is not None:
            saved = _merge_onto_installed(
                model,
                existing,
                name=model_name_override or existing.name,
            )
            model_repo.update(saved)
        else:
            desired_name = model_name_override or model.name or "Imported"
            model.name = _unique_model_name(desired_name, model_repo)
            saved = model_repo.create(model)

        # Decide target directories (per-model `<id>/images` and
        # `<id>/trainedmodel` under `data/models/`).
        img_dir = Path(images_target_dir) if images_target_dir else paths.model_images_dir(saved.id)
        mod_dir = Path(models_target_dir) if models_target_dir else paths.model_trained_dir(saved.id)
        img_dir.mkdir(parents=True, exist_ok=True)
        mod_dir.mkdir(parents=True, exist_ok=True)

        for hs_name in manifest.get("Headstamps") or []:
            if hs_name:
                with _suppressed(Exception):
                    headstamp_repo.add(saved.id, hs_name)

        # Extract files
        progress_total = len(entries)
        for i, entry in enumerate(entries, 1):
            rel = _normalize(entry.filename)
            if rel.endswith("/") or rel == "manifest.json":
                if progress:
                    progress(i, progress_total)
                continue
            posix = PurePosixPath(rel)
            if posix.parts and posix.parts[0] == "images":
                dest = img_dir / posix.parts[-1]
                _extract_to(zf, entry, dest)
            elif posix.parts and posix.parts[0] == "model":
                model_basename = posix.parts[-1]
                # Standardize on `<model_id>.pth` so the runtime knows where to look
                # regardless of what the exporter named the file.
                suffix = Path(model_basename).suffix.lower()
                if model_basename.lower() == _LEGACY_ZIP_MODEL_NAME:
                    suffix = ".pth"
                elif not suffix:
                    suffix = ".pth"
                dest = mod_dir / f"{saved.id}{suffix}"
                _extract_to(zf, entry, dest)
                saved.model_path = str(dest)
                model_repo.update(saved)
            if progress:
                progress(i, progress_total)

        return saved.cartridge_id, saved.id


def _extract_to(zf: zipfile.ZipFile, entry: zipfile.ZipInfo, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(entry) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
