"""Shared offscreen fixtures for the UI tests.

Every window here is built against a REAL SQLite-backed ``Config`` on a temp
dir — the Sort dashboard reads slot assignments straight out of it, so a stub
config would only test the stub.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Must be set before anything constructs a QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# A non-root conftest's collection hook still sees the whole item list, so the
# filter below is what scopes it. Derived from __file__, never spelled out: a
# stale path literal here silently puts the whole suite back under coverage.
_THIS_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: Any, items: list) -> None:
    """Run every test in this directory outside coverage measurement.

    The full suite under pytest-cov's tracer segfaults, and which test dies
    is decided by allocation noise, not by any line of ours: bisected to the
    point where adding or removing a single unrelated QAction flips the
    crash, on both CI runners and locally, on every coverage core and with
    branch coverage on or off. Uninstrumented, the suite has never crashed.
    pytest-cov's ``no_cover`` marker is the supported per-test off switch.
    """
    for item in items:
        if Path(item.path).resolve().is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.no_cover)


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _collect_between_tests():
    """Flush deferred deletions between tests — and nothing else.

    Almost no test pumps Qt's event loop (``drain_until`` drains only the
    bus), so deleteLater work from every test otherwise piles up until the
    suite's rare ``processEvents()`` call flushes hundreds of tests' worth at
    once — which is where the Windows runner took a deterministic access
    violation. Flushing per test keeps that backlog at one test's worth.

    Deliberately NOT a generic ``processEvents()``: delivering arbitrary
    queued events into half-torn windows is what segfaulted Linux when that
    was tried. And deliberately no forced ``gc.collect()``: shiboken wrappers
    finalizing against C++ objects mid-teardown reproducibly corrupted the
    heap on the Windows CI runner (0xc0000374). Qt test suites (pytest-qt
    included) run without forced collection; so do these.
    """
    yield
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def config(tmp_path: Path):
    from sorter.data.config import Config
    from sorter.data.db import Database

    db = Database(tmp_path / "casesorter.db")
    db.ensure_initialized()
    yield Config(db).load()
    # Explicit close, or every window teardown surfaces a ResourceWarning
    # once the finalizer actually runs (window_factory deleteLater's).
    db.close()


@pytest.fixture
def window_factory(qapp) -> Any:
    """Build a window on a given config; every one is closed at teardown."""
    from sorter.ui.app import QtMainWindow

    windows = []

    def _make(cfg: Any) -> Any:
        # auto_connect=False: constructing the shell must not open a camera or a port.
        window = QtMainWindow(cfg, auto_connect=False)
        windows.append(window)
        return window

    yield _make
    for window in windows:
        window.close()
        window.deleteLater()


@pytest.fixture
def window(window_factory, config):
    return window_factory(config)


def seed_model(config: Any, assignments: dict[str, int], *, name: str = "Test model") -> int:
    """Create a model, activate it, and assign headstamps to slots."""
    from sorter.data.models import Model
    from sorter.data.repository import CartridgeRepo, HeadstampRepo, ModelRepo, SettingsRepo

    cartridge = CartridgeRepo(config.db).list()[0]
    model = ModelRepo(config.db).create(Model(name=name, cartridge_id=cartridge.id))
    headstamps = HeadstampRepo(config.db)
    for headstamp, slot in assignments.items():
        headstamps.add(model.id, headstamp, slot)
    SettingsRepo(config.db).set_active_model_id(model.id)
    return model.id


def drain_until(window: Any, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Pump the bus on this thread until ``predicate`` holds (or time runs out).

    The worker/broker threads post; only the main thread may dispatch, so this
    is the test-side stand-in for the drain QTimer.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        window.bus.drain()
        if predicate():
            return True
        time.sleep(0.01)
    window.bus.drain()
    return predicate()
