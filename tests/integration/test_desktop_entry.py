"""The desktop entry, judged by freedesktop's own validator.

``tests/unit/ui/test_desktop_integration.py`` asserts the keys this app means
to write. It cannot tell you whether the *file* is legal — a stray unescaped
character in ``Exec``, a category that isn't in the registry, a key that moved
between spec versions — and a desktop that rejects the entry does so silently,
which reads to the user as "the menu item just never appeared".

So run the real thing, the same way test_cliff_config.py runs the real
git-cliff. ``desktop-file-validate`` ships in ``desktop-file-utils``, is
packaged by every distribution this app targets, and is the same tool distro
packaging pipelines gate on. Skipped where it isn't installed.

Warnings count, not just errors: the validator writes them to stdout and still
exits 0, and each one names a real desktop-side consequence (an app appearing
twice in the menu, for one). Anything it prints fails this test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from sorter.ui import desktop_integration as di

VALIDATE = shutil.which("desktop-file-validate")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(VALIDATE is None, reason="desktop-file-validate (desktop-file-utils) not installed"),
]


def _validate(entry: Path) -> str:
    assert VALIDATE is not None
    result = subprocess.run([VALIDATE, str(entry)], capture_output=True, text=True, timeout=60)
    return (result.stdout + result.stderr).strip()


@pytest.mark.parametrize(
    "folder",
    [
        "plain",
        # The case that motivates quoting Exec at all.
        "with spaces",
        # Legal in a path and reserved in an Exec value; both have to survive.
        "dollar$and`tick",
    ],
)
def test_the_entry_validates(tmp_path: Path, folder: str) -> None:
    app_root = tmp_path / folder
    (app_root).mkdir()
    script = app_root / "start.sh"
    script.write_text("#!/bin/sh\n")

    entry = tmp_path / f"{di.APP_ID}.desktop"
    entry.write_text(di.desktop_entry(script, app_root), encoding="utf-8")

    assert _validate(entry) == ""


def test_the_entry_this_install_would_write_validates(tmp_path: Path) -> None:
    """Not a constructed path: the real app root, exactly as ``ensure`` writes it."""
    from sorter import paths

    script = paths.app_root() / "start.sh"
    entry = tmp_path / f"{di.APP_ID}.desktop"
    entry.write_text(di.desktop_entry(script, paths.app_root()), encoding="utf-8")

    assert _validate(entry) == ""
