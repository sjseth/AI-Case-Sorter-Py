"""Monochrome vector icons for the Qt shell, colored by the active theme.

Each icon is a hand-authored, stroke-only SVG on a 24x24 viewBox carrying the
literal token ``{color}`` where its ink goes; :func:`icon` substitutes the
caller's color and rasterises through ``QSvgRenderer``. One document per
motif, colored at render time — never a per-theme image asset, so a new or
edited palette themes the sidebar with nothing to redraw.

The motifs are Seth's concept art (2026-08-13): Sort and Train carry the
machine's own identity (a cartridge case, a headstamp seen base-on) rather
than generic glyphs; AI Config, Models, Community and Settings use familiar
metaphors.

The launcher mark at the foot of this file is the exception to every word of
that: filled, full-colour and untouched by the palette, because the desktop
draws it on a background of its own (``app_icon``, ``launcher_svg``).
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

SORT = "sort"
TRAIN = "train"
AI_CONFIG = "ai_config"
MODELS = "models"
COMMUNITY = "community"
SETTINGS = "settings"

COLOR_TOKEN = "{color}"

# Shared drawing style: line art, no fills, round ends — the whole set reads as
# one hand. Per-shape overrides go on the shape, not here.
_STYLE = 'fill="none" stroke="{color}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"'


def _svg(body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" {_STYLE}>{body}</svg>'


_SVGS: dict[str, str] = {
    # Upright cartridge case, with a forking arrow routing right into two bins.
    SORT: _svg(
        """
        <path d="M3.6 6.2a1.6 1.6 0 0 1 1.6-1.6h1.8a1.6 1.6 0 0 1 1.6 1.6V20H3.6Z"/>
        <path d="M3.6 7.7h5" stroke-width="1.4"/>
        <path d="M3.6 17.3h5" stroke-width="1.4"/>
        <path d="M8.9 12h2.5"/>
        <path d="M11.4 8v8"/>
        <path d="M11.4 8h5.2"/>
        <path d="M11.4 16h5.2"/>
        <path d="M15.4 6.3 17.1 8l-1.7 1.7"/>
        <path d="M15.4 14.3 17.1 16l-1.7 1.7"/>
        """
    ),
    # A headstamp base-on (rim, primer ring, primer) in a crosshair, plus a
    # sparkle: the machine learning to read the stamp.
    TRAIN: _svg(
        """
        <circle cx="11" cy="13" r="6.25"/>
        <circle cx="11" cy="13" r="2.75"/>
        <circle cx="11" cy="13" r="0.85" fill="{color}" stroke="none"/>
        <path d="M11 4.5V6.25"/>
        <path d="M11 19.75v1.75"/>
        <path d="M2.5 13h1.75"/>
        <path d="M17.75 13h1.75"/>
        <path d="M19.6 2.4C19.6 4.7 19.6 4.7 21.9 4.7 19.6 4.7 19.6 4.7 19.6 7
                 19.6 4.7 19.6 4.7 17.3 4.7 19.6 4.7 19.6 4.7 19.6 2.4Z"
              stroke-width="1.5"/>
        """
    ),
    # A prompt bubble: the chat-shaped sibling of TRAIN, since AI Config is
    # what takes the Train activity's place in AI Config mode (Seth).
    AI_CONFIG: _svg(
        """
        <path d="M5.2 4.6h13.6a1.7 1.7 0 0 1 1.7 1.7v8.4a1.7 1.7 0 0 1-1.7 1.7h-8.3l-4.1 3.3v-3.3h-1.2
                 a1.7 1.7 0 0 1-1.7-1.7V6.3a1.7 1.7 0 0 1 1.7-1.7Z"/>
        <path d="M8.5 8.6 10.9 10.7 8.5 12.8" stroke-width="1.5"/>
        <path d="M12.6 13h4" stroke-width="1.5"/>
        """
    ),
    # Three cubes stacked on a platform: the model library as things on a shelf.
    MODELS: _svg(
        """
        <path d="M7.9 10.35 10.9 12 10.9 15.4 7.9 17.05 4.9 15.4 4.9 12Z" stroke-width="1.6"/>
        <path d="M4.9 12 7.9 13.65 10.9 12M7.9 13.65 7.9 17.05" stroke-width="1.2"/>
        <path d="M16.1 10.35 19.1 12 19.1 15.4 16.1 17.05 13.1 15.4 13.1 12Z" stroke-width="1.6"/>
        <path d="M13.1 12 16.1 13.65 19.1 12M16.1 13.65 16.1 17.05" stroke-width="1.2"/>
        <path d="M12 5.3 15 6.95 15 10.35 12 12 9 10.35 9 6.95Z" stroke-width="1.6"/>
        <path d="M9 6.95 12 8.6 15 6.95M12 8.6 12 12" stroke-width="1.2"/>
        <path d="M12 18.6 20 20.3 12 22 4 20.3Z" stroke-width="1.6"/>
        <path d="M4 20.3v0.9L12 22.9 20 21.2v-0.9" stroke-width="1.2"/>
        """
    ),
    # A globe with people badges in orbit: models shared by other reloaders.
    COMMUNITY: _svg(
        """
        <circle cx="8.8" cy="13" r="5"/>
        <path d="M3.8 13h10" stroke-width="1.3"/>
        <ellipse cx="8.8" cy="13" rx="2.2" ry="5" stroke-width="1.3"/>
        <circle cx="18.8" cy="6.6" r="3.1" stroke-width="1.3"/>
        <circle cx="18.8" cy="5.5" r="0.85" stroke-width="1.3"/>
        <path d="M17.35 8.4a1.45 1.45 0 0 1 2.9 0" stroke-width="1.3"/>
        <circle cx="18.8" cy="16.6" r="3.1" stroke-width="1.3"/>
        <circle cx="18.8" cy="15.5" r="0.85" stroke-width="1.3"/>
        <path d="M17.35 18.4a1.45 1.45 0 0 1 2.9 0" stroke-width="1.3"/>
        """
    ),
    # A drawn gear — eight teeth around a hub.
    SETTINGS: _svg(
        """
        <path d="M10.57 7.31 L10.8 5.2 L13.2 5.2 L13.43 7.31 L14.3 7.67 L15.96 6.35
                 L17.65 8.04 L16.33 9.7 L16.69 10.57 L18.8 10.8 L18.8 13.2 L16.69 13.43
                 L16.33 14.3 L17.65 15.96 L15.96 17.65 L14.3 16.33 L13.43 16.69
                 L13.2 18.8 L10.8 18.8 L10.57 16.69 L9.7 16.33 L8.04 17.65 L6.35 15.96
                 L7.67 14.3 L7.31 13.43 L5.2 13.2 L5.2 10.8 L7.31 10.57 L7.67 9.7
                 L6.35 8.04 L8.04 6.35 L9.7 7.67 Z"/>
        <circle cx="12" cy="12" r="2.6"/>
        """
    ),
}

ICON_NAMES: tuple[str, ...] = tuple(_SVGS)


def svg_document(name: str, color: str) -> str:
    """The named icon's SVG with its ink token substituted."""
    try:
        template = _SVGS[name]
    except KeyError:
        raise KeyError(f"unknown icon {name!r}; known: {', '.join(ICON_NAMES)}") from None
    return template.replace(COLOR_TOKEN, color)


def _device_pixel_ratio() -> float:
    """Rasterise at the screen's ratio so the strokes stay crisp on HiDPI."""
    screen = QGuiApplication.primaryScreen() if QGuiApplication.instance() is not None else None
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


def pixmap(name: str, color: str, size: int) -> QPixmap:
    """Render the named icon at ``size`` logical px, on a transparent ground."""
    document = svg_document(name, color)
    ratio = _device_pixel_ratio()
    edge = max(1, round(size * ratio))
    canvas = QPixmap(edge, edge)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        QSvgRenderer(QByteArray(document.encode("utf-8"))).render(painter, QRectF(0, 0, edge, edge))
    finally:
        painter.end()
    # After painting, not before: the painter would otherwise scale device
    # coordinates a second time on top of the ratio already baked into `edge`.
    canvas.setDevicePixelRatio(ratio)
    return canvas


def icon(name: str, color: str, size: int) -> QIcon:
    """A QIcon of the named motif, inked in ``color`` at ``size`` logical px."""
    return QIcon(pixmap(name, color, size))


# ---------------------------------------------------------------------------
# The launcher mark — the one icon an operating system shows
# ---------------------------------------------------------------------------
#
# Everything above is themed line art drawn on our own surfaces. This is the
# opposite by necessity: a taskbar tile, a Start Menu entry and a Dock tile are
# drawn on a background the desktop owns, at sizes it picks, in a row of icons
# that are all filled and saturated — where a mid-gray stroke drawing reads as
# a disabled one. So the same motif (a headstamp base-on: rim, groove, primer
# ring, primer) is redrawn filled, in brass on gunmetal, carrying its own
# colors instead of the palette's.
#
# Two documents, and the threshold between them is the point: the groove and
# primer ring that make the detailed mark a *case head* collapse into mud below
# 48 px — rendered and looked at, not assumed — where three high-contrast discs
# still read as one. Every consumer picks through :func:`launcher_svg`, so the
# .ico, the hicolor PNGs and the .icns all switch at the same size.
LAUNCHER_DETAIL_MIN = 48

# The sizes the OS asset pipelines want between them: the Icon Theme Spec's
# usual hicolor rungs, Windows' .ico set, and macOS's .icns set (which also
# wants 1024 for @2x — `desktop_integration` adds it rather than putting a size
# no other platform uses in the shared list).
LAUNCHER_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256, 512)

_LAUNCHER_DETAILED = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="plate" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3E444D"/><stop offset="1" stop-color="#1E2228"/>
    </linearGradient>
    <linearGradient id="rim" x1="0.15" y1="0.05" x2="0.85" y2="0.95">
      <stop offset="0" stop-color="#F5DE9C"/><stop offset="0.5" stop-color="#C89C48"/>
      <stop offset="1" stop-color="#7E581B"/>
    </linearGradient>
    <linearGradient id="face" x1="0.2" y1="0.12" x2="0.8" y2="0.9">
      <stop offset="0" stop-color="#DCB868"/><stop offset="0.55" stop-color="#B0863A"/>
      <stop offset="1" stop-color="#6E4E17"/>
    </linearGradient>
    <linearGradient id="primer" x1="0.25" y1="0.15" x2="0.75" y2="0.85">
      <stop offset="0" stop-color="#F6E3AD"/><stop offset="1" stop-color="#B08432"/>
    </linearGradient>
  </defs>
  <rect x="16" y="16" width="480" height="480" rx="112" fill="url(#plate)"/>
  <circle cx="256" cy="256" r="172" fill="url(#rim)"/>
  <circle cx="256" cy="256" r="140" fill="none" stroke="#241A06" stroke-width="14"/>
  <circle cx="256" cy="256" r="133" fill="url(#face)"/>
  <circle cx="256" cy="256" r="78" fill="#241A06"/>
  <circle cx="256" cy="256" r="64" fill="url(#primer)"/>
  <circle cx="256" cy="256" r="22" fill="#241A06"/>
</svg>
"""

_LAUNCHER_SIMPLE = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect x="8" y="8" width="496" height="496" rx="104" fill="#272C33"/>
  <circle cx="256" cy="256" r="184" fill="#D4A94F"/>
  <circle cx="256" cy="256" r="96" fill="#2A1E06"/>
  <circle cx="256" cy="256" r="70" fill="#E8CB84"/>
</svg>
"""


def launcher_svg(size: int) -> str:
    """The launcher artwork to use at ``size`` px — detailed, or the small cut."""
    return _LAUNCHER_DETAILED if size >= LAUNCHER_DETAIL_MIN else _LAUNCHER_SIMPLE


def launcher_pixmap(size: int) -> QPixmap:
    """The launcher mark at exactly ``size`` **physical** pixels.

    Deliberately not :func:`pixmap`, which bakes the screen's device pixel
    ratio in: right for a widget, wrong for a file on disk, where the size in
    the path (``48x48/apps/…``) is a promise about how many pixels are in it.
    """
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        document = QByteArray(launcher_svg(size).encode("utf-8"))
        QSvgRenderer(document).render(painter, QRectF(0, 0, size, size))
    finally:
        painter.end()
    return canvas


def app_icon() -> QIcon:
    """The window/taskbar icon — the launcher mark at every size a shell asks for."""
    result = QIcon()
    for size in LAUNCHER_SIZES:
        result.addPixmap(launcher_pixmap(size))
    return result
