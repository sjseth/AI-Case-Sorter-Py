"""The sidebar's vector icons: they render, and the theme's ink reaches them.

Nothing here asserts what a motif looks like — that is Seth's concept art, not
a fixture. What is pinned is the mechanism: one document per name, the color
token substituted at render time, and a real rasterised pixmap out the far end.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from sorter.ui import icons

SIDEBAR_SIZE = 26


def _bytes(pixmap) -> bytes:
    return bytes(pixmap.toImage().constBits())


def test_every_name_is_declared(qapp) -> None:
    assert set(icons.ICON_NAMES) == {
        icons.APP,
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


@pytest.mark.parametrize("size", [16, 32, 64])
def test_the_application_mark_survives_taskbar_sizes(qapp, size: int) -> None:
    pixmap = icons.pixmap(icons.APP, icons.APP_ICON_COLOR, size)

    assert not pixmap.isNull()
    assert any(_bytes(pixmap))


def test_the_application_icon_carries_every_size(qapp) -> None:
    icon = icons.app_icon()

    assert not icon.isNull()
    for size in (16, 32, 64):
        assert not icon.pixmap(size, size).isNull()
    # Fixed neutral, deliberately: a taskbar owns its own background.
    assert icons.APP_ICON_COLOR in icons.svg_document(icons.APP, icons.APP_ICON_COLOR)
