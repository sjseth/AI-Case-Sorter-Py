"""Make `sorter` importable when running pytest from the repo root."""

from __future__ import annotations

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


# Every test gets its own throwaway data root. Without this, any test that
# writes through `sorter.paths` without setting CASESORTER_DATA_DIR itself
# lands in the contributor's REAL data folder — the one their installed app
# uses. test_model_ownership.py's imports did exactly that: a fresh test DB
# hands its imported model id 2, so a 10-byte placeholder checkpoint
# overwrote a real models/2/trainedmodel/2.pth, which then surfaced in the
# app as "could not be loaded by PyTorch". A separate directory from
# `tmp_path`, so tests that inspect their own tmp_path don't see it; a test
# that needs a particular root (test_paths.py) still sets or clears the
# variable itself, after this runs.
@pytest.fixture(autouse=True)
def _isolate_data_dir(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path_factory.mktemp("casesorter-data")))


# A repo-wide `gc.collect()` between tests lived here for Tk: widgets and
# StringVars only die in the cyclic collector, and one finalized on a worker
# thread called into Tcl from the wrong thread. It went with the Tk UI —
# forcing the collector over half-torn-down Qt widget trees is actively
# harmful (see tests/unit/ui/conftest.py), so the fixture is not something to
# reinstate.
