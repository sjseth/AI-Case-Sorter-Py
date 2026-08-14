"""Moderator notes: the client-side acknowledgement store.

The server sends every live note for a model on every ``FetchModelSettings``
and holds **no acknowledgement state** — acking is entirely ours, recorded
against the server's stable ``note.id``.

Storage is one ``SettingsRepo`` row per model, key
``community_notes:<community_model_uid>``, holding
``[{"id", "note", "created", "acknowledged"}]``. No table: the list is a
handful of short strings per model.

Merge rules (``merge``): an incoming note updates the stored text/date and
**keeps** its acknowledged flag; a note the server no longer sends stays in the
store, so the history view keeps showing it. Order is stable — stored notes
first, new ones appended — and any display ordering is the caller's business.

UI-free on purpose: the dialog renders these, nothing here knows about it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .community_api import ModeratorNote

KEY_PREFIX = "community_notes"


def settings_key(community_model_uid: str) -> str:
    return f"{KEY_PREFIX}:{community_model_uid}"


@dataclass
class StoredNote:
    id: int
    note: str
    created: str = ""
    acknowledged: bool = False

    @classmethod
    def from_json(cls, entry: Any) -> StoredNote | None:
        """One stored row, or ``None`` for anything malformed.

        A hand-edited settings row must not be able to break the Sort page, so
        every field is validated rather than trusted.
        """
        if not isinstance(entry, dict):
            return None
        raw_id = entry.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            return None
        try:
            note_id = int(raw_id)
        except ValueError:
            return None
        text = entry.get("note")
        if not isinstance(text, str) or not text.strip():
            return None
        created = entry.get("created")
        return cls(
            id=note_id,
            note=text.strip(),
            created=str(created or ""),
            acknowledged=bool(entry.get("acknowledged", False)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "note": self.note,
            "created": self.created,
            "acknowledged": self.acknowledged,
        }


def _repo(db: Any) -> Any | None:
    if db is None:
        return None
    from ..data.repository import SettingsRepo

    return SettingsRepo(db)


def load(db: Any, community_model_uid: str) -> list[StoredNote]:
    """Every note stored for this model, acknowledged or not."""
    repo = _repo(db)
    if repo is None or not community_model_uid:
        return []
    raw = repo.get(settings_key(community_model_uid))
    if not isinstance(raw, list):
        return []
    notes = [StoredNote.from_json(entry) for entry in raw]
    return [note for note in notes if note is not None]


def save(db: Any, community_model_uid: str, notes: Sequence[StoredNote]) -> None:
    repo = _repo(db)
    if repo is None or not community_model_uid:
        return
    repo.set(settings_key(community_model_uid), [note.to_json() for note in notes])


def merge(db: Any, community_model_uid: str, incoming: Sequence[ModeratorNote]) -> list[StoredNote]:
    """Fold a fetch's notes into the store and return the whole list.

    Incoming text/date win, the stored acknowledgement survives, and notes the
    server has stopped sending are kept as history.
    """
    stored = load(db, community_model_uid)
    by_id = {note.id: note for note in stored}
    for note in incoming:
        existing = by_id.get(note.id)
        if existing is None:
            new = StoredNote(id=note.id, note=note.note, created=note.created)
            by_id[note.id] = new
            stored.append(new)
            continue
        existing.note = note.note
        existing.created = note.created
    save(db, community_model_uid, stored)
    return stored


def unacknowledged(notes: Iterable[StoredNote]) -> list[StoredNote]:
    return [note for note in notes if not note.acknowledged]


def acknowledge(db: Any, community_model_uid: str, ids: Iterable[int]) -> list[StoredNote]:
    """Mark ``ids`` acknowledged so they never raise the modal again."""
    wanted = {int(i) for i in ids}
    notes = load(db, community_model_uid)
    for note in notes:
        if note.id in wanted:
            note.acknowledged = True
    save(db, community_model_uid, notes)
    return notes
