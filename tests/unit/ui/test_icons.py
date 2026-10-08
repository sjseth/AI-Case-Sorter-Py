"""The sidebar's vector icons: they render, and the theme's ink reaches them.

Nothing here asserts what a motif looks like — that is Seth's concept art, not
a fixture. What is pinned is the mechanism: one document per name, the color
token substituted at render time, and a real rasterised pixmap out the far end.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import pytest

pytest.importorskip("PySide6")

from sorter.ui import icons

SIDEBAR_SIZE = 26


def _bytes(pixmap) -> bytes:
    return bytes(pixmap.toImage().constBits())


def test_every_name_is_declared(qapp) -> None:
    assert set(icons.ICON_NAMES) == {
        icons.SORT,
        icons.TRAIN,
        icons.AI_CONFIG,
        icons.MODELS,
        icons.COMMUNITY,
        icons.SETTINGS,
    }


@pytest.mark.parametrize("name", list(icons.ICON_NAMES))
def test_icon_renders_at_the_sidebar_size(qapp, name: str) -> None:
    pixmap = icons.pixmap(name, "#d4d4d4", SIDEBAR_SIZE)

    assert not pixmap.isNull()
    assert (pixmap.width(), pixmap.height()) != (0, 0)
    # Transparent-only would mean the SVG parsed to nothing.
    assert any(_bytes(pixmap)), f"{name} rendered blank"
    assert not icons.icon(name, "#d4d4d4", SIDEBAR_SIZE).isNull()


@pytest.mark.parametrize("name", list(icons.ICON_NAMES))
def test_the_color_token_is_substituted(qapp, name: str) -> None:
    document = icons.svg_document(name, "#123456")

    assert icons.COLOR_TOKEN not in document
    assert "#123456" in document
    assert _bytes(icons.pixmap(name, "#ffffff", SIDEBAR_SIZE)) != _bytes(icons.pixmap(name, "#202020", SIDEBAR_SIZE))


@pytest.mark.parametrize("name", list(icons.ICON_NAMES))
def test_documents_are_stroke_only_line_art(name: str) -> None:
    document = icons.svg_document(name, "#abcdef")

    assert 'viewBox="0 0 24 24"' in document
    assert 'fill="none"' in document
    assert 'stroke-linecap="round"' in document


def test_unknown_name_raises(qapp) -> None:
    with pytest.raises(KeyError):
        icons.svg_document("nope", "#ffffff")
    with pytest.raises(KeyError):
        icons.icon("nope", "#ffffff", SIDEBAR_SIZE)


@pytest.mark.parametrize("size", list(icons.LAUNCHER_SIZES))
def test_the_launcher_mark_renders_at_every_size_it_ships(qapp, size: int) -> None:
    pixmap = icons.launcher_pixmap(size)

    assert not pixmap.isNull()
    assert any(_bytes(pixmap))


@pytest.mark.parametrize("size", list(icons.LAUNCHER_SIZES))
def test_the_launcher_mark_is_exactly_the_size_asked_for(qapp, size: int) -> None:
    """Physical pixels, not logical: these become files whose path states a size,
    and a HiDPI ratio baked in here would make every one of them a lie."""
    pixmap = icons.launcher_pixmap(size)

    assert (pixmap.width(), pixmap.height()) == (size, size)


def test_the_small_sizes_get_the_simplified_cut(qapp) -> None:
    """Not a style preference: below the threshold the groove and primer ring
    turn to mud, so those rungs carry different artwork — and must, or the .ico
    and the hicolor tree quietly ship the unreadable one."""
    assert icons.launcher_svg(icons.LAUNCHER_DETAIL_MIN - 1) != icons.launcher_svg(icons.LAUNCHER_DETAIL_MIN)
    assert icons.launcher_svg(16) == icons.launcher_svg(32)
    assert icons.launcher_svg(64) == icons.launcher_svg(512)


def test_the_launcher_mark_carries_its_own_colors(qapp) -> None:
    """The one icon the palette does not reach: the desktop draws it on a
    background of its own, where a themed ink would vanish."""
    for size in (16, 64):
        assert icons.COLOR_TOKEN not in icons.launcher_svg(size)


def test_the_application_icon_carries_every_size(qapp) -> None:
    icon = icons.app_icon()

    assert not icon.isNull()
    for size in icons.LAUNCHER_SIZES:
        assert not icon.pixmap(size, size).isNull()


# ---------------------------------------------------------------------------
# The committed artwork files
# ---------------------------------------------------------------------------
#
# The launcher SVGs live in assets/ so docs and packaging can point at them,
# and two rasters are committed from them by tools/make_app_icons.py. These pin
# that the files are there, are what they claim, and still match the SVGs.

ROOT = Path(__file__).resolve().parents[3]
SVG_NS = "{http://www.w3.org/2000/svg}"


def _load_tool():
    spec = importlib.util.spec_from_file_location("make_app_icons", ROOT / "tools" / "make_app_icons.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _premultiplied(image) -> np.ndarray:
    """RGBA as premultiplied ints, so colour under a fully transparent pixel can't differ."""
    rgba = np.asarray(image.convert("RGBA"), dtype=np.int32)
    return np.concatenate([rgba[..., :3] * rgba[..., 3:] // 255, rgba[..., 3:]], axis=-1)


def _assert_same_pixels(committed, rendered, what: str) -> None:
    assert committed.size == rendered.size, f"{what}: {committed.size} != {rendered.size}"
    # Not byte equality: encoders may differ between platforms. A real artwork
    # change moves pixels by far more than this.
    diff = int(np.abs(_premultiplied(committed) - _premultiplied(rendered)).max())
    assert diff <= 2, f"{what} no longer matches the SVG (max channel diff {diff}); re-run tools/make_app_icons.py"


def test_the_launcher_artwork_is_read_from_the_committed_svgs() -> None:
    assert icons.assets_dir() == ROOT / "assets"
    detailed = (ROOT / "assets" / icons.LAUNCHER_ASSET).read_text(encoding="utf-8")
    small = (ROOT / "assets" / icons.LAUNCHER_SMALL_ASSET).read_text(encoding="utf-8")

    assert icons.launcher_svg(icons.LAUNCHER_DETAIL_MIN) == detailed
    assert icons.launcher_svg(icons.LAUNCHER_DETAIL_MIN - 1) == small


@pytest.mark.parametrize("name", [icons.LAUNCHER_ASSET, icons.LAUNCHER_SMALL_ASSET])
def test_the_committed_svgs_are_valid_square_documents(qapp, name: str) -> None:
    from PySide6.QtSvg import QSvgRenderer

    path = ROOT / "assets" / name
    root = ElementTree.parse(path).getroot()

    assert root.tag == f"{SVG_NS}svg"
    assert root.get("viewBox") == "0 0 512 512"
    assert QSvgRenderer(str(path)).isValid()


def test_the_committed_png_is_what_the_tool_renders(qapp, tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    tool = _load_tool()
    tool.build_png(tmp_path / "rendered.png", icons)

    with Image.open(tool.PNG_PATH) as committed, Image.open(tmp_path / "rendered.png") as rendered:
        assert committed.size == (tool.PNG_SIZE, tool.PNG_SIZE)
        _assert_same_pixels(committed, rendered, tool.PNG_PATH.name)


def test_the_committed_ico_is_what_the_tool_renders(qapp, tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import IcoImagePlugin, Image

    tool = _load_tool()
    tool.build_ico(tmp_path / "rendered.ico", icons)

    with Image.open(tool.ICO_PATH) as committed, Image.open(tmp_path / "rendered.ico") as rendered:
        assert isinstance(committed, IcoImagePlugin.IcoImageFile)
        assert isinstance(rendered, IcoImagePlugin.IcoImageFile)
        assert {s[0] for s in committed.info["sizes"]} == set(tool.ICO_SIZES)
        for size in tool.ICO_SIZES:
            _assert_same_pixels(
                committed.ico.getimage((size, size)),
                rendered.ico.getimage((size, size)),
                f"casesorter.ico {size}x{size}",
            )


def test_a_missing_artwork_file_costs_the_window_icon_not_the_launch(qapp, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(icons, "assets_dir", lambda: tmp_path)
    icons._read_asset.cache_clear()
    try:
        assert icons.app_icon().isNull()
    finally:
        icons._read_asset.cache_clear()
