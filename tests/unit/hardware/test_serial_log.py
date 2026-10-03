"""The continuous serial traffic log (issue #112, item A18)."""

from __future__ import annotations

import itertools
import logging
import re
from pathlib import Path

import pytest

from sorter import paths
from sorter.control.events import EventBus
from sorter.hardware import serial_log
from sorter.hardware.serial_log import SerialTrafficLog

LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ")


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))


def _traffic(path: Path) -> list[str]:
    """The log's traffic lines, without the "# …" session markers."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]


def test_bus_traffic_lands_timestamped_and_direction_marked() -> None:
    bus = EventBus()
    log = SerialTrafficLog(enabled=True)
    log.attach(bus)

    bus.post("serial/tx", "version")
    bus.post("serial/rx", "CS7.2 Firmware V1.7")
    bus.post("serial/note", "Emulated did not handshake")
    bus.drain()
    log.close()

    assert log.path is not None
    assert log.path.parent == paths.logs_dir()
    assert log.path.name.startswith("serial-")
    lines = _traffic(log.path)
    assert [LINE_RE.sub("", line) for line in lines] == [
        "-> version",
        "<- CS7.2 Firmware V1.7",
        "-- Emulated did not handshake",
    ]
    assert all(LINE_RE.match(line) for line in lines)


def test_nothing_is_written_while_off() -> None:
    bus = EventBus()
    log = SerialTrafficLog()
    log.attach(bus)

    bus.post("serial/tx", "version")
    bus.drain()

    assert not log.enabled
    assert serial_log.serial_logs() == []
    assert not paths.logs_dir().exists()


def test_toggling_appends_to_the_same_session_file() -> None:
    log = SerialTrafficLog(enabled=True)
    log.write("tx", "first")
    log.set_enabled(False)
    log.write("tx", "while off")
    log.set_enabled(True)
    log.write("tx", "second")
    log.close()

    assert log.path is not None
    assert serial_log.serial_logs() == [log.path]
    assert [LINE_RE.sub("", line) for line in _traffic(log.path)] == ["-> first", "-> second"]


def test_switching_off_flushes_what_was_buffered() -> None:
    clock = iter([1000.0, 1000.1, 1000.2]).__next__
    log = SerialTrafficLog(enabled=True, clock=clock)  # the header flushes at 1000.0
    log.write("rx", "buffered")  # 0.1 s later: inside the flush interval
    assert log.path is not None
    assert "buffered" not in log.path.read_text(encoding="utf-8")

    log.set_enabled(False)
    assert "buffered" in log.path.read_text(encoding="utf-8")


def test_an_unwritable_log_directory_disables_logging_with_one_warning(caplog) -> None:
    # A plain file where the logs directory should be: mkdir fails on every OS.
    paths.app_data_dir().mkdir(parents=True)
    paths.logs_dir().write_text("not a directory", encoding="utf-8")
    bus = EventBus()

    with caplog.at_level(logging.WARNING, logger="sorter.hardware.serial_log"):
        log = SerialTrafficLog(enabled=True)
        log.attach(bus)
        bus.post("serial/rx", "ok")
        bus.drain()  # must not raise into the handler

    assert not log.enabled
    assert len([r for r in caplog.records if r.name == "sorter.hardware.serial_log"]) == 1


def test_a_write_error_disables_logging_instead_of_raising(caplog) -> None:
    log = SerialTrafficLog(enabled=True)
    assert log._handle is not None
    log._handle.close()  # every later write raises ValueError

    with caplog.at_level(logging.WARNING, logger="sorter.hardware.serial_log"):
        log.write("rx", "ok")
        log.write("rx", "ok again")

    assert not log.enabled
    assert len(caplog.records) == 1


def test_a_full_file_rolls_over_to_a_new_one(monkeypatch) -> None:
    monkeypatch.setattr(serial_log, "MAX_SERIAL_LOG_BYTES", 200)
    seconds = itertools.count()
    monkeypatch.setattr(
        serial_log, "serial_log_path", lambda _stamp: paths.logs_dir() / f"serial-20260101-{next(seconds):06d}.log"
    )
    log = SerialTrafficLog(enabled=True)
    for index in range(10):
        log.write("rx", f"line {index} " + "x" * 40)
    log.close()

    files = serial_log.serial_logs()
    assert len(files) > 1
    assert files[0] == log.path  # still writing to the newest
    longest_line = max(len(line) + 1 for p in files for line in p.read_text(encoding="utf-8").splitlines())
    # The cap overshoots by one line; the slack also covers Windows' CRLF, which the count ignores.
    assert all(p.stat().st_size < 200 + 2 * longest_line for p in files)
    assert "line 9" in log.path.read_text(encoding="utf-8")


def test_old_session_files_are_pruned() -> None:
    logs = paths.logs_dir()
    logs.mkdir(parents=True)
    for index in range(serial_log.MAX_SERIAL_LOGS + 5):
        (logs / f"serial-2020010{index // 10}-0000{index % 10}.log").write_text("old", encoding="utf-8")
    (logs / "training-20200101-000000.log").write_text("not ours", encoding="utf-8")

    SerialTrafficLog(enabled=True).close()

    # Pruned before the new file opens, the same as the training logs.
    assert len(serial_log.serial_logs()) == serial_log.MAX_SERIAL_LOGS + 1
    assert (logs / "training-20200101-000000.log").exists()
