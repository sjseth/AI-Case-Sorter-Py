"""Tests for sorter.__version__'s three-way fallback chain.

The logic runs at import time (a module-level try/except in
sorter/__init__.py, the standard hatch-vcs idiom), so each branch is
exercised by controlling what's importable and reloading the module --
not by calling a function. Whatever sorter/_version.py happens to exist on
disk when the suite runs (a real build leaves one; it's gitignored, so its
presence is not guaranteed) is deliberately not relied on either way --
every test controls its own inputs via sys.modules and monkeypatch.
"""

from __future__ import annotations

import importlib
import re
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import ModuleType

import pytest

import sorter

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def _restore_sorter_after():
    """Reloading sorter mutates real global state (sys.modules); always put
    it back to a normal, real import afterward so later tests in the same
    process see the genuine module, not a test's stand-in."""
    yield
    sys.modules.pop("sorter._version", None)
    importlib.reload(sorter)


def test_prefers_the_generated_version_file(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_version_module = ModuleType("sorter._version")
    # `ModuleType` has no statically-declared `__version__` — modules gain it
    # dynamically (this stands in for the real hatch-vcs-generated one, which
    # does the same) — so there's no static type to narrow to here.
    fake_version_module.__version__ = "1.2.3"  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, "sorter._version", fake_version_module)

    importlib.reload(sorter)

    assert sorter.__version__ == "1.2.3"


def test_falls_back_to_installed_package_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sorter._version", None)  # import sorter._version -> ImportError
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "4.5.6")

    importlib.reload(sorter)

    assert sorter.__version__ == "4.5.6"


def test_falls_back_to_a_literal_placeholder_when_nothing_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """The genuine "never built, never installed" case -- a plain git clone
    with `uv run python main.py` and no build ever having happened."""
    monkeypatch.setitem(sys.modules, "sorter._version", None)

    def _raise(_name: str) -> str:
        raise PackageNotFoundError("ai-case-sorter")

    monkeypatch.setattr("importlib.metadata.version", _raise)

    importlib.reload(sorter)

    assert sorter.__version__ == "0.0.0+unknown"


def test_window_title_is_not_a_hardcoded_version() -> None:
    """The window title said "v2.0.1" for who knows how long -- a literal
    unconnected to sorter.__version__ (which was 0.1.0 at the time), to any
    git tag (there were none), or to pyproject.toml. Users saw one number
    while the in-app updater compared a completely different one. Deriving
    the version from git tags is pointless if a string literal in the UI can
    still drift away from it, so this guards the one place that did."""
    source = (ROOT / "src" / "sorter" / "ui" / "app.py").read_text(encoding="utf-8")
    title_calls = re.findall(r"self\.root\.title\((.*?)\)", source)

    assert title_calls, "expected a self.root.title(...) call in app.py"
    for call in title_calls:
        assert "__version__" in call, (
            f"window title {call!r} does not derive from __version__ -- "
            "hardcoding a version here is exactly the drift hatch-vcs exists to remove"
        )
        assert not re.search(r"\bv?\d+\.\d+\.\d+\b", call), f"window title {call!r} contains a hardcoded version number"
