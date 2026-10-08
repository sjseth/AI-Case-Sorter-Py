"""Filter-box matching shared by the slot editors.

Both assignment surfaces — the per-slot dialog and the headstamp-first one —
filter the same names the same way, so a search that finds a headstamp in one
finds it in the other.
"""

from __future__ import annotations

from collections.abc import Iterable


def words_match(needle: str, *texts: str) -> bool:
    """True when one of ``texts`` contains every whitespace-separated word of ``needle``.

    Case-insensitive and order-free: ``win 9`` matches ``WIN 9MM LUGER``. An
    empty needle matches everything.
    """
    words = needle.casefold().split()
    return any(all(word in text.casefold() for word in words) for text in texts) if words else True


def matching(entries: Iterable[dict], needle: str) -> list[dict]:
    """The entries whose ``name`` matches ``needle`` (see ``words_match``)."""
    return [entry for entry in entries if words_match(needle, entry["name"])]
