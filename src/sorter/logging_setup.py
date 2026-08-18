"""One-shot logging configuration for the app.

Two handlers, because they answer different questions:

* **stderr, INFO** — what a developer running from a terminal already saw,
  unchanged. ``bootstrap.py`` pipes the app's stdout/stderr into
  ``<data root>/logs/launch.log``, so on the normal double-click launch this
  half is persisted for free.
* **``<data root>/logs/casesorter.log``, DEBUG, rotating** — what ``launch.log``
  structurally cannot be. That file is rewritten on every launch with a single
  backup, so its history is two launches deep, and it *is* the console stream,
  so it can't carry DEBUG without spraying DEBUG at anyone watching. Rotation
  buys the history a bug report needs; the separate handler buys the level.

Under the data root rather than the app folder for the reason everything else
is (``paths.logs_dir``): the updater replaces the app folder wholesale.

**Configuring must never be the reason a launch fails.** An unwritable data
directory costs the file handler, not the app — the same rule ``bootstrap.py``
follows for ``launch.log``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sorter import paths

LOG_FILENAME = "casesorter.log"

#: Enough to span the several launches a user takes to reproduce a bug, and
#: still small enough to attach to a GitHub issue.
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_configured = False


def _console_level() -> int:
    """``CASESORTER_LOG_LEVEL`` over the INFO default, name or number."""
    raw = os.environ.get("CASESORTER_LOG_LEVEL", "").strip()
    if not raw:
        return logging.INFO
    if raw.isdigit():
        return int(raw)
    return getattr(logging, raw.upper(), logging.INFO)


def log_path() -> Path:
    return paths.logs_dir() / LOG_FILENAME


def configure(force: bool = False) -> None:
    """Install the handlers on the ``sorter`` logger. Idempotent.

    Scoped to ``sorter`` rather than the root logger so a third-party library's
    output isn't silently adopted into our log file — and so the tests, which
    import the package without ever calling this, keep whatever handlers pytest
    installed.
    """
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger("sorter")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG)
    # Ours alone: propagating would hand every record to whatever the root
    # logger is configured with, which for a library consumer is not our call.
    root.propagate = False

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(_console_level())
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        directory = paths.logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / LOG_FILENAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        # Deliberately through the handler we did manage to install: losing the
        # file is worth one line, and raising here would cost the launch.
        root.warning("no log file (%s); logging to stderr only", exc)
    else:
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _apply_subsystem_levels()
    _configured = True


def _apply_subsystem_levels() -> None:
    """Per-logger levels for the knobs that predate this module.

    The feedback tracing was a print gated on an environment variable; the
    variable survives as a level, so the same knob does the same thing. Its
    default is INFO rather than DEBUG so an opted-out user's log doesn't fill
    with capture decisions on every case — which is what the flag meant when
    it gated a print, and has to keep meaning now that a DEBUG file handler
    would otherwise keep everything.

    Both modules, because the flag never only meant one of them: the capture
    decision is in ``feedback`` and the HTTP round-trip it leads to is in
    ``community_api``, and reading one half of that chain was never the point.
    Keeping ``community_api`` off by default also keeps server response bodies
    — which is what those lines quote — out of a log nobody asked for.
    """
    from sorter.community import community_api, feedback

    level = logging.DEBUG if feedback.debug_enabled() else logging.INFO
    for module in (feedback, community_api):
        logging.getLogger(module.__name__).setLevel(level)
