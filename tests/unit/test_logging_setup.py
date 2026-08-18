"""Tests for the one-shot logging configuration.

What matters here isn't the formatting — it's that configuring can never cost
a launch, that the file lands under the data root (out of the updater's reach),
and that the ``CASESORTER_FEEDBACK_DEBUG`` knob still does what it did when it
gated a print.
"""

from __future__ import annotations

import logging

import pytest

from sorter import logging_setup, paths


@pytest.fixture(autouse=True)
def _fresh_config(monkeypatch, tmp_path):
    """Each test configures from scratch against its own data root."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_configured", False)
    yield
    root = logging.getLogger("sorter")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.NOTSET)
    root.propagate = True


def test_the_log_file_lands_under_the_data_root(tmp_path) -> None:
    logging_setup.configure()
    logging.getLogger("sorter.test").warning("hello %s", "world")

    path = logging_setup.log_path()
    assert path.parent == paths.logs_dir()
    assert tmp_path in path.parents, "under the data root, so the updater can't delete it"
    assert "hello world" in path.read_text(encoding="utf-8")


def test_the_launcher_log_is_left_alone() -> None:
    """`launch.log` is bootstrap.py's; this file sits beside it, never over it."""
    logging_setup.configure()
    assert logging_setup.log_path().name != "launch.log"


def test_an_unwritable_data_dir_costs_the_file_not_the_launch(monkeypatch) -> None:
    def explode() -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(paths, "logs_dir", explode)
    logging_setup.configure()  # must not raise

    handlers = logging.getLogger("sorter").handlers
    assert len(handlers) == 1, "the console handler survives on its own"
    assert isinstance(handlers[0], logging.StreamHandler)


def test_configure_is_idempotent_and_does_not_stack_handlers() -> None:
    logging_setup.configure()
    before = len(logging.getLogger("sorter").handlers)
    logging_setup.configure()
    logging_setup.configure(force=True)
    assert len(logging.getLogger("sorter").handlers) == before


def test_the_console_level_is_overridable(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_LOG_LEVEL", "DEBUG")
    logging_setup.configure()
    console = next(h for h in logging.getLogger("sorter").handlers if isinstance(h, logging.StreamHandler))
    assert console.level == logging.DEBUG


def test_an_unparseable_console_level_falls_back_to_info(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_LOG_LEVEL", "LOUD")
    logging_setup.configure()
    console = next(h for h in logging.getLogger("sorter").handlers if isinstance(h, logging.StreamHandler))
    assert console.level == logging.INFO


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("1", logging.DEBUG), ("0", logging.INFO)],
)
def test_the_feedback_flag_survives_as_a_level(monkeypatch, flag: str, expected: int) -> None:
    """The knob predates logging; it has to keep meaning the same thing.

    Both loggers, because the flag never only meant one of them — the capture
    decision and the HTTP round-trip it leads to are two modules.
    """
    monkeypatch.setenv("CASESORTER_FEEDBACK_DEBUG", flag)
    logging_setup.configure()
    assert logging.getLogger("sorter.community.feedback").level == expected
    assert logging.getLogger("sorter.community.community_api").level == expected


def test_a_library_logger_is_not_adopted() -> None:
    """Scoped to `sorter`, so a dependency's output never lands in our file."""
    logging_setup.configure()
    logging.getLogger("requests.packages").warning("not ours")
    assert "not ours" not in logging_setup.log_path().read_text(encoding="utf-8")
