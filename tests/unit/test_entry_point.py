"""Guards on ``main.py`` -- the root shim that puts ``src/`` on ``sys.path``.

Nothing else in the suite runs the app's entry point, so without this a typo
in the one line that makes ``import sorter`` resolve ships a release where
every launch dies instantly. It is also the file with the least obvious reason
to exist (see its docstring: the in-app update path, not this tree's own
launch), which makes it a standing candidate for deletion.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"


def _probe(expr: str) -> str:
    """Import main.py the way a launch does, then report something about it.

    ``import main`` runs the shim without tripping its ``__main__`` guard, so
    this is the real path setup rather than a re-creation of it.

    ``-S`` because a dev checkout is ``uv sync``-ed with the project installed
    editable, leaving a ``.pth`` in site-packages that points at this repo's
    ``src/``. Without it every assertion here passes on that ``.pth`` no
    matter what ``main.py`` does.
    """
    result = subprocess.run(
        [sys.executable, "-S", "-c", f"import main, json, sys; print({expr})"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_src_goes_on_the_path_first() -> None:
    assert json.loads(_probe("json.dumps(sys.path)"))[0] == str(SRC)


def test_sorter_resolves_to_this_tree() -> None:
    """Not just importable -- importable as the package in ``src/``.

    An install upgraded from the flat layout keeps a stale root ``sorter/``
    until the next update prunes it, and the root is on ``sys.path`` too
    (Python puts the script's directory there). ``src/`` has to come first.
    """
    resolved = Path(_probe("__import__('sorter').__file__"))
    assert resolved == SRC / "sorter" / "__init__.py"


def test_the_package_directory_itself_stays_off_the_path() -> None:
    """``src/sorter`` on ``sys.path`` makes every subpackage a top-level name.

    A dependency doing ``import ui``, ``import data`` or ``import update``
    would then get a piece of this app. The flat layout had the same hazard in
    reverse; keeping ``src/`` (never ``src/sorter``) on the path is what
    removes it.
    """
    path = json.loads(_probe("json.dumps(sys.path)"))
    assert str(SRC / "sorter") not in path
    for name in ("ui", "data", "update", "control", "training"):
        assert not any((Path(entry) / name / "__init__.py").is_file() for entry in path if entry), (
            f"`import {name}` would resolve to a sorter subpackage"
        )


def test_the_entry_point_module_does_no_path_setup_of_its_own() -> None:
    """One shim, in one file.

    ``src/sorter/__main__.py`` carried a second copy of this while the root
    ``main.py`` was gone. Both existing means two things to keep in step, and
    the ``sys.path`` one is precisely the kind that fails silently -- the app
    starts either way, and only shadowing tells you which copy won.
    """
    tree = ast.parse((SRC / "sorter" / "__main__.py").read_text(encoding="utf-8"))
    touches_path = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ("path",) and ast.unparse(node).startswith("sys.path")
    ]
    assert not touches_path, f"sorter/__main__.py should leave sys.path to main.py: {touches_path}"


def test_the_stdlib_only_pre_launch_hook_runs(tmp_path: Path) -> None:
    """End to end on the real file, under the conditions bootstrap.py uses.

    ``--apply-update`` is the only path through ``main.py`` that stays
    stdlib-only and exits without opening a window, which is also what makes
    it the path that has to survive ``-S`` -- bootstrap reaches for it before
    the venv has anything in it.
    """
    result = subprocess.run(
        [sys.executable, "-S", "main.py", "--apply-update"],
        cwd=ROOT,
        env={**os.environ, "CASESORTER_DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
