#!/usr/bin/env python3
"""Build the committed raster exports of the launcher mark from ``assets/``.

The artwork is two SVGs in ``assets/`` — ``app-icon.svg`` and its simplified
small cut ``app-icon-small.svg`` — which ``sorter.ui.icons`` reads at runtime.
Two rasters are committed beside them, and this tool is the only thing that
writes either:

``installer/casesorter.ico``
    ``install-windows.ps1`` points the Start Menu shortcut at it while it is
    still laying the app down, before any Python of ours runs.
``assets/app-icon-512.png``
    For documentation, which can't render an SVG everywhere it is shown.

Everything else — the hicolor rungs on Linux, the ``.icns`` in the macOS
bundle — is generated at launch by ``sorter.ui.desktop_integration`` from the
same SVGs. ``tests/unit/ui/test_icons.py`` fails if a committed raster no
longer matches what this tool renders.

Run this after editing the launcher artwork, and commit what changes::

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src uv run --no-sync python tools/make_app_icons.py

Two optional outputs, both look-at-it artifacts rather than assets — give them
paths outside the tree, and don't commit one:

``--preview PATH``
    A contact sheet of every size, so a rung that has stopped reading is visible
    rather than theoretical. That is the whole reason the artwork has a
    simplified small cut at all.
``--icns PATH``
    The macOS icon, for inspecting or for dropping into a real bundle build,
    without needing a Mac.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Windows' shell reads whichever member fits the surface; these are the sizes
# every ICO in the wild carries, and the ones Explorer actually asks for.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

ICO_PATH = ROOT / "installer" / "casesorter.ico"
PNG_SIZE = 512
PNG_PATH = ROOT / "assets" / f"app-icon-{PNG_SIZE}.png"


def _frames(sizes, icons):
    """Rasterise the launcher mark once per size, as RGBA Pillow images."""
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice

    frames = []
    for size in sizes:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not icons.launcher_pixmap(size).save(buffer, "PNG"):
            raise SystemExit(f"could not rasterise the launcher mark at {size} px")
        frames.append(Image.open(io.BytesIO(bytes(buffer.data().data()))).convert("RGBA"))
    return frames


def build_ico(target: Path, icons) -> None:
    frames = _frames(ICO_SIZES, icons)
    # Pillow's ICO writer resamples the largest frame down for `sizes` it is
    # given, which would throw away the simplified small cut. Handing it the
    # already-rendered members as `append_images` keeps each size's own artwork.
    frames[-1].save(target, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    print(f"wrote {target} ({', '.join(f'{s}x{s}' for s in ICO_SIZES)})")


def build_png(target: Path, icons) -> None:
    if not icons.launcher_pixmap(PNG_SIZE).save(str(target), "PNG"):
        raise SystemExit(f"could not write {target}")
    print(f"wrote {target}")


def build_icns(target: Path) -> None:
    from sorter.ui import desktop_integration

    desktop_integration.write_icns_with_pillow(target)
    print(f"wrote {target}")


def build_preview(target: Path, icons) -> None:
    from PIL import Image

    sizes = (512, 128, 64, 48, 32, 24, 16)
    tile = 160
    sheet = Image.new("RGBA", (tile * len(sizes), tile), (255, 255, 255, 255))
    for column, frame in enumerate(_frames(sizes, icons)):
        # Nearest-neighbour on the way up: this is a sheet for judging the
        # small members, and a smooth upscale would flatter them.
        scaled = frame.resize((tile, tile), Image.Resampling.NEAREST)
        sheet.paste(scaled, (column * tile, 0), scaled)
    sheet.save(target)
    print(f"wrote {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", metavar="PATH", type=Path, help="also write a contact sheet of every size here")
    parser.add_argument("--icns", metavar="PATH", type=Path, help="also write the macOS icon here")
    args = parser.parse_args()

    from PySide6.QtGui import QGuiApplication

    from sorter.ui import icons

    # QPixmap needs one; offscreen so this runs in a terminal or in CI.
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    assert app is not None

    build_ico(ICO_PATH, icons)
    build_png(PNG_PATH, icons)
    if args.icns is not None:
        build_icns(args.icns)
    if args.preview is not None:
        build_preview(args.preview, icons)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
