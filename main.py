#!/usr/bin/env python3
"""Entry point. Puts ``src/`` on ``sys.path``, then hands off to ``sorter``.

Stays at the repo root rather than moving into the package with everything
else, because an in-app update is applied by the copy already installed:
every release up to 1.1.0 ends that launch with ``python main.py``, from a
``bootstrap.py`` already in memory when the new tree lands. Ship no
``main.py`` and the first launch after upgrading is a traceback.

Stdlib-only — it runs before ``uv sync`` on that launch.
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    # Ahead of everything: upgrading from the flat layout leaves a stale root
    # `sorter/`, and `src/sorter` has to win.
    sys.path.insert(0, _SRC)

from sorter import paths  # noqa: E402
from sorter.__main__ import main  # noqa: E402

paths.set_installed_package(False)


if __name__ == "__main__":
    raise SystemExit(main())
