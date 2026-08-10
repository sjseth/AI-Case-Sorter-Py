"""Make `sorter` importable when running pytest from the repo root."""

from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# Developer overrides (see sorter/community/appenv.py) must not leak into the suite: a
# contributor pointing their app at a local backend should still get the same
# test results as CI. Tests that want an override set it themselves.
_DEV_ENV_VARS = (
    "CASESORTER_ENV_FILE",
    "CASESORTER_API_BASE",
    "CASESORTER_API_CA_BUNDLE",
    "CASESORTER_API_INSECURE",
)


@pytest.fixture(autouse=True)
def _clear_dev_env(monkeypatch):
    for name in _DEV_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _freeze_import_garbage():
    """Move everything the imports built into gc's permanent generation.

    The per-test collection below then only has to walk objects the tests
    themselves created, which keeps it off the suite's runtime.
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture(autouse=True)
def _collect_between_tests():
    """Finalize each test's garbage on the main thread, before the next one.

    Tk widgets and StringVars reference each other, so they only die in the
    cyclic collector — and left alone that runs on whichever thread happens to
    allocate next. Several tests start worker threads, and a `Variable.__del__`
    that lands on one calls into Tcl from the wrong thread: it hangs there
    waiting on the interpreter lock, or aborts the process outright. Which
    tests collide is a matter of allocation timing, so adding an unrelated UI
    test can surface it anywhere — collecting here keeps Tk garbage from
    outliving the test that made it.
    """
    yield
    gc.collect()
