"""The layout change, applied by the release that predates it.

An in-app update is applied by the copy **already installed**, so the code
that installs the ``src/`` layout is a release that has never heard of it.
Every unit test in the suite exercises this tree's ``apply_update`` against
this tree's layout, which is the one combination that cannot happen on a real
machine: it is always version N applying version N+1.

So check out the previous release from git and run *its* updater against a
real built sdist of this tree. ``uv build``/``git-cliff`` get the same
treatment in this directory -- assert the real artifact, not a description
of it.

The sdist specifically, not a copy of the working tree: the root ``main.py``
that makes this work exists *only* in the archive, mapped there from
``src/sorter/_legacy_entry.py`` by ``pyproject.toml``'s ``force-include``.
Staging a tree copy would test a layout no user ever receives.

What this pins, concretely:

* A tree updated by 1.1.0 still launches. 1.1.0 ends that launch with
  ``python main.py``, a path baked into a ``bootstrap.py`` already in memory
  by the time the new tree lands, so the archive has to carry one. Drop the
  ``force-include`` and the first post-update launch is an ``ImportError``.
* ``src/sorter`` wins over the stale root ``sorter/`` that 1.1.0's updater
  leaves behind (it prunes the flat package, then re-creates
  ``sorter/_version.py`` inside it).
* This tree's updater cleans that residue up, and downgrades back to the flat
  layout without stranding the entry point.

Skipped when git, uv, or the tag isn't available; CI has all three.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# The newest release that predates the src/ layout -- the version a real user
# is updating *from*. Its updater accepts the new layout (REQUIRED_ENTRY_SETS,
# shipped by #62) but knows nothing about applying or launching it.
PREVIOUS_RELEASE = "1.1.0"

# Never copied into a simulated install: user data, virtualenvs, and caches
# that would make a "did the update place this file" assertion meaningless.
_SKIP = shutil.ignore_patterns(".git", ".venv", ".uv", ".uv-cache", "data", "__pycache__", "*.pyc", "dist", "build")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _has_previous_release() -> bool:
    if shutil.which("git") is None:
        return False
    return _git("rev-parse", "--verify", f"{PREVIOUS_RELEASE}^{{commit}}").returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed"),
    pytest.mark.skipif(
        not _has_previous_release(),
        reason=f"git or the {PREVIOUS_RELEASE} tag is unavailable (shallow clone?)",
    ),
]


@pytest.fixture(scope="module")
def release_tree(tmp_path_factory) -> Path:
    """This tree as a user receives it: a built sdist, unpacked.

    Built once for the module -- it is the slow step here, and every test
    wants the same bytes.
    """
    out = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"uv build --sdist failed:\n{proc.stdout}\n{proc.stderr}")

    tarballs = list(out.glob("*.tar.gz"))
    assert len(tarballs) == 1, f"expected exactly one sdist, got {tarballs}"

    unpacked = out / "unpacked"
    shutil.unpack_archive(str(tarballs[0]), str(unpacked))
    wrappers = list(unpacked.iterdir())
    assert len(wrappers) == 1, f"expected a single wrapper directory, got {wrappers}"
    return wrappers[0]


def _install_previous_release(dest: Path) -> Path:
    """Lay down the previous release the way an installed copy looks on disk."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / f"{PREVIOUS_RELEASE}.tar"
    proc = _git("archive", "--format=tar", "-o", str(archive), PREVIOUS_RELEASE)
    assert proc.returncode == 0, proc.stderr
    shutil.unpack_archive(str(archive), str(dest), format="tar")
    archive.unlink()
    return dest


def _stage(tree: Path, data_dir: Path, version: str, from_version: str) -> None:
    """Put `tree` in <data>/updates/pending/ with the metadata apply_update reads."""
    updates = data_dir / "updates"
    pending = updates / "pending"
    updates.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tree, pending, ignore=_SKIP)
    (updates / "pending.json").write_text(
        json.dumps({"version": version, "tag": version, "from_version": from_version}),
        encoding="utf-8",
    )


def _run(args: list[str], cwd: Path, data_dir: Path) -> subprocess.CompletedProcess:
    """Run a simulated install's own Python, seeing only what it ships.

    ``-S``, and it is load-bearing rather than tidiness: a dev checkout is
    ``uv sync``-ed with the project installed editable, so site-packages holds
    a ``.pth`` pointing at *this repo's* ``src/``. Without ``-S`` every
    subprocess here resolves ``sorter`` to the working tree no matter what the
    temporary install contains, and the assertions pass on the wrong package.
    Everything these subprocesses touch is stdlib-only by design (see
    ``apply_update``'s module docstring), which is what makes ``-S`` viable.
    """
    env = dict(os.environ)
    env["CASESORTER_DATA_DIR"] = str(data_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-S", *args], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    out = tmp_path / "data"
    out.mkdir()
    return out


@pytest.fixture
def upgraded(tmp_path: Path, data_dir: Path, release_tree: Path) -> Path:
    """An install of the previous release, updated to this tree by *its own* updater.

    ``main.py --apply-update`` is the previous release's own pre-launch entry
    point, running its own ``sorter/apply_update.py`` against its own
    ``PRUNE_ROOTS``. Nothing from this tree takes part in applying it.
    """
    app = _install_previous_release(tmp_path / "app")
    _stage(release_tree, data_dir, version="9.9.9", from_version=PREVIOUS_RELEASE)

    result = _run(["main.py", "--apply-update"], cwd=app, data_dir=data_dir)
    assert result.returncode == 0, result.stderr
    return app


def test_the_previous_release_can_apply_this_layout(upgraded: Path) -> None:
    assert (upgraded / "src" / "sorter" / "__init__.py").is_file()
    assert (upgraded / "src" / "sorter" / "__main__.py").is_file()


def test_the_updated_tree_still_has_the_entry_point_the_old_launcher_runs(upgraded: Path, release_tree: Path) -> None:
    """1.1.0's in-memory ``run_app`` execs ``python main.py`` on this very launch.

    It resolved that path before the update replaced the tree underneath it,
    and its pruner only touches ``sorter/``, so the shim has to be shipped and
    has to survive. Without it the user's first post-update launch is a
    traceback.
    """
    shipped = upgraded / "main.py"
    assert shipped.is_file(), (
        "the archive carries no root main.py -- the launch that applies this update "
        "runs `python main.py` from a bootstrap.py that predates the move"
    )
    # Content, not presence: the previous release ships its own main.py, and
    # the old pruner never touches the repo root, so a *stale* one is left
    # sitting there either way. Presence alone would assert nothing.
    assert shipped.read_text(encoding="utf-8") == (release_tree / "main.py").read_text(encoding="utf-8"), (
        "main.py in the updated tree is the previous release's, not the one this archive shipped"
    )


def test_the_first_post_update_launch_imports_the_new_package(upgraded: Path) -> None:
    """The failure this whole file exists for, reproduced as an assertion.

    Importing ``main`` runs the shim without tripping its ``__main__`` guard,
    so this is the real import path of that launch and not a re-creation of
    it. The reported symptom was ``ImportError: cannot import name 'appenv'
    from 'sorter' (unknown location)`` -- ``unknown location`` being the stale
    root ``sorter/`` resolving as a namespace package.
    """
    result = _run(
        ["-c", "import main, sorter; print(sorter.__file__)"],
        cwd=upgraded,
        data_dir=upgraded / "unused-data",
    )
    assert result.returncode == 0, result.stderr
    resolved = Path(result.stdout.strip())
    assert resolved == upgraded / "src" / "sorter" / "__init__.py", (
        f"`import sorter` resolved to {resolved}, not the new package under src/"
    )


def test_this_tree_cleans_up_the_residue_the_old_updater_leaves(
    upgraded: Path, data_dir: Path, release_tree: Path
) -> None:
    """PRUNE_ROOTS covers ``sorter/`` as well, or the leftovers are permanent.

    1.1.0 empties the flat package and then re-creates ``sorter/_version.py``
    inside it (its ``_stamp_version`` looks for a *flat* ``_version.py`` in the
    archive, and the sdist ships the ``src/`` one). Nothing prunes that
    afterwards unless this tree's updater is told to.
    """
    assert (upgraded / "sorter").is_dir(), "expected the residue this test is about"

    _stage(release_tree, data_dir, version="9.9.10", from_version="9.9.9")
    result = _run(["main.py", "--apply-update"], cwd=upgraded, data_dir=data_dir)
    assert result.returncode == 0, result.stderr

    assert not (upgraded / "sorter").exists(), "the stale flat package survived a second update"
    assert (upgraded / "src" / "sorter" / "__init__.py").is_file()


def test_downgrading_to_the_previous_release_restores_its_entry_point(
    tmp_path: Path, data_dir: Path, release_tree: Path
) -> None:
    """The version picker makes this a supported move, so it has to land intact.

    Applied by *this* tree's updater, which prunes all of ``src/sorter`` --
    including the ``__main__.py`` its own in-memory ``run_app`` is about to
    launch. That is what ``bootstrap.relaunch_after_update`` exists for; here
    the concern is only that the flat entry point is on disk to relaunch into.
    """
    app = tmp_path / "app"
    shutil.copytree(release_tree, app, ignore=_SKIP)
    old = _install_previous_release(tmp_path / "previous")
    _stage(old, data_dir, version=PREVIOUS_RELEASE, from_version="9.9.9")

    result = _run(["main.py", "--apply-update"], cwd=app, data_dir=data_dir)
    assert result.returncode == 0, result.stderr

    assert (app / "main.py").is_file()
    assert (app / "sorter" / "__init__.py").is_file()
    assert not (app / "src" / "sorter" / "__main__.py").exists(), "the src layout was not pruned on downgrade"

    # And the restored flat entry point actually runs, rather than tripping
    # over anything the src layout left behind.
    launched = _run(["main.py", "--apply-update"], cwd=app, data_dir=tmp_path / "empty-data")
    assert launched.returncode == 0, launched.stderr
