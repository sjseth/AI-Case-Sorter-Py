"""Apply a staged update — the pre-launch half of the updater.

Imported and run by ``bootstrap.py`` *before* ``uv sync``, so a staged
update's own ``pyproject.toml``/``uv.lock`` is what gets synced on that same
launch. (``__main__.py --apply-update`` still invokes it too, as a
compatibility entry point for anything that launched the old way;
``bootstrap.py`` is the live route.)

**This module must stay stdlib-only.** It runs against a virtualenv that may
hold nothing at all yet — importing ``requests`` here would break the very
launch it is supposed to fix. That is also why it re-reads ``pending.json``
itself instead of importing ``sorter.update.updater``. It is grouped under
``sorter/update/`` with ``updater.py`` (which does need ``requests``) purely
for subsystem organization — ``sorter/update/__init__.py`` is and must stay
empty, so importing this module alone never drags ``updater.py`` (or its
``requests`` import) in with it.

Why a separate pre-launch step at all: on Windows the venv's ``.pyd``/``.dll``
files (opencv, numpy) are locked while the app is running, so replacing files
in-place is unreliable. Applying before Python loads anything sidesteps
file-locking entirely.

Safety model:

* Everything about to be overwritten or pruned is copied to
  ``<data>/updates/backup/`` first, and restored if any step raises.
* Pruning is confined to ``PRUNE_ROOTS`` — package directories that never
  hold user data — so removed-upstream modules don't linger as importable
  stale code. Every other path is overwrite-only.
* The exit code is **always 0**. A broken updater must never stop the app
  from starting; it reports on stderr and leaves the previous version running.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .. import __version__, paths

# Never overwritten, never pruned, regardless of what an archive contains.
PROTECTED_TOP_LEVEL = frozenset({".git", ".venv", "venv", ".uv", "data", ".env", "portable.txt"})

# Pruning stale files is confined to these directories. This mirrors the
# layout a *release archive* ships (the sdist's `src/sorter/`), not this
# module's own path within the source tree.
#
# The flat `sorter/` entry is for installs predating #58: the update that
# moves them to the src layout is applied by *their* copy of this module,
# whose PRUNE_ROOTS was `("sorter",)`, so it empties the flat package on the
# way in and this version never sees it. What it leaves behind is whatever
# the old `_stamp_version` rebuilt afterwards (`sorter/_version.py`) plus any
# now-empty directories. Listing it here means the *next* update sweeps that
# residue instead of leaving an importable-looking `sorter/` shell next to
# the real one forever. A no-op on installs that never had one.
PRUNE_ROOTS = ("src/sorter", "sorter")


def _log(msg: str) -> None:
    print(f"[update] {msg}", file=sys.stderr)


def _meta_path() -> Path:
    return paths.updates_dir() / "pending.json"


def _pending_dir() -> Path:
    return paths.updates_dir() / "pending"


def _backup_dir() -> Path:
    return paths.updates_dir() / "backup"


def _is_protected(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0] in PROTECTED_TOP_LEVEL


def _archive_files(root: Path) -> list[Path]:
    """Relative paths of every file in the staged tree, protected ones dropped."""
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _is_protected(rel):
            continue
        out.append(rel)
    return out


def _stale_files(app_dir: Path, keep: set[Path]) -> list[Path]:
    """Files under PRUNE_ROOTS that the new version no longer ships."""
    out: list[Path] = []
    for root_name in PRUNE_ROOTS:
        root = app_dir / root_name
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(app_dir)
            if _is_protected(rel) or rel in keep:
                continue
            out.append(rel)
    return out


def _backup(app_dir: Path, backup: Path, rel: Path) -> None:
    dest = backup / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(app_dir / rel, dest)


def _rollback(app_dir: Path, backup: Path, created: list[Path]) -> int:
    """Undo a half-applied update: drop new files, restore saved ones.

    Returns the number of files it could **not** restore. A non-zero count
    means the install is genuinely inconsistent, which the caller reports
    loudly — the backup is left on disk so it can be recovered by hand.
    """
    failures = 0
    for rel in created:
        try:
            (app_dir / rel).unlink(missing_ok=True)
        except OSError:
            failures += 1
    if not backup.is_dir():
        return failures
    for src in backup.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(backup)
        dest = app_dir / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError as exc:
            failures += 1
            _log(f"rollback could not restore {rel}: {exc}")
    return failures


# Where `_version.py` goes, per layout. Order matters only as a tie-break for
# an archive carrying neither package marker, which `updater` rejects anyway.
_VERSION_STAMPS = (
    ("src/sorter/__init__.py", "src/sorter/_version.py"),
    ("sorter/__init__.py", "sorter/_version.py"),
)


def _version_stamp_for(shipped: set[Path]) -> str:
    """Pick the stamp path matching the layout the archive actually laid down.

    Not a constant, because this module applies archives of *either* layout:
    the version picker (CLAUDE.md §7) can install a release older than the
    #58 move, which lays down a flat `sorter/` and no `src/`. Stamping
    `src/sorter/_version.py` in that case writes a file the tree it just
    installed does not read, so the install reports `0.0.0+unknown` — the
    exact re-prompt loop `_stamp_version` exists to break, with the added
    twist of leaving a stray `src/` behind.
    """
    for marker, stamp in _VERSION_STAMPS:
        if Path(marker) in shipped:
            return stamp
    return _VERSION_STAMPS[0][1]


def _stamp_version(app_dir: Path, version: str, shipped: set[Path]) -> None:
    """Record the applied version when the archive didn't carry one.

    ``sorter/__init__.py`` reads ``src/sorter/_version.py`` first, and the sdist
    ships it: hatch-vcs's build hook stamps it into every build target, so
    the released asset carries it with nothing to keep in sync. GitHub's
    auto-generated *source* archive — what ``updater._pick_asset`` falls
    back to when the named asset isn't published — contains neither that
    file nor ``.git``, so an install updated from it would keep reporting
    ``0.0.0+unknown``. That sentinel parses as a pre-release, meaning every
    subsequent check sees the same release as "newer" and the update prompt
    returns on every launch, each update a no-op. Writing the version we
    just applied breaks that loop.

    Skipped when the archive shipped its own ``_version.py`` — that one came
    from a real build and is authoritative.
    """
    stamp = _version_stamp_for(shipped)
    if Path(stamp) in shipped:
        return
    dest = app_dir / stamp
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            '"""Written by sorter.update.apply_update — the applied release version.\n\n'
            "Normally generated by hatch-vcs at build time; this copy exists because\n"
            "the archive this update came from carried no version of its own.\n"
            '"""\n\n'
            f'__version__ = "{version}"\n',
            encoding="utf-8",
        )
    except OSError as exc:
        # Non-fatal: a wrong version is a nagging update prompt, not a broken
        # install, and this module must never stop the app launching.
        _log(f"could not record the applied version ({exc}); the update prompt may recur.")


def apply_pending(app_dir: Path | None = None) -> bool:
    """Apply the staged update if there is one. Returns True if applied."""
    root = Path(app_dir) if app_dir is not None else paths.app_root()
    meta_path = _meta_path()
    pending = _pending_dir()

    if not meta_path.is_file() or not pending.is_dir():
        return False

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log(f"staged update metadata is unreadable ({exc}); discarding it.")
        _discard()
        return False

    version = str(meta.get("version") or "")
    if not version:
        _log("staged update has no version; discarding it.")
        _discard()
        return False

    files = _archive_files(pending)
    if not files:
        _log("staged update is empty; discarding it.")
        _discard()
        return False

    keep = set(files)
    stale = _stale_files(root, keep)

    backup = _backup_dir()
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    _log(f"applying {__version__} → {version} ({len(files)} files)")
    try:
        for rel in stale:
            _backup(root, backup, rel)
            (root / rel).unlink()

        for rel in files:
            dest = root / rel
            if dest.exists():
                _backup(root, backup, rel)
            else:
                created.append(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pending / rel, dest)
    except (OSError, shutil.Error) as exc:
        _log(f"failed: {exc}")
        _log("rolling back to the previous version…")
        failures = _rollback(root, backup, created)
        if failures:
            _log(
                f"WARNING: rollback could not restore {failures} file(s). "
                f"A copy of the previous version is at {backup}."
            )
        else:
            _log("rollback complete; keeping the staged update for a later retry.")
        return False

    _stamp_version(root, version, shipped=keep)

    _discard()
    try:
        (paths.updates_dir() / "last_applied.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "tag": str(meta.get("tag") or version),
                    "from_version": str(meta.get("from_version") or __version__),
                    "pruned": len(stale),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    _log(f"updated to {version}." + (f" Pruned {len(stale)} stale file(s)." if stale else ""))
    return True


def _discard() -> None:
    shutil.rmtree(_pending_dir(), ignore_errors=True)
    try:
        _meta_path().unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Entry point. Always returns 0 — never block the app from launching."""
    args = list(sys.argv[1:] if argv is None else argv)
    app_dir: Path | None = None
    if "--app-dir" in args:
        idx = args.index("--app-dir")
        if idx + 1 < len(args):
            app_dir = Path(args[idx + 1])

    try:
        # A pre-0.2 install still has its data inside the app folder; move it
        # before looking for staged updates, so both halves agree on where
        # the updates/ tree lives.
        paths.migrate_legacy_data_dir()
        apply_pending(app_dir)
    except Exception as exc:
        _log(f"unexpected error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
