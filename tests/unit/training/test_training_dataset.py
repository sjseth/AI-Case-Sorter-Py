"""Tests for the training dataset helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from sorter.training.dataset import (
    class_counts,
    dotnet_ticks,
    feedback_filename,
    parse_label,
    safe_label,
    save_feedback_image,
    save_training_image,
    training_filename,
)


def test_dotnet_ticks_for_known_date() -> None:
    """`new DateTime(2026,1,1,0,0,0,DateTimeKind.Utc).Ticks` == 639_028_224_000_000_000."""
    when = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert dotnet_ticks(when) == 639_028_224_000_000_000


def test_dotnet_ticks_at_unix_epoch_matches_known_offset() -> None:
    """1970-01-01 UTC must produce exactly the .NET Unix-epoch ticks constant."""
    when = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert dotnet_ticks(when) == 621_355_968_000_000_000


def test_filename_round_trip() -> None:
    fname = training_filename("Federal_45ACP")
    assert "__" in fname
    assert parse_label(fname) == "Federal_45ACP"


def test_parse_label_returns_none_for_legacy_names() -> None:
    assert parse_label("plainfile.jpg") is None


def test_save_training_image_writes_atomically(tmp_path: Path) -> None:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    dest = save_training_image(img, tmp_path / "train", "FOO")
    assert dest.exists()
    assert dest.parent == tmp_path / "train"
    assert dest.suffix == ".jpg"
    assert parse_label(dest.name) == "FOO"


def test_save_training_image_rejects_empty_label(tmp_path: Path) -> None:
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        save_training_image(img, tmp_path / "train", "")


def test_class_counts_groups_by_label(tmp_path: Path) -> None:
    d = tmp_path / "train"
    d.mkdir()
    (d / "FOO__1.jpg").write_bytes(b"x")
    (d / "FOO__2.jpg").write_bytes(b"x")
    (d / "BAR__3.png").write_bytes(b"x")
    (d / "junk.txt").write_bytes(b"x")
    (d / "no_label.jpg").write_bytes(b"x")
    counts = class_counts(d)
    assert counts == {"FOO": 2, "BAR": 1}


# ----- label sanitization -----------------------------------


def test_safe_label_passes_through_normal_headstamps() -> None:
    for label in ("9mm WIN", "R-P", "+P", "FC", "S&B"):
        assert safe_label(label) == label


def test_safe_label_neutralizes_traversal_and_separators() -> None:
    assert "/" not in safe_label("../../etc/passwd")
    assert "\\" not in safe_label("..\\..\\win")
    assert ".." not in safe_label("../../etc/passwd")


def test_safe_label_strips_illegal_chars_and_reserved_names() -> None:
    assert safe_label('x"<>|y') == "x____y"
    assert safe_label("CON") == "_CON"
    assert safe_label("") == "unknown"
    assert safe_label("   ") == "unknown"
    assert len(safe_label("A" * 500)) <= 100


def test_filenames_use_sanitized_label() -> None:
    when = datetime(2020, 1, 1, tzinfo=UTC)
    tf = training_filename("../evil", when=when)
    ff = feedback_filename("../evil", 42.7, when=when)
    assert "/" not in tf and ".." not in tf
    assert "/" not in ff and ".." not in ff


def test_save_training_image_writes_inside_target_dir(tmp_path: Path) -> None:
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    out = tmp_path / "run_images"
    dest = save_training_image(img, out, "../../escape")
    assert dest.resolve().parent == out.resolve()


def test_save_feedback_image_writes_inside_target_dir(tmp_path: Path) -> None:
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    out = tmp_path / "feedback_images"
    dest = save_feedback_image(img, out, "../../escape", 12.0)
    assert dest.resolve().parent == out.resolve()
