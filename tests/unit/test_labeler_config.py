"""Guards on .github/labeler.yml's subsystem coverage.

The point of #58's ``src/`` layout was that each subsystem boundary became a
real directory, so the labeler could stop enumerating individual filenames.
That only pays off while every subpackage actually has a glob -- and a missing
one is invisible: the action succeeds, it just applies nothing. Worse, the
workflow runs with ``sync-labels: true``, so a PR that matches no glob also has
any hand-applied subsystem label removed.

It cannot be checked on the PR that changes it either: the labeler runs on
``pull_request_target`` and so reads the *base* branch's config. This is the
only place the coverage can be verified before it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LABELER = ROOT / ".github" / "labeler.yml"
SETTINGS = ROOT / ".github" / "settings.yml"
PACKAGE = ROOT / "src" / "sorter"


def _globs() -> set[str]:
    return set(re.findall(r"'([^']+)'", LABELER.read_text(encoding="utf-8")))


def _declared_labels() -> set[str]:
    return set(re.findall(r"^  - name: \"?([^\"\n]+?)\"?$", SETTINGS.read_text(encoding="utf-8"), re.MULTILINE))


def _labeler_labels() -> set[str]:
    return set(re.findall(r"^([a-z][a-z0-9-]*):$", LABELER.read_text(encoding="utf-8"), re.MULTILINE))


@pytest.mark.parametrize(
    "subpackage",
    sorted(p.name for p in PACKAGE.iterdir() if p.is_dir() and (p / "__init__.py").is_file()),
)
def test_every_subpackage_has_a_labeler_glob(subpackage: str) -> None:
    assert f"src/sorter/{subpackage}/**" in _globs(), (
        f"src/sorter/{subpackage}/ matches no glob in .github/labeler.yml, so a PR "
        f"touching only that subsystem gets no label -- and sync-labels strips any "
        f"applied by hand. Add a glob, or fold it into an existing label's list."
    )


def test_every_label_used_by_the_labeler_is_declared() -> None:
    """actions/labeler errors on a label that does not exist in the repo."""
    missing = _labeler_labels() - _declared_labels()
    assert not missing, (
        f"labeler.yml uses undeclared label(s): {sorted(missing)}. Declare them in .github/settings.yml."
    )
