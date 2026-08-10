"""Model image manager helpers (thumbnail decode).

Importing the module is display-free, but it pulls in the GUI stack, so skip
where tkinter/cv2/PIL aren't importable. ``_decode_thumb`` itself only needs
PIL and is the cross-platform thumbnail path used off the UI thread.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")
Image = pytest.importorskip("PIL.Image")

from sorter.ui import dialog_model_images  # noqa: E402


def _write(path: Path, size: tuple[int, int], color=(200, 50, 50)) -> None:
    Image.new("RGB", size, color).save(path)


def test_decode_thumb_letterboxes_to_square(tmp_path: Path) -> None:
    src = tmp_path / "WIN__1.jpg"
    _write(src, (200, 100))  # wide image
    thumb = dialog_model_images._decode_thumb(str(src), size=120)
    assert thumb is not None
    assert thumb.size == (120, 120)  # always padded to a square


def test_decode_thumb_handles_portrait(tmp_path: Path) -> None:
    src = tmp_path / "FC__2.png"
    _write(src, (60, 240))  # tall image
    thumb = dialog_model_images._decode_thumb(str(src), size=80)
    assert thumb is not None
    assert thumb.size == (80, 80)


def test_decode_thumb_bad_path_returns_none(tmp_path: Path) -> None:
    assert dialog_model_images._decode_thumb(str(tmp_path / "missing.jpg")) is None
