"""Evaluator hybrid-preview result updates (delete / reassign from the popup).

Exercises the pure ``ModelEvaluatorDialog._apply_change`` recompute directly —
importing the module is display-free, so no Tk root is needed. The GUI stack
(tkinter/cv2/PIL) must import for the module to load, so skip where absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")
pytest.importorskip("PIL")

from sorter.ui.dialog_model_evaluator import ModelEvaluatorDialog


def _row(**over):
    base = {
        "filename": "WIN__1.jpg",
        "filepath": "/imgs/WIN__1.jpg",
        "predicted": "FC",
        "confidence": 80.0,
        "original": "WIN",
        "raw_original": "WIN",
        "has_mapping": False,
        "match": "mismatch",
    }
    base.update(over)
    return base


def test_delete_drops_matching_row() -> None:
    rows = [_row(), _row(filename="FC__2.jpg", filepath="/imgs/FC__2.jpg")]
    out = ModelEvaluatorDialog._apply_change(
        rows,
        {"action": "deleted", "old_path": "/imgs/WIN__1.jpg"},
        {},
    )
    assert out is not None  # "deleted" is a handled action, never a no-op
    assert [r["filepath"] for r in out] == ["/imgs/FC__2.jpg"]


def test_reclassify_rescores_against_prediction() -> None:
    # Predicted FC; reassign ground truth from WIN -> FC makes it a match.
    rows = [_row()]
    out = ModelEvaluatorDialog._apply_change(
        rows,
        {
            "action": "reclassified",
            "old_path": "/imgs/WIN__1.jpg",
            "new_path": "/imgs/FC__1.jpg",
            "new_headstamp": "FC",
        },
        {},
    )
    assert out is not None  # "reclassified" is a handled action, never a no-op
    r = out[0]
    assert r["filename"] == "FC__1.jpg"
    assert r["filepath"] == "/imgs/FC__1.jpg"
    assert r["raw_original"] == "FC"
    assert r["original"] == "FC"
    assert r["match"] == "match"


def test_reclassify_applies_mapping() -> None:
    # New prefix is a folder class that maps to the model's class space.
    rows = [_row(predicted="WIN")]
    out = ModelEvaluatorDialog._apply_change(
        rows,
        {
            "action": "reclassified",
            "old_path": "/imgs/WIN__1.jpg",
            "new_path": "/imgs/winchester__1.jpg",
            "new_headstamp": "winchester",
        },
        {"winchester": "WIN"},
    )
    assert out is not None  # "reclassified" is a handled action, never a no-op
    r = out[0]
    assert r["raw_original"] == "winchester"
    assert r["original"] == "WIN"
    assert r["has_mapping"] is True
    assert r["match"] == "match"


def test_reclassify_leaves_other_rows_untouched() -> None:
    rows = [_row(), _row(filename="FC__2.jpg", filepath="/imgs/FC__2.jpg")]
    out = ModelEvaluatorDialog._apply_change(
        rows,
        {
            "action": "reclassified",
            "old_path": "/imgs/WIN__1.jpg",
            "new_path": "/imgs/FCX__1.jpg",
            "new_headstamp": "FCX",
        },
        {},
    )
    assert out is not None  # "reclassified" is a handled action, never a no-op
    assert out[1] == rows[1]  # second row identical


def test_unknown_action_is_noop() -> None:
    assert ModelEvaluatorDialog._apply_change([_row()], {"action": "huh"}, {}) is None
