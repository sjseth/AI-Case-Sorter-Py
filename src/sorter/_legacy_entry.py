#!/usr/bin/env python3
"""Compatibility entry point, shipped **only** in the release archive.

Never executed from the source tree, and never imported. `pyproject.toml`'s
`[tool.hatch.build.targets.sdist.force-include]` copies this file into the
sdist as a root `main.py`, which is the one place it makes sense: an in-app
update is applied by the copy already installed, and every release up to
1.1.0 ends that launch with `uv run --no-sync python main.py`, from a
`bootstrap.py` already in memory when the new tree lands. With no `main.py`
in the archive that launch dies with an `ImportError`; the next one recovers,
but the user has already been shown a crash. Their pruner only touches
`sorter/`, so this survives being applied by it.

Deleting it means deciding that no install older than the `src/` layout can
still be out there — one line in `pyproject.toml`, no source layout to
disturb, which is the whole reason it lives here rather than at the root.

Stdlib-only: it runs before `uv sync` on that launch.
"""

from __future__ import annotations

import os
import sys

# `src` relative to *the root of an installed app folder* -- where the sdist
# places this file, and the only place it ever runs.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    # Ahead of everything: upgrading from the flat layout leaves a stale root
    # `sorter/` behind, and `src/sorter` has to win.
    sys.path.insert(0, _SRC)

from sorter import paths  # noqa: E402
from sorter.__main__ import main  # noqa: E402

paths.set_installed_package(False)


if __name__ == "__main__":
    raise SystemExit(main())
