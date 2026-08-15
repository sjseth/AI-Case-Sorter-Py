"""Guards on tools/gh_attach_images.py.

Only the parts that decide *whether* to upload are exercised: the upload
itself needs a real token and a real repository, and an accidental one is
permanent (the endpoint has no delete). See .claude/skills/ui-screenshots.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = ROOT / "tools" / "gh_attach_images.py"

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("gh_attach_images", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_real_png_is_accepted(tool, tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_HEADER + b"\x00" * 32)

    assert tool.looks_like(shot)


def test_a_renamed_non_image_is_refused(tool, tmp_path: Path) -> None:
    """`mv secrets.env shot.png` must not reach a permanent, public upload."""
    disguised = tmp_path / "shot.png"
    disguised.write_text("API_TOKEN=hunter2\n")

    assert not tool.looks_like(disguised)


def test_an_empty_file_is_refused(tool, tmp_path: Path) -> None:
    empty = tmp_path / "shot.png"
    empty.touch()

    assert not tool.looks_like(empty)


def test_webp_needs_both_of_its_markers(tool, tmp_path: Path) -> None:
    """RIFF alone is also .wav and .avi; the WEBP tag at offset 8 is the
    half that says which container this is."""
    riff_only = tmp_path / "shot.webp"
    riff_only.write_bytes(b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 8)
    real = tmp_path / "real.webp"
    real.write_bytes(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8)

    assert not tool.looks_like(riff_only)
    assert tool.looks_like(real)


def test_svg_is_not_an_accepted_extension(tool) -> None:
    """The endpoint takes it, but it has no magic number to verify and is a
    scriptable document rather than a picture."""
    assert ".svg" not in tool.CONTENT_TYPES


def test_every_accepted_extension_has_a_signature(tool) -> None:
    """Otherwise looks_like() raises KeyError on the one that doesn't."""
    assert set(tool.CONTENT_TYPES) == set(tool.MAGIC)
