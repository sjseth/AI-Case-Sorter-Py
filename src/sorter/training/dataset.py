"""Training-image dataset helpers.

Uses the same filename convention as the legacy app so community model
exports interchange cleanly:

    {classification}__{ticks}.jpg

where `ticks` is the .NET ``DateTime.Ticks`` value (100-nanosecond intervals
since 0001-01-01).  Inference, training, and community export/import all
depend on this exact format.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

# Classification labels can come from an untrusted source — in AI Config mode
# the label is whatever free text the HTTP classification server returns — yet
# they are used verbatim as filename components for run/feedback/training image
# storage. Sanitize before a label ever touches the filesystem so it cannot
# traverse directories, name a Windows device, or contain illegal characters.
_UNSAFE_LABEL_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_RESERVED_WIN_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_LABEL_LEN = 100


def safe_label(label: str) -> str:
    """Sanitize a classification label for safe use as a path component.

    Replaces path separators, control characters, and filesystem-illegal
    characters with ``_``; collapses ``..`` so it cannot traverse; trims
    leading/trailing dots and whitespace; prefixes reserved Windows device
    names; and caps the length. Normal headstamps (letters, digits, spaces,
    ``+ - .``) pass through essentially unchanged. Returns ``"unknown"`` when
    nothing usable remains.
    """
    cleaned = _UNSAFE_LABEL_CHARS.sub("_", str(label))
    cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip().strip(". ")
    if cleaned.upper() in _RESERVED_WIN_NAMES:
        cleaned = f"_{cleaned}"
    cleaned = cleaned[:_MAX_LABEL_LEN].strip()
    return cleaned or "unknown"


def _assert_within(out_dir: Path, dest: Path) -> None:
    """Defense-in-depth: refuse to write `dest` outside `out_dir`."""
    base = out_dir.resolve()
    if not dest.resolve().is_relative_to(base):
        raise ValueError(f"refusing to write outside {base}: {dest}")


# Difference between Unix epoch (1970-01-01) and .NET reference (0001-01-01)
# expressed in 100-ns ticks. Uses the .NET DateTime.Ticks convention so the
# ticks generated here interchange byte-for-byte with the legacy app's.
_DOTNET_EPOCH_OFFSET_TICKS = 621_355_968_000_000_000


def dotnet_ticks(when: datetime | None = None) -> int:
    """Return a .NET ``DateTime.Ticks`` value for the given moment (default now)."""
    if when is None:
        when = datetime.now(UTC)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    unix_seconds = when.timestamp()
    return int(unix_seconds * 10_000_000) + _DOTNET_EPOCH_OFFSET_TICKS


def training_filename(label: str, *, ext: str = ".jpg", when: datetime | None = None) -> str:
    """Build a {label}__{ticks}{ext} filename. `label` may contain spaces.

    The label is sanitized (see :func:`safe_label`) before use so an untrusted
    classification string cannot escape the target directory.
    """
    if not label or not str(label).strip():
        raise ValueError("label cannot be empty")
    return f"{safe_label(label)}__{dotnet_ticks(when)}{ext}"


def save_training_image(
    image_bgr: np.ndarray,
    out_dir: Path | str,
    label: str,
    *,
    quality: int = 95,
    when: datetime | None = None,
) -> Path:
    """JPEG-encode an OpenCV BGR frame and write it to `<out_dir>/<filename>`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = training_filename(label, ext=".jpg", when=when)
    dest = out_dir / fname
    _assert_within(out_dir, dest)
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("Failed to JPEG-encode training image")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(buf.tobytes())
    os.replace(tmp, dest)
    return dest


def feedback_filename(label: str, confidence: float, *, ext: str = ".jpg", when: datetime | None = None) -> str:
    """Build a ``{label}__{confidence}__{ticks}{ext}`` feedback-image filename.

    Confidence is a rounded integer percent. Uses the legacy feedback-loop
    naming so the label + confidence can be recovered from the filename at
    upload time — the folder is the queue, so no database row is needed.
    """
    safe = safe_label(label)
    return f"{safe}__{int(round(float(confidence)))}__{dotnet_ticks(when)}{ext}"


def save_feedback_image(
    image_bgr: np.ndarray,
    out_dir: Path | str,
    label: str,
    confidence: float,
    *,
    quality: int = 95,
    when: datetime | None = None,
) -> Path:
    """JPEG-encode a BGR frame to ``<out_dir>/<feedback_filename>`` (atomic write)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / feedback_filename(label, confidence, ext=".jpg", when=when)
    _assert_within(out_dir, dest)
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("Failed to JPEG-encode feedback image")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(buf.tobytes())
    os.replace(tmp, dest)
    return dest


def parse_feedback_filename(filename: str) -> tuple[str, int] | None:
    """Inverse of `feedback_filename`: return ``(label, confidence)`` or None."""
    stem = Path(filename).stem
    parts = stem.split("__")
    if len(parts) < 3:
        return None
    label = parts[0]
    try:
        confidence = int(round(float(parts[1])))
    except ValueError:
        return None
    return label, confidence


def parse_label(filename: str) -> str | None:
    """Return the class label encoded in a training filename, or None."""
    base = Path(filename).name
    if "__" not in base:
        return None
    return base.split("__", 1)[0]


def class_counts(image_dir: Path | str) -> dict[str, int]:
    """Return a dict of {class_label: count} from filenames in `image_dir`."""
    d = Path(image_dir)
    if not d.exists():
        return {}
    counter: Counter[str] = Counter()
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
            continue
        label = parse_label(entry.name)
        if label:
            counter[label] += 1
    return dict(counter)
