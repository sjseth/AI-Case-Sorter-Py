"""File operations for a model's training images.

Images use the ``{headstamp}__{ticks}.ext`` convention. Reclassifying renames
the prefix; deleting removes the file. Pure and cross-platform (pathlib only),
so the UI image-manager and evaluator share one source of truth.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

IMAGE_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

ALL_FILTER = "All"
UNKNOWN_FILTER = "UNKNOWN HEADSTAMP"


def parse_headstamp(path: Path | str) -> str:
    """The classification prefix encoded in a filename.

    ``WIN__123.jpg`` -> ``WIN``. Files without the ``__`` separator have no
    headstamp; their stem is returned so they still display/sort sensibly.
    """
    stem = Path(path).stem
    return stem.split("__", 1)[0] if "__" in stem else stem


def has_headstamp(path: Path | str) -> bool:
    return "__" in Path(path).stem


def list_images(folder: Path | str, extensions: Iterable[str] = IMAGE_EXTS) -> list[Path]:
    d = Path(folder)
    if not d.is_dir():
        return []
    exts = {e.lower() for e in extensions}
    return sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.name.casefold(),
    )


def filter_images(
    files: list[Path],
    selected: str,
    known_headstamps: Iterable[str],
) -> list[Path]:
    """Filter by the headstamp dropdown selection.

    ``All`` -> everything; ``UNKNOWN HEADSTAMP`` -> files whose prefix is not a
    known headstamp (or have no prefix); otherwise files with that exact prefix.
    """
    if not selected or selected == ALL_FILTER:
        return list(files)
    if selected == UNKNOWN_FILTER:
        known = set(known_headstamps)
        return [p for p in files if not has_headstamp(p) or parse_headstamp(p) not in known]
    prefix = f"{selected}__"
    return [p for p in files if p.name.startswith(prefix)]


def reclassify(path: Path | str, new_headstamp: str) -> Path | None:
    """Rename ``{old}__{rest}`` to ``{new}__{rest}``.

    Returns the new path, or None when nothing changed / the target already
    exists / the file has no ``__`` prefix / the rename failed.
    """
    p = Path(path)
    name = p.name
    if "__" not in name or not new_headstamp:
        return None
    idx = name.index("__")
    if name[:idx] == new_headstamp:
        return None
    dest = p.with_name(new_headstamp + name[idx:])
    if dest.exists():
        return None
    try:
        p.rename(dest)
        return dest
    except OSError:
        return None


def delete(path: Path | str) -> bool:
    try:
        Path(path).unlink()
        return True
    except OSError:
        return False
