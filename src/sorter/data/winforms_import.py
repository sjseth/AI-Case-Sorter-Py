"""One-shot import of an existing WinForms ("AI Brass Sorter") installation.

The legacy Windows app keeps **everything in its own install directory** — there
is no per-user data folder and, despite appearances, nothing usable in the
registry (see `find_installation`). The layout, as shipped by the 1.3.8 MSI:

    <root>/Data/ConfigDB.sjdb.json      the whole database, one JSON document
    <root>/Data/Settings.json           three app-level toggles incl. baud rate
    <root>/training/images/<id>/        training images, `{label}__{ticks}.jpg`
    <root>/training/models/<id>.zip     the trained checkpoint for model <id>

`ConfigDB.sjdb.json` is a single object whose top-level keys are the tables:
`Models`, `Headstamps`, `Cartridges`, `HeadStampParents`, `HeadStampParentLinks`,
`SlotConfigs`, `SortingTemplates`, `CommunityNotes` and a `Defaults` blob holding
the app-level settings. Model rows use the **same PascalCase shape** as the
`ModelInfo` block of an exported model ZIP, which is why this module can hand
them straight to `model_io.model_from_export_dict` instead of re-parsing them.

**`training/models/<id>.zip` is not a ZIP of anything.** It is a raw
`torch.save` archive — which is itself a zip container — so it drops in as our
`<id>.pth` with a plain copy. The legacy app also ships an **ML.NET** pipeline
whose models live beside it under the same `.zip` extension (a
`TransformerChain/` tree wrapping a frozen TensorFlow graph). Those cannot
classify here, so `_checkpoint_kind` looks inside before copying and a model
that only has an ML.NET checkpoint is imported as a **shell**: metadata,
headstamps and images come across, and the user retrains into it. Importing it
anyway is the point — the images are the expensive part, and our
`NoLocalCheckpointError` path already explains a model that cannot classify yet.

Everything here is **read-only with respect to the source install**: files are
copied out, nothing is moved, renamed or deleted. The legacy app keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import paths

# The two per-model settings keys are private to `config`, and imported rather
# than re-spelled here: a key written under a name `Config` does not read back
# is a silently-dropped import, which no test of this module would catch.
from .config import (
    _PACKAGE_SLOTS_KEY,
    _USE_PARENT_RUNTIME_KEY,
    DEFAULT_INIT_SETTINGS,
    Config,
)
from .db import Database
from .model_io import WINFORMS_MODELMODE_OPENAI, model_from_export_dict
from .models import AIModelConfig, Model
from .repository import (
    CartridgeRepo,
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)

log = logging.getLogger(__name__)

# Relative layout inside a legacy install root.
CONFIG_DB_NAME = "Data/ConfigDB.sjdb.json"
SETTINGS_NAME = "Data/Settings.json"
IMAGES_SUBDIR = "training/images"
MODELS_SUBDIR = "training/models"

# Where the MSI puts the app. The registry is deliberately not consulted: the
# uninstall entry's `InstallLocation` is empty, `HKCU\Software\AICaseSorter`
# exists but holds no values at all, and the only thing under
# `HKCU\Software\SJSeth\...` is an MSI-authored `DesktopFolder` path. Nothing
# there records where the app was installed or anything a user would want back,
# so a custom install is handled by letting the user point at the folder.
_INSTALL_SUBPATH = ("SJSeth", "AI Brass Sorter")

# Only these land in `data/models/<id>/images/`. The legacy app writes JPEGs;
# the rest are here because its own importer accepted them.
_VALID_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Which legacy model ids an install has already brought across, so a second run
# updates rather than duplicates. Keyed by install root so two machines' folders
# imported in turn cannot collide on legacy id.
_IMPORTED_MODELS_KEY = "winforms_imported_models"
# Set once the first-run offer has been made, whether accepted or declined.
FIRST_RUN_SEEN_KEY = "winforms_import_offered"

# `Defaults.IP_*` → our app-level `image_proc.linescan`. There is deliberately
# no Hough mapping: the legacy pipeline has no Hough stage, so its numbers mean
# nothing to ours and inventing a conversion would silently detune a working
# crop. `strategy` is left alone for the same reason.
_LINESCAN_FROM_LEGACY = {
    "scan_precision": ("IP_Resolution", int),
    "scan_sensitivity": ("IP_Sensitivity", float),
    "padding_pct": ("IP_Padding", int),
    "bg_cliff": ("IP_BgCliff", int),
}

# What the user is told about a model whose only checkpoint is the legacy
# ML.NET pipeline's. A named constant rather than an inline f-string so a test
# can assert the exact message instead of sniffing for a substring of it —
# CodeQL reads `"ML.NET" in some_string` as a hostname allowlist check
# (`.NET` parses as a TLD) and flags it as incomplete URL sanitization.
MLNET_WARNING = (
    "'{name}' has only an ML.NET checkpoint, which cannot classify here — "
    "it is imported without one so you can retrain it."
)

# One unreadable row costs that row, not the whole import.
MODEL_FAILED_WARNING = "Could not import '{name}': {error}"

# Every item the user can tick, in the order the dialog shows them — roughly by
# how much work each one saves.
ITEM_MODELS = "models"
ITEM_IMAGES = "training_images"
ITEM_HEADSTAMPS = "headstamps"
ITEM_IMAGE_PROC = "image_processing"
ITEM_SERIAL = "serial"
ITEM_AI_CONFIG = "ai_config"


@dataclass
class ImportOptions:
    """Which checkboxes were ticked.

    `training_images` and `headstamps` are meaningless without `models` — the
    images land in a model's folder and the headstamps hang off a model row —
    so `normalized()` folds them off rather than making the dialog police it.
    """

    models: bool = True
    training_images: bool = True
    headstamps: bool = True
    image_processing: bool = False
    serial: bool = False
    ai_config: bool = False

    def normalized(self) -> ImportOptions:
        if self.models:
            return self
        return ImportOptions(
            models=False,
            training_images=False,
            headstamps=False,
            image_processing=self.image_processing,
            serial=self.serial,
            ai_config=self.ai_config,
        )

    def any_selected(self) -> bool:
        n = self.normalized()
        return any((n.models, n.image_processing, n.serial, n.ai_config))


@dataclass
class LegacyModel:
    """One `Models` row plus what the filesystem says about it."""

    legacy_id: int
    name: str
    raw: dict[str, Any]
    cartridge_name: str
    image_count: int = 0
    checkpoint: Path | None = None
    checkpoint_kind: str = "none"  # "torch" | "mlnet" | "none"
    headstamp_count: int = 0

    @property
    def has_usable_checkpoint(self) -> bool:
        return self.checkpoint_kind == "torch" and self.checkpoint is not None

    @property
    def was_openai_mode(self) -> bool:
        """True when the legacy row classified over HTTP rather than locally."""
        mode = self.raw.get("ModelMode")
        if isinstance(mode, bool):  # bool is an int; a True here is not mode 1
            return False
        if isinstance(mode, int):
            return mode == WINFORMS_MODELMODE_OPENAI
        return isinstance(mode, str) and mode.strip().lower() == "openai"


@dataclass
class LegacySurvey:
    """What an install root offers, without importing any of it.

    Built by `survey()` and handed to the dialog so each checkbox can say how
    much it would bring across. `is_empty` is what keeps the first-run offer
    silent for an install holding nothing.
    """

    root: Path
    models: list[LegacyModel] = field(default_factory=list)
    has_serial: bool = False
    has_image_processing: bool = False
    has_ai_config: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def total_images(self) -> int:
        return sum(m.image_count for m in self.models)

    @property
    def total_headstamps(self) -> int:
        return sum(m.headstamp_count for m in self.models)

    @property
    def is_empty(self) -> bool:
        return not (self.models or self.has_serial or self.has_image_processing or self.has_ai_config)


@dataclass
class ImportResult:
    models_imported: int = 0
    models_updated: int = 0
    images_copied: int = 0
    headstamps_imported: int = 0
    parents_imported: int = 0
    slots_assigned: int = 0
    checkpoints_copied: int = 0
    serial_imported: bool = False
    image_processing_imported: bool = False
    ai_config_imported: bool = False
    activated_model_id: int | None = None
    warnings: list[str] = field(default_factory=list)


# ----- discovery --------------------------------------------------------------


def candidate_install_dirs() -> list[Path]:
    """Well-known places the MSI lays the app down, most likely first.

    `%ProgramFiles%` is read from the environment rather than hard-coded so a
    machine with a relocated Program Files still resolves, and the 32-bit view
    is included because the same MSI has shipped both ways.
    """
    roots: list[Path] = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    if not roots:  # non-Windows, or a stripped environment
        roots = [Path("C:/Program Files"), Path("C:/Program Files (x86)")]
    seen: set[Path] = set()
    out: list[Path] = []
    for base in roots:
        candidate = base.joinpath(*_INSTALL_SUBPATH)
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def looks_like_installation(root: Path | str) -> bool:
    """True when `root` holds the one file an import cannot proceed without."""
    try:
        return (Path(root) / CONFIG_DB_NAME).is_file()
    except OSError:
        return False


def find_installation() -> Path | None:
    """The first candidate that actually holds a config DB, or None.

    Returning None is the normal case — a user who never ran the Windows app
    must never see any of this (issue #98's last acceptance criterion).
    """
    for candidate in candidate_install_dirs():
        if looks_like_installation(candidate):
            return candidate
    return None


# ----- reading the legacy database -------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    """Parse one of the legacy JSON files.

    `utf-8-sig`: the app writes these with a BOM, which plain `utf-8` would
    leave stuck to the front of the first key.
    """
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _rows(db: dict[str, Any], table: str) -> list[dict[str, Any]]:
    value = db.get(table)
    if not isinstance(value, list):
        return []
    return [r for r in value if isinstance(r, dict)]


def _checkpoint_kind(path: Path) -> str:
    """Classify a `training/models/<id>.zip`.

    A `torch.save` archive holds `<something>/data.pkl`; the legacy ML.NET
    pipeline writes a `TransformerChain/` tree instead. Anything unreadable is
    "none" — a corrupt file must not stop the rest of the import.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return "none"
    for name in names:
        if name.replace("\\", "/").rsplit("/", 1)[-1] == "data.pkl":
            return "torch"
    for name in names:
        if name.replace("\\", "/").startswith("TransformerChain/"):
            return "mlnet"
    return "none"


def _count_images(directory: Path) -> int:
    try:
        return sum(1 for p in directory.iterdir() if p.is_file() and p.suffix.lower() in _VALID_IMAGE_EXTS)
    except OSError:
        return 0


def _legacy_ai_config(row: dict[str, Any]) -> dict[str, Any] | None:
    """One model's `AIModelConfig`, or None when there is nothing worth taking.

    "Present" is not enough: the legacy app writes the blob on every model, so
    an install's first row is usually an all-blank one. Treating that as the
    config would let it win over the model that actually holds the endpoint and
    report a successful import that changed nothing.
    """
    value = row.get("AIModelConfig")
    if not isinstance(value, dict) or not value:
        return None
    parsed = AIModelConfig.from_dict(value)
    if not (parsed.endpoint_url or parsed.model or parsed.prompt):
        return None
    return value


def survey(root: Path | str) -> LegacySurvey:
    """Read an install root and report what could be imported.

    Never raises for a merely-incomplete install: a missing `Settings.json`, an
    absent images folder or a model with no checkpoint are all normal and become
    counts of zero. A missing or unparseable `ConfigDB.sjdb.json` is the one
    hard error, because without it there is nothing to import at all.
    """
    root = Path(root)
    config_path = root / CONFIG_DB_NAME
    if not config_path.is_file():
        raise FileNotFoundError(f"{root}: no {CONFIG_DB_NAME} — not an AI Brass Sorter installation")
    raw = _read_json(config_path)

    cartridges = {int(c.get("Id") or 0): str(c.get("Name") or "") for c in _rows(raw, "Cartridges")}
    headstamp_counts: dict[int, int] = {}
    for hs in _rows(raw, "Headstamps"):
        mid = int(hs.get("Model_Id") or 0)
        headstamp_counts[mid] = headstamp_counts.get(mid, 0) + 1

    out = LegacySurvey(root=root)
    for row in _rows(raw, "Models"):
        legacy_id = int(row.get("Id") or 0)
        if not legacy_id:
            continue
        checkpoint = root / MODELS_SUBDIR / f"{legacy_id}.zip"
        kind = _checkpoint_kind(checkpoint) if checkpoint.is_file() else "none"
        entry = LegacyModel(
            legacy_id=legacy_id,
            name=str(row.get("Name") or f"Model {legacy_id}"),
            raw=row,
            cartridge_name=cartridges.get(int(row.get("CartridgeId") or 0)) or "Imported",
            image_count=_count_images(root / IMAGES_SUBDIR / str(legacy_id)),
            checkpoint=checkpoint if kind == "torch" else None,
            checkpoint_kind=kind,
            headstamp_count=headstamp_counts.get(legacy_id, 0),
        )
        out.models.append(entry)
        # An openai-mode row needs no warning: "openai" is a first-class mode
        # here too, so it imports faithfully — config, headstamps and all.
        if kind == "mlnet" and not entry.was_openai_mode:
            out.warnings.append(MLNET_WARNING.format(name=entry.name))

    defaults = raw.get("Defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    out.has_serial = bool(defaults.get("DefaultSerialPort") or defaults.get("InitSettings"))
    out.has_image_processing = any(key in defaults for key, _ in _LINESCAN_FROM_LEGACY.values())
    out.has_ai_config = any(_legacy_ai_config(m.raw) for m in out.models)
    return out


# ----- the import -------------------------------------------------------------


def _imported_map(settings: SettingsRepo, root: Path) -> dict[str, int]:
    """legacy model id (as a string) → our model id, for this install root."""
    raw = settings.get(_IMPORTED_MODELS_KEY) or {}
    if not isinstance(raw, dict):
        return {}
    scoped = raw.get(str(root))
    if not isinstance(scoped, dict):
        return {}
    return {str(k): int(v) for k, v in scoped.items() if isinstance(v, int)}


def _remember_import(settings: SettingsRepo, root: Path, legacy_id: int, model_id: int) -> None:
    raw = settings.get(_IMPORTED_MODELS_KEY) or {}
    if not isinstance(raw, dict):
        raw = {}
    scoped = raw.get(str(root))
    if not isinstance(scoped, dict):
        scoped = {}
    scoped[str(legacy_id)] = int(model_id)
    raw[str(root)] = scoped
    settings.set(_IMPORTED_MODELS_KEY, raw)


def _find_existing(
    entry: LegacyModel,
    model: Model,
    *,
    model_repo: ModelRepo,
    remembered: dict[str, int],
) -> Model | None:
    """The installed row this legacy model updates, or None to create one.

    Two ways to recognise it, in order. A **community UID** is authoritative and
    cross-machine — the same shared model installed here from the Community page
    is the same model, so re-importing it must not fork the user's slot layout.
    Failing that, an earlier run of *this* import against *this* root recorded
    the pairing, which is what makes running the import twice idempotent.
    """
    if model.community_model_uid:
        found = model_repo.find_by_community_uid(model.community_model_uid)
        if found is not None:
            return found
    remembered_id = remembered.get(str(entry.legacy_id))
    if remembered_id:
        return model_repo.get(remembered_id)
    return None


def _copy_images(source: Path, dest: Path, *, progress: Callable[[str], None] | None) -> int:
    """Copy the legacy `{label}__{ticks}.jpg` files across.

    Basename only and extension-checked: the source is a directory the user
    pointed at, so it is treated with the same suspicion as a ZIP entry. Files
    already present are skipped, which is what makes a re-run cheap instead of
    re-copying a 12,000-image set.
    """
    if not source.is_dir():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _VALID_IMAGE_EXTS:
            continue
        target = dest / path.name
        if target.exists() and target.stat().st_size == path.stat().st_size:
            continue
        try:
            shutil.copy2(path, target)
        except OSError as exc:  # a locked or unreadable file loses that image, not the import
            log.warning("winforms import: could not copy %s: %s", path, exc)
            continue
        copied += 1
        if progress and copied % 200 == 0:
            progress(f"Copied {copied} images from '{source.name}'…")
    return copied


def _import_headstamps(
    entry: LegacyModel,
    model_id: int,
    raw_db: dict[str, Any],
    *,
    headstamp_repo: HeadstampRepo,
    parent_repo: HeadstampParentRepo,
    result: ImportResult,
) -> None:
    """Headstamps, parent classifications and the slot layout for one model.

    Slot assignments are inverted on the way in: the legacy app stores them as
    `SlotConfigs[].Config` (a slot listing its headstamps), while ours live on
    the headstamp row as a single `slot` int. Package-mode rows are a separate
    list in the legacy DB too, and are handled by the caller because they land
    in a settings key rather than on a row.
    """
    existing = {h.name: h for h in headstamp_repo.list_for_model(model_id)}
    by_legacy_id: dict[int, str] = {}
    for row in _rows(raw_db, "Headstamps"):
        if int(row.get("Model_Id") or 0) != entry.legacy_id:
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        by_legacy_id[int(row.get("Id") or 0)] = name
        if name not in existing:
            existing[name] = headstamp_repo.add(model_id, name)
            result.headstamps_imported += 1

    parents_by_legacy_id: dict[int, int] = {}
    for row in _rows(raw_db, "HeadStampParents"):
        if int(row.get("Model_Id") or 0) != entry.legacy_id:
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        found = parent_repo.find_by_name(model_id, name)
        if found is None:
            found = parent_repo.add(model_id, name)
            result.parents_imported += 1
        parents_by_legacy_id[int(row.get("Id") or 0)] = found.id

    for row in _rows(raw_db, "HeadStampParentLinks"):
        if int(row.get("ModelId") or 0) != entry.legacy_id:
            continue
        parent_id = parents_by_legacy_id.get(int(row.get("ParentId") or 0))
        child_name = by_legacy_id.get(int(row.get("HeadStampId") or 0))
        if parent_id is None or child_name is None:
            continue
        child = existing.get(child_name)
        if child is not None:
            headstamp_repo.set_parent(child.id, parent_id)

    for slot_row in _rows(raw_db, "SlotConfigs"):
        if int(slot_row.get("ModelId") or 0) != entry.legacy_id or slot_row.get("PackageMode"):
            continue
        slot = int(slot_row.get("SlotNumber") or 0)
        for assignment in slot_row.get("Config") or []:
            name = _assignment_name(assignment)
            target = existing.get(name) if name else None
            if target is not None:
                headstamp_repo.update_slot(target.id, slot)
                result.slots_assigned += 1
        for assignment in slot_row.get("ParentConfig") or []:
            name = _assignment_name(assignment)
            parent = parent_repo.find_by_name(model_id, name) if name else None
            if parent is not None:
                parent_repo.update_slot(parent.id, slot)
                result.slots_assigned += 1


def _assignment_name(assignment: Any) -> str:
    """Pull the headstamp/parent name out of one `SlotConfigs` entry.

    The observed shape is `{"Headstamp": {"Name": ...}, "Counter": n}`, but the
    parent list is empty in every install seen so far, so its exact spelling is
    unconfirmed. Accepting the bare name, a `Parent` wrapper and a plain string
    costs nothing and keeps an unexpected shape from silently dropping a slot.
    """
    if isinstance(assignment, str):
        return assignment.strip()
    if not isinstance(assignment, dict):
        return ""
    for key in ("Headstamp", "Parent", "HeadStampParent"):
        nested = assignment.get(key)
        if isinstance(nested, dict):
            return str(nested.get("Name") or "").strip()
    return str(assignment.get("Name") or "").strip()


def _package_slots(raw_db: dict[str, Any], legacy_id: int) -> dict[str, list[str]]:
    """The package-mode slot map for one model, in our settings-key shape."""
    out: dict[str, list[str]] = {}
    for slot_row in _rows(raw_db, "SlotConfigs"):
        if int(slot_row.get("ModelId") or 0) != legacy_id or not slot_row.get("PackageMode"):
            continue
        slot = int(slot_row.get("SlotNumber") or 0)
        if slot <= 0:
            continue
        names = [n for n in (_assignment_name(a) for a in slot_row.get("Config") or []) if n]
        if names:
            out[str(slot)] = names
    return out


_COUNT_FIELDS = (
    "models_imported",
    "models_updated",
    "headstamps_imported",
    "parents_imported",
    "slots_assigned",
)


def _merge_counts(into: ImportResult, delta: ImportResult) -> None:
    for name in _COUNT_FIELDS:
        setattr(into, name, getattr(into, name) + getattr(delta, name))


def _import_one_model(
    entry: LegacyModel,
    raw_db: dict[str, Any],
    *,
    options: ImportOptions,
    model_repo: ModelRepo,
    cart_repo: CartridgeRepo,
    headstamp_repo: HeadstampRepo,
    parent_repo: HeadstampParentRepo,
    settings_repo: SettingsRepo,
    root: Path,
    remembered: dict[str, int],
    result: ImportResult,
) -> int:
    """Bring one legacy `Models` row across and return our model id.

    Raises rather than warns: the caller runs this inside its own SAVEPOINT and
    turns a failure into a skipped model.
    """
    model = model_from_export_dict(entry.raw)
    model.name = entry.name
    existing = _find_existing(entry, model, model_repo=model_repo, remembered=remembered)
    model.cartridge_id = (
        existing.cartridge_id if existing is not None else cart_repo.get_or_create(entry.cartridge_name).id
    )
    if existing is not None:
        model.id = existing.id
        model.name = existing.name
        # The user's local API credentials outlive a re-import — unless they
        # never entered any. A row first imported before "openai" was a mode
        # here has a blank local config, and keeping that blank would discard
        # the endpoint the legacy install actually holds.
        if existing.ai_model_config.to_dict() != AIModelConfig().to_dict():
            model.ai_model_config = existing.ai_model_config
        model.model_path = existing.model_path
        model_repo.update(model)
        saved_id = existing.id
        result.models_updated += 1
    else:
        saved_id = model_repo.create(model).id
        result.models_imported += 1
    _remember_import(settings_repo, root, entry.legacy_id, saved_id)

    if options.headstamps:
        _import_headstamps(
            entry,
            saved_id,
            raw_db,
            headstamp_repo=headstamp_repo,
            parent_repo=parent_repo,
            result=result,
        )
        package = _package_slots(raw_db, entry.legacy_id)
        if package:
            settings_repo.set(f"{_PACKAGE_SLOTS_KEY}:{saved_id}", package)
        if bool(entry.raw.get("UseParentClassificationsAtRuntime")):
            settings_repo.set(f"{_USE_PARENT_RUNTIME_KEY}:{saved_id}", True)
    return saved_id


def _import_serial(defaults: dict[str, Any], settings_json: dict[str, Any], config: Config) -> None:
    """Port, baud, slot count and the board's init values.

    The init values are the payload here: they are per-machine calibration the
    user tuned against their own hardware. Only keys this app already knows are
    taken — an unrecognised one would be written straight back to the board.
    """
    serial = config.serial
    port = str(defaults.get("DefaultSerialPort") or "").strip()
    if port:
        serial["port"] = port
    baud = settings_json.get("serialBaudRate")
    if isinstance(baud, int) and baud > 0:
        serial["baud"] = baud
    slots = defaults.get("SlotQuantity")
    if isinstance(slots, int) and slots > 0:
        serial["slot_quantity"] = slots

    init = dict(serial.get("init_settings") or {})
    for row in defaults.get("InitSettings") or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("Key") or "").strip()
        if key not in DEFAULT_INIT_SETTINGS:
            continue
        raw_value = row.get("Value")
        try:
            init[key] = int(str(raw_value).strip())
        except (TypeError, ValueError):
            init[key] = str(raw_value)
    serial["init_settings"] = init


def _import_image_processing(defaults: dict[str, Any], config: Config) -> bool:
    """The line-scan tuning from `Defaults.IP_*`.

    Returns False when the install carries none of it. The per-model primer
    settings are *not* handled here — `UsePrimerMask` / `HidePrimer` /
    `PrimerMaskSize` ride along on the model row itself via
    `model_from_export_dict`, which is where this app also keeps them.
    """
    linescan = dict(config.image_proc.get("linescan") or {})
    touched = False
    for field_name, (legacy_key, caster) in _LINESCAN_FROM_LEGACY.items():
        if legacy_key not in defaults:
            continue
        try:
            linescan[field_name] = caster(defaults[legacy_key])
        except (TypeError, ValueError):
            continue
        touched = True
    if touched:
        config.image_proc["linescan"] = linescan
    return touched


def _ai_config_candidates(survey_in: LegacySurvey, default_legacy_id: int = 0) -> list[LegacyModel]:
    """Legacy models in the order their AI settings should be preferred.

    Per-model in the legacy app, app-level here (§4, "Active-model concept"), so
    exactly one of them wins. A model set to OpenAI mode is the one that was
    genuinely classifying over HTTP, so it outranks the legacy active model,
    which outranks whatever happens to be declared first.
    """

    def rank(entry: LegacyModel) -> int:
        if entry.was_openai_mode:
            return 0
        return 1 if entry.legacy_id == default_legacy_id else 2

    return sorted(survey_in.models, key=rank)


def _import_ai_config(survey_in: LegacySurvey, config: Config, default_legacy_id: int = 0) -> bool:
    """The best `AIModelConfig` on any model, into the app-level `api`.

    `AIModelConfig.from_dict` already knows the legacy `OpenAI_*` key spellings.
    """
    for entry in _ai_config_candidates(survey_in, default_legacy_id):
        raw = _legacy_ai_config(entry.raw)
        if raw is None:
            continue
        parsed = AIModelConfig.from_dict(raw)
        api = config.api
        if parsed.endpoint_url:
            api["endpoint_url"] = parsed.endpoint_url
        if parsed.api_key:
            api["api_key"] = parsed.api_key
        if parsed.model:
            api["model"] = parsed.model
        if parsed.prompt:
            api["prompt"] = parsed.prompt
        api["image_quality"] = int(parsed.image_quality or api.get("image_quality", 100))
        api["image_scale"] = int(parsed.image_scale or api.get("image_scale", 100))
        return True
    return False


def import_installation(
    root: Path | str,
    *,
    db: Database,
    config: Config,
    options: ImportOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> ImportResult:
    """Import the ticked items from a legacy install at `root`.

    Non-destructive to the source: every file is copied, nothing there is
    written, moved or removed.

    Re-running is safe. A model already brought across from this same root — or
    one already installed under the same community UID — is refreshed in place,
    so slot assignments and sorting templates survive and the library does not
    grow duplicates. Images already copied are skipped by name and size.

    DB writes run inside one transaction; the bulk file copies deliberately do
    not, since holding the SQLite write lock for the minutes a 12,000-image copy
    takes would block the whole app.

    A model this app cannot accept is skipped with a warning rather than
    failing the run — an install's worth of images is not worth losing to one
    bad row.
    """
    root = Path(root)
    options = (options or ImportOptions()).normalized()
    survey_in = survey(root)
    result = ImportResult(warnings=list(survey_in.warnings))
    if not options.any_selected():
        return result

    settings_json: dict[str, Any] = {}
    settings_path = root / SETTINGS_NAME
    if settings_path.is_file():
        try:
            settings_json = _read_json(settings_path)
        except (OSError, json.JSONDecodeError) as exc:
            result.warnings.append(f"Could not read {SETTINGS_NAME}: {exc}")

    raw_db = _read_json(root / CONFIG_DB_NAME)
    defaults = raw_db.get("Defaults")
    defaults = defaults if isinstance(defaults, dict) else {}

    settings_repo = SettingsRepo(db)
    model_repo = ModelRepo(db)
    cart_repo = CartridgeRepo(db)
    headstamp_repo = HeadstampRepo(db)
    parent_repo = HeadstampParentRepo(db)

    # legacy model id -> our model id, for the file copies after the transaction.
    imported: dict[int, int] = {}
    activate_legacy_id = int(defaults.get("DefaultModelId") or 0)

    with db.transaction():
        remembered = _imported_map(settings_repo, root)
        if options.models:
            for entry in survey_in.models:
                if progress:
                    progress(f"Importing '{entry.name}'…")
                # A nested transaction is a SAVEPOINT, so a row this app will
                # not accept rolls back to just before itself. Counts go to a
                # scratch result for the same reason — merged only once the
                # row has actually committed.
                delta = ImportResult()
                try:
                    with db.transaction():
                        saved_id = _import_one_model(
                            entry,
                            raw_db,
                            options=options,
                            model_repo=model_repo,
                            cart_repo=cart_repo,
                            headstamp_repo=headstamp_repo,
                            parent_repo=parent_repo,
                            settings_repo=settings_repo,
                            root=root,
                            remembered=remembered,
                            result=delta,
                        )
                except (ValueError, TypeError, sqlite3.Error) as exc:
                    log.warning("winforms import: skipping model %r: %s", entry.name, exc)
                    result.warnings.append(MODEL_FAILED_WARNING.format(name=entry.name, error=exc))
                    continue
                _merge_counts(result, delta)
                imported[entry.legacy_id] = saved_id

        if options.serial:
            _import_serial(defaults, settings_json, config)
            result.serial_imported = True
        if options.image_processing:
            result.image_processing_imported = _import_image_processing(defaults, config)
        if options.ai_config:
            result.ai_config_imported = _import_ai_config(survey_in, config, activate_legacy_id)
        if result.serial_imported or result.image_processing_imported or result.ai_config_imported:
            config.save()

        # Only ever *adopt* the legacy app's active model, never override a
        # choice already made here — the import is an offer, not a takeover.
        if options.models and settings_repo.get_active_model_id() is None:
            target = imported.get(activate_legacy_id)
            if target is not None:
                settings_repo.set_active_model_id(target)
                result.activated_model_id = target

    # ----- file copies, outside the write lock --------------------------------
    for entry in survey_in.models:
        model_id = imported.get(entry.legacy_id)
        if model_id is None:
            continue
        paths.ensure_model_subtree(model_id)
        if options.training_images:
            result.images_copied += _copy_images(
                root / IMAGES_SUBDIR / str(entry.legacy_id),
                paths.model_images_dir(model_id),
                progress=progress,
            )
        if entry.has_usable_checkpoint and entry.checkpoint is not None:
            dest = paths.model_trained_path(model_id)
            # Same skip rule as the images: a re-run must not pay a multi-
            # hundred-MB copy for a checkpoint that hasn't changed. Size is
            # the same cheap proxy — a retrained model virtually never lands
            # on the identical byte count.
            try:
                unchanged = dest.exists() and dest.stat().st_size == entry.checkpoint.stat().st_size
            except OSError:
                unchanged = False
            if unchanged:
                # The row still has to point at it — belt-and-braces for a
                # half-finished earlier run that copied but never recorded.
                with db.transaction():
                    saved = model_repo.get(model_id)
                    if saved is not None and saved.model_path != str(dest):
                        saved.model_path = str(dest)
                        model_repo.update(saved)
                continue
            if progress:
                progress(f"Copying the trained model for '{entry.name}'…")
            try:
                shutil.copy2(entry.checkpoint, dest)
            except OSError as exc:
                result.warnings.append(f"Could not copy the checkpoint for '{entry.name}': {exc}")
                continue
            result.checkpoints_copied += 1
            with db.transaction():
                saved = model_repo.get(model_id)
                if saved is not None:
                    saved.model_path = str(dest)
                    model_repo.update(saved)

    log.info(
        "winforms import from %s: %d new / %d updated models, %d images, %d checkpoints",
        root,
        result.models_imported,
        result.models_updated,
        result.images_copied,
        result.checkpoints_copied,
    )
    return result


# ----- first-run offer --------------------------------------------------------


def _looks_unused(db: Database) -> bool:
    """True while this app still looks freshly installed.

    Not "has no models": `Database.ensure_initialized` seeds a starter
    cartridge and model on every fresh database, so the library is never empty.
    What distinguishes a used install is a model the user has actually put to
    work — one with a trained checkpoint on disk — or a deliberate choice of
    active model. Either means the import offer would be noise.
    """
    if SettingsRepo(db).get_active_model_id() is not None:
        return False
    return not any(m.model_path and Path(m.model_path).exists() for m in ModelRepo(db).list())


def should_offer_first_run(db: Database) -> Path | None:
    """The install to offer importing on this launch, or None to stay silent.

    None whenever the offer has already been made (accepted or declined — it
    stays reachable from Settings either way), whenever this app is already in
    use, or whenever there is simply no Windows install to find.
    """
    settings = SettingsRepo(db)
    if settings.get(FIRST_RUN_SEEN_KEY):
        return None
    if not _looks_unused(db):
        return None
    root = find_installation()
    if root is None:
        return None
    try:
        found = survey(root)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None if found.is_empty else root


def mark_first_run_offered(db: Database) -> None:
    SettingsRepo(db).set(FIRST_RUN_SEEN_KEY, True)
