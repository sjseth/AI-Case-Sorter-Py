"""Guards on how the app is launched, and on it needing no ``sys.path`` shim.

``bootstrap.py`` runs ``python -m sorter`` with ``PYTHONPATH=src``, so no file
in the tree rewrites ``sys.path`` to launch itself. That is worth pinning from
the application side as well as the launcher's: the obvious "fix" the first
time someone runs ``python src/sorter/__main__.py`` by hand and gets
``ModuleNotFoundError`` is to add a shim back into ``__main__.py``, and a shim
that inserts the wrong directory puts ``src/sorter`` on the path, where ``ui``,
``data`` and ``update`` shadow same-named third-party packages.

The compatibility entry point (``_legacy_entry.py``, shipped as a root
``main.py`` only in the release archive) is covered by
``tests/integration/test_cross_version_update.py``, which runs it from a real
built sdist rather than from here.
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


def _launch(expr: str) -> str:
    """Start the app the way bootstrap.py does, and report something about it.

    Same argv and same PYTHONPATH; ``-c`` swapped for ``-m`` so it can be
    asked a question instead of opening a window. ``-S`` because a dev
    checkout is ``uv sync``-ed with the project installed editable, leaving a
    ``.pth`` in site-packages that points at this repo's ``src/`` -- without
    it these assertions pass on that ``.pth`` whatever PYTHONPATH says.
    """
    result = subprocess.run(
        [sys.executable, "-S", "-c", f"import sorter, json, sys; print({expr})"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_sorter_resolves_to_this_tree() -> None:
    assert Path(_launch("sorter.__file__")) == SRC / "sorter" / "__init__.py"


def test_the_package_directory_itself_stays_off_the_path() -> None:
    """``src/sorter`` on ``sys.path`` makes every subpackage a top-level name.

    A dependency doing ``import ui``, ``import data`` or ``import update``
    would then get a piece of this app instead. Running as ``-m`` from
    ``src/`` is what avoids it; a hand-rolled shim in ``__main__.py`` is what
    would reintroduce it.
    """
    path = json.loads(_launch("json.dumps(sys.path)"))
    assert str(SRC / "sorter") not in path
    for name in ("ui", "data", "update", "control", "training"):
        assert not any((Path(entry) / name / "__init__.py").is_file() for entry in path if entry), (
            f"`import {name}` would resolve to a sorter subpackage"
        )


def test_no_module_in_the_package_rewrites_sys_path() -> None:
    """The shim, kept out. One launcher owns the path, and it isn't the app.

    Scoped to the package because ``_legacy_entry.py`` is exempt by design --
    it runs as a bare script inside an *installed* app folder, where nothing
    has set PYTHONPATH for it.
    """
    offenders = {}
    for module in sorted(SRC.rglob("*.py")):
        if module.name == "_legacy_entry.py":
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        found = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node).startswith(("sys.path.", "sys .path."))
        ]
        if found:
            offenders[str(module.relative_to(ROOT))] = found
    assert not offenders, f"bootstrap.py sets PYTHONPATH; the app must not touch sys.path: {offenders}"


def test_the_stdlib_only_pre_launch_hook_runs(tmp_path: Path) -> None:
    """End to end on the real entry point, under bootstrap.py's conditions.

    ``--apply-update`` is the only path through it that stays stdlib-only and
    exits without opening a window -- which is also what makes it the path
    that has to survive ``-S``, since bootstrap reaches for it before the venv
    has anything in it.
    """
    result = subprocess.run(
        [sys.executable, "-S", "-m", "sorter", "--apply-update"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(SRC),
            "CASESORTER_DATA_DIR": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
