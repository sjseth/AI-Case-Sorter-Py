"""Guards on the two entry-point shims.

Both are small, both are load-bearing, and neither had any coverage before:
they are the first code to run on every launch, and the sole path an
already-installed version takes when it updates itself onto a new layout.

``src/sorter/__main__.py`` is what ``bootstrap.py`` launches. The root
``main.py`` exists for updates only -- see its module docstring -- and is what
a pre-#58 install runs on the launch that installs the src layout.
"""

from __future__ import annotations

import ast
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE_MAIN = ROOT / "src" / "sorter" / "__main__.py"
ROOT_MAIN = ROOT / "main.py"


def _run_shim(script: Path) -> list[str]:
    """Execute a shim's module level the way a bare script run does, and
    return the resulting ``sys.path``.

    ``runpy.run_path`` reproduces the two conditions the shim keys on -- it
    leaves ``__package__`` empty (verified below) and leaves ``__name__``
    something other than ``"__main__"``, so ``main()`` does not run and this
    stays a test of the shim rather than of the app.
    """
    original = list(sys.path)
    try:
        sys.path.insert(0, str(script.parent))  # what CPython does for a script
        namespace = runpy.run_path(str(script))
        assert not namespace.get("__package__"), "shim guard keys on __package__ being empty"
        return list(sys.path)
    finally:
        sys.path[:] = original


def test_package_main_puts_src_on_the_path() -> None:
    path = _run_shim(PACKAGE_MAIN)
    assert path[0] == str(ROOT / "src")


def test_package_main_removes_its_own_directory_from_the_path() -> None:
    """The subtle half, and the reason a plain ``sys.path.insert`` is wrong.

    Python puts ``src/sorter`` on ``sys.path`` automatically for a script run.
    Left there, this package's own submodules are importable as top-level
    names -- so any dependency doing ``import ui`` or ``import update`` gets
    ``sorter.ui`` instead. Reads like a redundant tidy-up, so it is exactly
    the line a later simplification would drop.
    """
    path = _run_shim(PACKAGE_MAIN)
    assert str(PACKAGE_MAIN.parent) not in path

    shadowable = {p.stem for p in PACKAGE_MAIN.parent.iterdir() if p.suffix == ".py" or p.is_dir()}
    assert {"ui", "update", "data"} <= shadowable, "premise check: these would shadow if the dir stayed"


def test_importing_the_package_does_not_touch_sys_path() -> None:
    """The guard must not fire for an installed package or a plain import."""
    before = list(sys.path)
    import sorter.__main__  # noqa: F401

    assert sys.path == before


@pytest.mark.parametrize("script", [PACKAGE_MAIN, ROOT_MAIN], ids=["package_main", "root_main"])
def test_apply_update_hook_runs_without_third_party_packages(script: Path, tmp_path: Path) -> None:
    """``--apply-update`` is reached before ``uv sync``, against a venv that
    may hold nothing. ``-S`` is the same view of the world it really has.

    This is also the end-to-end proof that each shim resolves ``sorter`` at
    all: an unresolvable one exits non-zero with an ImportError.
    """
    env = dict(os.environ, CASESORTER_DATA_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-S", str(script), "--apply-update"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr


def test_root_main_is_stdlib_only_at_module_level() -> None:
    """It runs on the update-applying launch, in the *old* process."""
    tree = ast.parse(ROOT_MAIN.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    offenders = imported - (set(sys.stdlib_module_names) | {"__future__"})
    assert not offenders, f"main.py imports non-stdlib module(s) at module level: {offenders}"


def test_root_main_delegates_rather_than_reimplementing() -> None:
    """A second copy of the startup sequence would drift from the real one.

    The shim's whole value is being a redirect: whatever ``__main__.main``
    grows, the compatibility path gets it for free.
    """
    source = ROOT_MAIN.read_text(encoding="utf-8")
    assert "from sorter.__main__ import main" in source
