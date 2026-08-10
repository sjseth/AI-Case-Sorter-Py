"""Offline model-evaluation logic (no PyTorch — classify_fn is injected)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sorter import evaluator

cv2 = pytest.importorskip("cv2")


def _write_img(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((8, 8, 3), 127, dtype=np.uint8))


def _make_folder(tmp_path: Path, names: list[str]) -> Path:
    folder = tmp_path / "imgs"
    for n in names:
        _write_img(folder / n)
    return folder


def test_list_images_and_folder_classes(tmp_path: Path) -> None:
    folder = _make_folder(
        tmp_path,
        [
            "WIN__1.jpg",
            "WIN__2.JPG",
            "FC__3.png",
            "noprefix.jpg",
        ],
    )
    (folder / "notes.txt").write_text("x")
    assert len(evaluator.list_images(folder)) == 4  # txt excluded
    assert evaluator.folder_classes(folder) == ["FC", "WIN"]


def test_map_classification() -> None:
    mapping = {"FC09": "FC", "win": "WIN"}
    assert evaluator.map_classification("FC09", mapping) == ("FC", True)
    assert evaluator.map_classification("WIN", mapping) == ("WIN", True)  # case-insensitive
    assert evaluator.map_classification("RP", mapping) == ("RP", False)  # unmapped -> passthrough
    assert evaluator.map_classification("", mapping) == ("", False)


def test_match_status() -> None:
    assert evaluator.match_status("WIN", "WIN") == "match"
    assert evaluator.match_status("WIN", "FC") == "mismatch"
    assert evaluator.match_status("WIN", "") == "unknown"


def test_suggest_mapping_levels() -> None:
    model = ["WIN", "FC", "9mm Luger", "5.56x45"]
    assert evaluator.suggest_mapping("win", model) == "WIN"  # exact (ci)
    assert evaluator.suggest_mapping("556X45", model) == "5.56x45"  # normalized tokens
    assert evaluator.suggest_mapping("ZZZ", model) is None
    auto = evaluator.auto_map(["win", "fc", "ZZZ"], model)
    assert auto == {"win": "WIN", "fc": "FC"}


def test_evaluate_folder_scores_with_fake_classifier(tmp_path: Path) -> None:
    folder = _make_folder(
        tmp_path,
        [
            "WIN__1.jpg",  # predicted WIN  -> match
            "WIN__2.jpg",  # predicted FC   -> mismatch
            "FC__3.jpg",  # predicted FC   -> match
            "noprefix.jpg",  # no ground truth
        ],
    )

    # Deterministic fake: prediction encoded by filename.
    preds = {
        "WIN__1.jpg": ("WIN", 96.0),
        "WIN__2.jpg": ("FC", 51.0),
        "FC__3.jpg": ("FC", 80.0),
        "noprefix.jpg": ("WIN", 40.0),
    }
    seen: list[str] = []

    def fake_classify(image, model_path, *, image_size=None):
        # image actually loaded from disk, model_path/image_size threaded through
        assert image is not None and model_path == "m.pth" and image_size == 232
        # map by matching against the known set via a side channel
        return preds[_current[0]]

    # Wrap to know which file is being classified (progress gives us the name).
    _current = [""]

    def progress(i, n, name):
        _current[0] = name
        seen.append(name)

    out = evaluator.evaluate_folder(
        folder,
        model_path="m.pth",
        image_size=232,
        classify_fn=fake_classify,
        progress=progress,
    )
    results = {r["filename"]: r for r in out["results"]}
    assert results["WIN__1.jpg"]["match"] == "match"
    assert results["WIN__2.jpg"]["match"] == "mismatch"
    assert results["FC__3.jpg"]["match"] == "match"
    assert results["noprefix.jpg"]["match"] == "unknown"
    assert len(seen) == 4

    s = out["summary"]
    assert s["total"] == 4
    assert s["with_original"] == 3  # three have a filename label
    # 2 of 3 labelled images predicted correctly
    assert round(s["total_accuracy"], 1) == round(2 / 3 * 100, 1)
    # per-class FC: 2 predictions (WIN__2 mislabelled-as-FC, FC__3 correct), 1 mismatch
    fc = next(r for r in s["per_class"] if r["cls"] == "FC")
    assert fc["count"] == 2 and fc["mismatches"] == 1


def test_evaluate_folder_known_accuracy_uses_mapping(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path, ["BLZR__1.jpg", "RP__2.jpg"])

    def fake_classify(image, model_path, *, image_size=None):
        return ("Brass", 90.0)  # always predicts the parent/model class "Brass"

    out = evaluator.evaluate_folder(
        folder,
        model_path="m.pth",
        image_size=232,
        mapping={"BLZR": "Brass"},  # only BLZR is mapped
        classify_fn=fake_classify,
    )
    s = out["summary"]
    assert s["with_mapping"] == 1  # only BLZR
    assert s["known_accuracy"] == 100.0  # mapped BLZR predicted Brass -> correct
    # RP is unmapped: counts toward total (passthrough original "RP") but not known
    assert s["with_original"] == 2


def test_evaluate_folder_stop(tmp_path: Path) -> None:
    folder = _make_folder(tmp_path, [f"WIN__{i}.jpg" for i in range(5)])
    calls = {"n": 0}

    def fake_classify(image, model_path, *, image_size=None):
        calls["n"] += 1
        return ("WIN", 99.0)

    out = evaluator.evaluate_folder(
        folder,
        model_path="m.pth",
        classify_fn=fake_classify,
        should_stop=lambda: calls["n"] >= 2,  # stop after 2 classified
    )
    assert out["stopped"] is True
    assert len(out["results"]) == 2
