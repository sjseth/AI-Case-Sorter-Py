"""Offline model evaluation: classify a folder of labelled images and score it.

Provides the legacy app's batch-image-tester feature, but runs inference
**in-process** via :mod:`sorter.ml.local_inference` instead of shelling out to a
worker. The ground-truth label for each image is taken from the
training filename convention ``{label}__{ticks}.ext``; an optional
folder-class -> model-class mapping lets a dataset labelled with different names
be scored against the model's classes.

Everything here is pure/headless and unit-testable: ``evaluate_folder`` takes an
injectable ``classify_fn`` so tests don't need PyTorch.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ..training.dataset import parse_label

IMAGE_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

ClassifyFn = Callable[..., tuple[str, float]]
ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]


# ----- folder / filename helpers --------------------------------------------


def list_images(folder: Path | str, extensions: Iterable[str] = IMAGE_EXTS) -> list[Path]:
    """All image files directly in ``folder`` (case-insensitive extension)."""
    d = Path(folder)
    if not d.is_dir():
        return []
    exts = {e.lower() for e in extensions}
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)


def folder_classes(folder: Path | str, extensions: Iterable[str] = IMAGE_EXTS) -> list[str]:
    """Distinct ground-truth labels found in the folder's filenames, sorted."""
    seen: dict[str, None] = {}
    for path in list_images(folder, extensions):
        label = parse_label(path.name)
        if label:
            seen.setdefault(label, None)
    return sorted(seen, key=str.casefold)


def map_classification(original: str, mapping: dict[str, str]) -> tuple[str, bool]:
    """Map a folder label to a model label. Returns (value, was_mapped).

    Falls back to the original (unmapped) when no mapping entry matches, so
    same-named datasets still score without an explicit mapping.
    """
    if not original or not original.strip():
        return "", False
    if original in mapping:
        return mapping[original], True
    upper = original.upper()
    for key, value in mapping.items():
        if key.upper() == upper:
            return value, True
    return original, False


def match_status(predicted: str, original: str) -> str:
    """'match' / 'mismatch' / 'unknown' (no ground truth)."""
    if not original:
        return "unknown"
    return "match" if predicted == original else "mismatch"


# ----- auto-map suggestion ---------------------------------------------------


def _tokenize(headstamp: str) -> list[str]:
    return [t for t in re.split(r"[ \-_+]+", headstamp) if t]


def _norm(token: str) -> str:
    return "".join(ch for ch in token if ch.isalnum()).upper()


def _tokens_equivalent(a: str, b: str) -> bool:
    return a.casefold() == b.casefold() or _norm(a) == _norm(b)


def _score_token_match(target: list[str], model: list[str]) -> float:
    matches = [(t, m) for t in range(len(target)) for m in range(len(model)) if _tokens_equivalent(target[t], model[m])]
    if not matches:
        return 0.0
    match_count = min(len({t for t, _ in matches}), len({m for _, m in matches}))
    # A lone match only counts when it's the leading token on both sides.
    if match_count == 1:
        t0, m0 = matches[0]
        if t0 != 0 or m0 != 0:
            return 0.0
    score = match_count * 100.0
    positional = sum(1 for i in range(min(len(target), len(model))) if _tokens_equivalent(target[i], model[i]))
    score += positional * 50.0
    in_order, last = True, -1
    for _, m in sorted(matches, key=lambda x: x[0]):
        if m < last:
            in_order = False
            break
        last = m
    if in_order:
        score += 25.0
    score -= abs(len(target) - len(model)) * 10.0
    if match_count == len(target) == len(model):
        score += 75.0
    return score


def suggest_mapping(target_class: str, model_classes: list[str]) -> str | None:
    """Best model class for a folder class, or None."""
    target = (target_class or "").strip()
    if not target:
        return None
    for mc in model_classes:  # exact (case-insensitive)
        if mc.casefold() == target.casefold():
            return mc
    target_tokens = _tokenize(target)
    best, best_score = None, 0.0
    for mc in model_classes:  # token-scored
        score = _score_token_match(target_tokens, _tokenize(mc))
        if score > best_score:
            best, best_score = mc, score
    if best is not None:
        return best
    for mc in model_classes:  # prefix
        if target.casefold().startswith(mc.casefold()):
            return mc
    for mc in model_classes:  # contains
        if mc.casefold() in target.casefold():
            return mc
    return None


def auto_map(target_classes: list[str], model_classes: list[str]) -> dict[str, str]:
    """Suggest a mapping for every target class that resolves to a model class."""
    out: dict[str, str] = {}
    for target in target_classes:
        suggestion = suggest_mapping(target, model_classes)
        if suggestion:
            out[target] = suggestion
    return out


# ----- evaluation ------------------------------------------------------------


def _default_classify(image_bgr, model_path: str, *, image_size: int | None) -> tuple[str, float]:
    from . import local_inference

    return local_inference.classify(image_bgr, model_path, image_size=image_size)


def evaluate_folder(
    folder: Path | str,
    *,
    model_path: str,
    image_size: int | None = None,
    mapping: dict[str, str] | None = None,
    extensions: Iterable[str] = IMAGE_EXTS,
    classify_fn: ClassifyFn | None = None,
    progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
) -> dict[str, Any]:
    """Classify every image in ``folder`` and score against filename labels.

    Returns ``{"results": [...], "summary": {...}, "stopped": bool}``. Each
    result is a dict with predicted/confidence/original/match fields. Images
    that fail to load or classify are recorded with predicted ``"ERROR"`` so a
    single bad file never aborts the run.
    """
    classify = classify_fn or _default_classify
    mapping = mapping or {}
    images = list_images(folder, extensions)
    total = len(images)

    import cv2  # local: keep the module importable without OpenCV present

    results: list[dict[str, Any]] = []
    stopped = False
    for i, path in enumerate(images, 1):
        if should_stop is not None and should_stop():
            stopped = True
            break
        if progress is not None:
            progress(i, total, path.name)

        predicted, confidence = "ERROR", 0.0
        image = cv2.imread(str(path))
        if image is not None:
            try:
                predicted, confidence = classify(image, model_path, image_size=image_size)
            except Exception:
                predicted, confidence = "ERROR", 0.0

        raw = parse_label(path.name) or ""
        original, was_mapped = map_classification(raw, mapping)
        results.append(
            {
                "filename": path.name,
                "filepath": str(path),
                "predicted": predicted,
                "confidence": float(confidence),
                "original": original,
                "raw_original": raw,
                "has_mapping": was_mapped,
                "match": match_status(predicted, original),
            }
        )

    return {"results": results, "summary": summarize(results), "stopped": stopped}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Accuracy + per-predicted-class breakdown for a result list."""
    total = len(results)
    with_original = [r for r in results if r["original"]]
    with_mapping = [r for r in results if r["has_mapping"]]
    total_matches = sum(1 for r in with_original if r["predicted"] == r["original"])
    known_matches = sum(1 for r in with_mapping if r["predicted"] == r["original"])
    avg_conf = (sum(r["confidence"] for r in results) / total) if total else 0.0

    by_class: dict[str, dict[str, Any]] = {}
    for r in results:
        cls = r["predicted"]
        bucket = by_class.setdefault(cls, {"count": 0, "confs": [], "mismatches": 0})
        bucket["count"] += 1
        bucket["confs"].append(r["confidence"])
        if r["original"] and r["predicted"] != r["original"]:
            bucket["mismatches"] += 1

    per_class = []
    for cls, b in sorted(by_class.items(), key=lambda kv: kv[0].casefold()):
        confs = b["confs"]
        per_class.append(
            {
                "cls": cls,
                "count": b["count"],
                "avg": sum(confs) / len(confs) if confs else 0.0,
                "high": max(confs) if confs else 0.0,
                "low": min(confs) if confs else 0.0,
                "mismatches": b["mismatches"],
            }
        )

    return {
        "total": total,
        "with_original": len(with_original),
        "with_mapping": len(with_mapping),
        "total_matches": total_matches,
        "known_matches": known_matches,
        "total_accuracy": (total_matches / len(with_original) * 100.0) if with_original else None,
        "known_accuracy": (known_matches / len(with_mapping) * 100.0) if with_mapping else None,
        "avg_confidence": avg_conf,
        "per_class": per_class,
    }
