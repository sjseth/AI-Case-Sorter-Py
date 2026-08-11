"""HTML evaluation report generation (verbatim legacy format)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("PIL")

from sorter.ml import eval_report  # noqa: E402


def _results(img_dir: Path) -> list[dict]:
    for n in ("WIN__1.jpg", "FC__2.jpg"):
        cv2.imwrite(str(img_dir / n), np.full((20, 20, 3), 120, np.uint8))
    return [
        {
            "filename": "WIN__1.jpg",
            "filepath": str(img_dir / "WIN__1.jpg"),
            "predicted": "WIN",
            "confidence": 97.0,
            "original": "WIN",
            "raw_original": "WIN",
            "has_mapping": True,
            "match": "match",
        },
        {
            "filename": "FC__2.jpg",
            "filepath": str(img_dir / "FC__2.jpg"),
            "predicted": "Brass",
            "confidence": 40.0,
            "original": "FC",
            "raw_original": "FC",
            "has_mapping": False,
            "match": "mismatch",
        },
    ]


def test_generate_report_writes_expected_html(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    img_dir.mkdir()
    out = tmp_path / "reports" / "r.html"  # parent dir created by generate_report
    eval_report.generate_report(_results(img_dir), out)

    html = out.read_text(encoding="utf-8")
    assert "<title>Batch Image Classification Report</title>" in html
    assert "<h1>Batch Inference Report</h1>" in html

    m = re.search(r"const data = (\[.*?\]);", html, re.S)
    assert m, "embedded data array not found"
    data = json.loads(m.group(1))
    assert {d["classification"] for d in data} == {"WIN", "Brass"}

    win = next(d for d in data if d["filename"] == "WIN__1.jpg")
    # confidence rescaled to the report's 0..1 convention
    assert abs(win["confidence"] - 0.97) < 1e-9
    # thumbnail embedded as a self-contained data URI
    assert win["thumbnail"].startswith("data:image/jpeg;base64,")
    assert win["original_classification"] == "WIN"
    assert win["has_mapping"] is True


def test_report_row_key_order_matches_winforms(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    img_dir.mkdir()
    rows = eval_report.report_rows(_results(img_dir))
    # Key order must match the legacy report for byte-stable output.
    assert list(rows[0].keys()) == [
        "filename",
        "filepath",
        "thumbnail",
        "classification",
        "confidence",
        "original_classification",
        "raw_original_classification",
        "has_mapping",
    ]
