"""Compatibility entry point — delegates to ``src/sorter/__main__.py``.

**This file exists for updates, not for developers.** The live entry point is
``src/sorter/__main__.py``, and ``bootstrap.py`` launches that directly. Run
the app with ``./start.sh`` / ``start.bat``; this shim is not the documented
way in.

Why it has to stay, permanently:

A self-update is applied by the version being *replaced*, not the one being
installed (see ``sorter/update/apply_update.py``). ``bootstrap.py`` imports
``apply_update``, applies the staged tree, and then launches the app — all in
one process, holding the *old* code in memory the whole time. So the launch
that installs a new layout is the one launch that cannot know about it.

Every release up to and including 1.0.x runs ``python main.py`` at that
point. If the archive those installs receive doesn't carry a ``main.py``,
that launch dies with ``ImportError: cannot import name 'appenv' from
'sorter'`` — the old pruner having just deleted the flat ``sorter/`` package
the old entry point was about to import. It recovers on the *next* launch,
by which time the new ``bootstrap.py`` is on disk, but the user has already
seen the update appear to break the app.

Deleting this file therefore re-breaks that upgrade for anyone who has not
yet passed through a release containing it. Since we cannot know when the
last 1.0.x install in the field has updated, it stays indefinitely — it costs
five lines and one indirection.

Stdlib-only at module level, like everything else on the pre-launch path: on
the update-applying launch, ``uv sync`` has run against the *new* lockfile
but this process is still the old one.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    src = os.path.join(os.path.abspath(os.path.dirname(__file__)), "src")
    # Insert at the front: an install upgraded from the flat layout can still
    # have a stale `<app>/sorter/` directory next to this file (the pre-#58
    # pruner rebuilt `sorter/_version.py` after emptying the package). Whatever
    # is first on sys.path wins, and a real package beats that leftover.
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)

    # Reaching this file at all means a source-tree run, exactly as
    # `__main__.py`'s own shim branch does — record it for the same reason.
    # Importing `sorter.__main__` as a submodule leaves `__package__` set, so
    # that branch does not fire and would otherwise leave this unrecorded.
    from sorter import paths

    paths.set_installed_package(False)

    from sorter.__main__ import main as _main

    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
