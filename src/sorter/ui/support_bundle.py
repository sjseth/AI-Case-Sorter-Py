"""Support-package collection: the user's configuration as a shareable report.

UI-free on purpose — ``dialog_support.py`` renders it, but everything here is
plain data so tests (and a future assistant reading ``config.json``) get the
same content the user pastes on Discord.

Redaction rules, applied at collection time so a secret never enters the
structured data at all:

* The AI Config API key is reported only as ``set`` / ``not set``.
* Nothing is ever read from the MSAL auth cache.
* File paths are rendered relative to the data root; anything outside it
  collapses to ``(outside data root)`` so a home directory can't leak.
"""

from __future__ import annotations

import json
import os
import platform
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import __version__, paths
from ..data.models import Model, is_foreign_model
from ..data.repository import ModelRepo, SettingsRepo
from ..hardware import serial_log
from ..training import manager

REPORT_MEMBER = "report.txt"
CONFIG_MEMBER = "config.json"
TRAINING_LOG_MEMBER = "training.log"
SERIAL_LOG_MEMBER = "serial.log"

# The masked spellings the report/JSON use for the API key. Tests assert on
# these; nothing else may carry the key's value.
KEY_SET = "set"
KEY_NOT_SET = "not set"


def _mask_secret(value: Any) -> str:
    return KEY_SET if str(value or "").strip() else KEY_NOT_SET


def _redact_path(raw: str | None) -> str | None:
    """A data-root-relative path, or a placeholder — never an absolute path."""
    if not raw:
        return None
    try:
        return str(Path(raw).relative_to(paths.app_data_dir()))
    except ValueError:
        return "(outside data root)"


def _active_model(db: Any) -> Model | None:
    active_id = SettingsRepo(db).get_active_model_id()
    return None if active_id is None else ModelRepo(db).get(active_id)


def collect_data(config: Any, db: Any) -> dict[str, Any]:
    """The redacted, machine-readable form of everything the report shows."""
    model = _active_model(db)
    serial = config.serial
    init_settings = dict(serial.get("init_settings") or {})
    image_proc = config.image_proc
    api = config.api
    camera = config.camera

    if model is None:
        model_data: dict[str, Any] | None = None
        training: dict[str, Any] | None = None
    else:
        checkpoint = bool(model.model_path and Path(model.model_path).exists())
        model_data = {
            "name": model.name,
            "base": model.model_mode,
            "type": model.model_type,
            "ownership": ("community-installed" if is_foreign_model(model) else "user-owned"),
            "community_uid": model.community_model_uid,
            "model_version": model.model_version,
            "image_size": model.training_config.image_size,
            "trained_image_count": model.trained_image_count,
            "last_training_date": model.last_training_date,
            "checkpoint": "present" if checkpoint else "missing",
            "checkpoint_path": _redact_path(model.model_path),
        }
        training = model.training_config.to_dict()
        training["image_directory"] = _redact_path(training.get("image_directory"))
        training["output_model_path"] = _redact_path(training.get("output_model_path"))

    return {
        "application": {
            "version": __version__,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "active_model": model_data,
        "run_options": {
            "confidence_floor": config.run_confidence_floor,
            "store_images": config.run_store_images,
            "package_mode": config.run_package_mode,
            "package_size": config.run_package_size,
            "auto_select_trays": config.run_auto_select_trays,
        },
        "training_config": training,
        "image_processing": {
            "strategy": image_proc.get("strategy"),
            "primer_mode": image_proc.get("primer_mode"),
            "primer_radius": image_proc.get("primer_radius"),
            "hough": dict(image_proc.get("hough") or {}),
            "led_level": init_settings.get("cameraledlevel"),
        },
        "serial": {
            "port": serial.get("port"),
            "baud": serial.get("baud"),
            "slot_quantity": serial.get("slot_quantity"),
            "init_on_startup": serial.get("init_on_startup"),
            "init_settings": init_settings,
        },
        "camera": {
            "device_index": camera.get("device_index"),
            "device_chosen": camera.get("device_chosen"),
            "resolution": f"{camera.get('width')}x{camera.get('height')}",
        },
        "ai_config": {
            "endpoint_url": api.get("endpoint_url"),
            "api_key": _mask_secret(api.get("api_key")),
            "model": api.get("model"),
            "prompt": api.get("prompt"),
            "image_quality": api.get("image_quality"),
            "image_scale": api.get("image_scale"),
        },
        "training_log": _log_summary(manager.training_logs(), TRAINING_LOG_MEMBER),
        "serial_log": _log_summary(serial_log.serial_logs(), SERIAL_LOG_MEMBER),
    }


def _log_summary(logs: list[Path], member: str) -> dict[str, Any]:
    """What the most recent training run (issue #100) or serial session left behind.

    The report names it; ``write_bundle`` puts the file itself in the ZIP. Only
    the newest — the whole point is "here is the run you are asking about",
    not an archive.
    """
    if not logs:
        return {"available": "none"}
    newest = logs[0]
    try:
        size = newest.stat().st_size
    except OSError:
        size = 0
    return {
        "available": member,
        "file": newest.name,
        "size_bytes": size,
        "older_runs_kept": len(logs) - 1,
    }


def _redact_text(text: str) -> str:
    """Strip machine-identifying prefixes out of a log before it is shared.

    The structured report never contains an absolute path (``_redact_path``),
    and a raw log must not be the hole in that: a training command line carries
    the data root, and the data root usually carries the user's name.
    """
    replacements = [
        (str(paths.app_data_dir()), "<data>"),
        (str(paths.app_root()), "<app>"),
        (str(Path.home()), "<home>"),
    ]
    # Longest first: the data root often sits inside the home directory, and
    # replacing the shorter one first would leave "<home>/.local/share/…".
    for needle, token in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        if needle and needle != os.sep:
            text = text.replace(needle, token)
    return text


def _value(raw: Any) -> str:
    if raw is None or raw == "":
        return "(not set)"
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    return str(raw)


def _lines(items: dict[str, Any], indent: str = "") -> list[str]:
    out: list[str] = []
    for key, raw in items.items():
        if isinstance(raw, dict):
            out.append(f"{indent}{key}:")
            out.extend(_lines(raw, indent + "  "))
        else:
            out.append(f"{indent}{key}: {_value(raw)}")
    return out


def render_report(data: dict[str, Any]) -> str:
    """The human-readable half of the bundle, from ``collect_data``'s output."""
    sections: list[tuple[str, dict[str, Any] | None, str]] = [
        ("Application", data["application"], ""),
        ("Active model", data["active_model"], "None — AI Config mode (HTTP classification)"),
        ("Run options", data["run_options"], ""),
        ("Training configuration", data["training_config"], "(no active local model)"),
        ("Image processing", data["image_processing"], ""),
        ("Serial", data["serial"], ""),
        ("Camera", data["camera"], ""),
        ("AI Config (HTTP classification)", data["ai_config"], ""),
        ("Last training run", data["training_log"], ""),
        ("Last serial log", data["serial_log"], ""),
    ]
    lines: list[str] = ["AI Case Sorter — support report"]
    for title, body, empty_text in sections:
        lines.append("")
        lines.append(f"== {title} ==")
        lines.extend(_lines(body) if body is not None else [empty_text])
    return "\n".join(lines) + "\n"


def collect_report(config: Any, db: Any) -> str:
    return render_report(collect_data(config, db))


def write_bundle(path: str | os.PathLike[str], config: Any, db: Any) -> Path:
    """Write the support ZIP atomically.

    ``report.txt`` + ``config.json``, plus ``training.log`` — the most recent
    training run's console (issue #100) — and ``serial.log`` — the most recent
    serial traffic log — when there are any. Each log is passed through
    ``_redact_text`` on the way in, so the ZIP keeps the promise the report
    makes: no absolute paths, nothing naming the machine.
    """
    out = Path(path)
    data = collect_data(config, db)
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(REPORT_MEMBER, render_report(data))
            archive.writestr(CONFIG_MEMBER, json.dumps(data, indent=2))
            for member, logs in (
                (TRAINING_LOG_MEMBER, manager.training_logs()),
                (SERIAL_LOG_MEMBER, serial_log.serial_logs()),
            ):
                log = _newest_log(logs)
                if log is not None:
                    archive.writestr(member, log)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def _newest_log(logs: list[Path]) -> str | None:
    """The newest of ``logs``, redacted. None when there isn't one."""
    if not logs:
        return None
    try:
        return _redact_text(logs[0].read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
