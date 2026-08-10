"""Characterization tests for the real SerialBroker line parser and handshake.

`test_serial_emulator.py` exercises the *fake* broker; nothing covered the real
`SerialBroker._process_buffer` / `try_open` until now.

Several assertions here deliberately pin behavior that is arguably wrong — the
dispatch chain is a sequence of unanchored `in` substring tests, so a line like
``"error: broken sensor"`` is routed to `on_ok` (because "broken" contains
"ok") and never reaches `on_error`. GitHub issue #34 proposes tightening that
matching. These tests exist so the fix shows up as an explicit, reviewable flip
of the assertions rather than as an invisible behavior change.

This was found by reading the code, not observed on hardware.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from sorter.hardware import serial_broker
from sorter.hardware.serial_broker import SerialBroker


class _Sink:
    """Records which callback lists fired, and with what payloads."""

    def __init__(self, broker: SerialBroker) -> None:
        self.done: list[str] = []
        self.ok: list[str] = []
        self.error: list[str] = []
        self.waiting: list[str] = []
        self.response: list[str] = []
        self.received: list[str] = []
        broker.on_done.append(self.done.append)
        broker.on_ok.append(self.ok.append)
        broker.on_error.append(self.error.append)
        broker.on_waiting.append(self.waiting.append)
        broker.on_response.append(self.response.append)
        broker.on_received.append(self.received.append)


@pytest.fixture
def broker() -> SerialBroker:
    """A broker that has never touched a real port."""
    return SerialBroker(port="/dev/null-not-opened")


@pytest.fixture
def sink(broker: SerialBroker) -> _Sink:
    return _Sink(broker)


def _feed(broker: SerialBroker, chunk: str) -> None:
    """Push `chunk` through the parser exactly as the buffer holds it.

    Note this bypasses `_reader_loop`, which stamps a newline onto every chunk
    it forwards (`line if line.endswith("\\n") else line + "\\n"`). So the
    partial-line buffering exercised below is `_process_buffer`'s own contract,
    not a state the production reader can actually produce — see
    `test_reader_loop_stamps_a_newline_onto_a_partial_read` for that half.
    """
    broker._buf += chunk
    broker._process_buffer()


# ----- clean routing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "attr"),
    [
        ("done", "done"),
        ("ok", "ok"),
        ("error", "error"),
        ("waiting", "waiting"),
    ],
)
def test_clean_token_routes_to_its_own_callback(broker: SerialBroker, sink: _Sink, line: str, attr: str) -> None:
    _feed(broker, line + "\n")
    assert getattr(sink, attr) == [line]
    others = {"done", "ok", "error", "waiting", "response"} - {attr}
    for other in others:
        assert getattr(sink, other) == [], f"{line!r} unexpectedly fired on_{other}"


def test_unrecognized_line_falls_through_to_on_response(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "feedspeed:60\n")
    assert sink.response == ["feedspeed:60"]
    assert sink.done == sink.ok == sink.error == sink.waiting == []


def test_every_line_fires_on_received(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "done\nfeedspeed:60\nwaiting\n")
    assert sink.received == ["done", "feedspeed:60", "waiting"]


def test_matching_is_case_insensitive(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "DONE\nOK\nERROR\nWaiting\n")
    assert sink.done == ["DONE"]
    assert sink.ok == ["OK"]
    assert sink.error == ["ERROR"]
    assert sink.waiting == ["Waiting"]


def test_most_recent_response_tracks_last_line(broker: SerialBroker) -> None:
    _feed(broker, "first\nsecond\n")
    assert broker.read_line() == "second"
    # read_line() consumes it.
    assert broker.read_line() == ""


# ----- buffering / line splitting --------------------------------------------


def test_partial_line_is_held_until_the_terminator_arrives(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "wai")
    assert sink.received == []
    _feed(broker, "ting\n")
    assert sink.waiting == ["waiting"]


def test_reader_loop_stamps_a_newline_onto_a_partial_read(broker: SerialBroker, sink: _Sink, monkeypatch) -> None:
    """Characterization: a timed-out mid-line read is dispatched, not buffered.

    `_reader_loop` appends `line + "\\n"` whenever pyserial's `readline()`
    returns without a terminator — which is exactly what a read timeout on a
    half-transmitted line produces. So `_process_buffer`'s buffering never
    engages in production: the fragment is treated as a complete line and
    routed on the spot. Combined with the unanchored substring matching above,
    a line split mid-word is how a fragment could be misrouted. See issue #34.
    """
    fake = _PartialReadSerial(["waiti"], broker._stop_event)
    monkeypatch.setattr(broker, "_sp", fake)

    broker._reader_loop()

    assert sink.response == ["waiti"], "the partial was dispatched as a complete line"
    assert sink.waiting == []
    assert broker._buf == "", "nothing was left buffered for the rest of the line"


def test_crlf_terminator_is_stripped(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "done\r\n")
    assert sink.done == ["done"]
    assert sink.received == ["done"]


def test_blank_and_whitespace_only_lines_are_skipped(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "\n\r\n   \n\t\n")
    assert sink.received == []
    assert sink.response == []


def test_multiple_lines_in_one_feed_all_dispatch(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "done\nwaiting\nfeedspeed:60\n")
    assert sink.done == ["done"]
    assert sink.waiting == ["waiting"]
    assert sink.response == ["feedspeed:60"]
    assert broker._buf == ""


def test_trailing_partial_line_stays_in_the_buffer(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "done\npartial")
    assert sink.done == ["done"]
    assert broker._buf == "partial"


def test_handler_exception_does_not_stop_the_remaining_handlers(broker: SerialBroker) -> None:
    seen: list[str] = []

    def boom(_payload: str) -> None:
        raise RuntimeError("handler blew up")

    broker.on_done.append(boom)
    broker.on_done.append(seen.append)
    _feed(broker, "done\n")
    assert seen == ["done"]


# ----- characterization: unanchored substring matching -----------------------


def test_error_line_containing_ok_is_routed_to_on_ok(broker: SerialBroker, sink: _Sink) -> None:
    # Characterization: matching is an unanchored substring test and the "ok"
    # branch `continue`s before the "error" branch is ever reached, so
    # "br-OK-en" wins and this fires on_ok. The error is silently reported as a
    # success. See issue #34 — flipping this assertion is the point of that fix.
    _feed(broker, "error: broken sensor\n")
    assert sink.ok == ["error: broken sensor"]
    assert sink.error == []


@pytest.mark.parametrize("line", ["token", "bookkeeping"])
def test_word_merely_containing_ok_fires_on_ok(broker: SerialBroker, sink: _Sink, line: str) -> None:
    # Characterization: any line containing the substring "ok" anywhere is
    # treated as an acknowledgement. See issue #34.
    _feed(broker, line + "\n")
    assert sink.ok == [line]
    assert sink.response == []


@pytest.mark.parametrize("line", ["abandoned", "sensor calibration done at 12:00", "undone"])
def test_line_merely_containing_done_fires_on_done(broker: SerialBroker, sink: _Sink, line: str) -> None:
    # Characterization: "done" is checked first and unanchored, so a diagnostic
    # line that merely mentions it satisfies a pending feed/sort wait. See
    # issue #34.
    #
    # "undone" is the case worth keeping of these: it is the semantic
    # *inversion* — a board reporting that an operation was reversed reads as
    # one that completed. The "ok" branch has a dedicated test for its
    # equivalent ("error: broken sensor"); this is the "done" branch's, and
    # "done" is the one a pending feed_one()/sort_and_move() waits on.
    _feed(broker, line + "\n")
    assert sink.done == [line]
    assert sink.response == []


def test_a_diagnostic_line_satisfies_a_pending_feed_one(broker: SerialBroker, monkeypatch) -> None:
    """Characterization: the `done` substring completes a real feed/sort wait.

    This is the consequence that makes the unanchored matching matter rather
    than merely being untidy. `feed_one()` / `sort_and_move()` /
    `force_sort_and_move()` all return by waiting on `on_done`, so a board line
    that only *mentions* "done" reports a physical operation as complete when
    nothing completed. See issue #34.
    """
    fake = _WritableSerial()
    monkeypatch.setattr(broker, "_sp", fake)

    def feed_when_awaited() -> None:
        # _await_topic appends its handler after send_command returns; wait for
        # it to land rather than sleeping a fixed amount, so this stays fast
        # and deterministic.
        for _ in range(2000):
            if broker.on_done:
                break
            time.sleep(0.001)
        _feed(broker, "sensor calibration done at 12:00\n")

    waiter = threading.Thread(target=feed_when_awaited)
    waiter.start()
    try:
        assert broker.feed_one() is True, "a mere mention of 'done' satisfied the wait"
    finally:
        waiter.join()

    assert fake.written == [b"xf:0\n"]


def test_done_beats_ok_beats_error_beats_waiting(broker: SerialBroker, sink: _Sink) -> None:
    # Characterization: the branches are checked in a fixed order and each one
    # `continue`s, so a line matching several only ever fires the first. See
    # issue #34.
    _feed(broker, "done ok error waiting\n")
    assert sink.done == ["done ok error waiting"]
    assert sink.ok == sink.error == sink.waiting == []


def test_json_config_payload_containing_ok_never_reaches_on_response(broker: SerialBroker, sink: _Sink) -> None:
    # Characterization: get_config() captures JSON from on_response, so a board
    # config whose keys happen to contain "ok"/"done" would be swallowed by the
    # ack branches and the getconfig call would time out. See issue #34.
    _feed(broker, '{"feedspeed": 60, "lookahead": 2}\n')
    assert sink.ok == ['{"feedspeed": 60, "lookahead": 2}']
    assert sink.response == []


# ----- try_open handshake ----------------------------------------------------


class _FakeSerial:
    """Minimal stand-in for serial.Serial covering only what try_open touches."""

    def __init__(self, lines: list[str], read_all_text: str = "") -> None:
        self._lines = list(lines)
        self._read_all_text = read_all_text
        self.is_open = True
        self.timeout: float | None = None
        self.written: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0).encode("ascii")

    def read_all(self) -> bytes:
        return self._read_all_text.encode("ascii")

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.is_open = False


class _WritableSerial(_FakeSerial):
    """A fake that only has to accept writes — used for send_command paths."""

    def __init__(self) -> None:
        super().__init__(lines=[])


class _PartialReadSerial:
    """Returns each line verbatim, then stops the reader loop.

    Unlike `_FakeSerial` the lines are *not* newline-terminated, which is what
    a pyserial read timeout mid-line looks like.
    """

    def __init__(self, lines: list[str], stop_event: threading.Event) -> None:
        self._lines = list(lines)
        self._stop_event = stop_event
        self.is_open = True

    def readline(self) -> bytes:
        if not self._lines:
            self._stop_event.set()
            return b""
        return self._lines.pop(0).encode("ascii")


def _patch_serial(monkeypatch: pytest.MonkeyPatch, fake: _FakeSerial) -> dict[str, Any]:
    """Point `serial.Serial` at `fake`, returning the kwargs it was built with.

    `serial_broker` does a plain `import serial`, so `serial_broker.serial` *is*
    the pyserial module object — this patch is process-global for the duration
    of the test, not scoped to the importing module. It is safe only because
    monkeypatch restores it and the suite is never run in parallel (see
    `tests/conftest.py`); don't "fix" it into a module-local patch, there isn't
    one to make.
    """
    captured: dict[str, Any] = {}

    def _factory(**kwargs: Any) -> _FakeSerial:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(serial_broker.serial, "Serial", _factory)
    return captured


def test_try_open_accepts_a_version_line_containing_ok(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    # Characterization: the handshake accepts ANY version reply containing the
    # substring "ok" — including one that is reporting a problem. See issue #34.
    fake = _FakeSerial(lines=["\n", "not ok, firmware broken\n"])
    kwargs = _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.is_connected is True
        assert broker.firmware_version == "not ok, firmware broken"
        assert fake.written == [b"version\n"]
        # The port is opened at the (user-configurable) probe timeout and only
        # relaxed to the steady-state read timeout once the board answers.
        assert kwargs["port"] == "/dev/null-not-opened"
        assert kwargs["baudrate"] == broker.baud
        assert kwargs["timeout"] == broker.handshake_timeout_s
        assert fake.timeout == serial_broker.READ_TIMEOUT_S
    finally:
        broker.close()


def test_try_open_accepts_anything_containing_7_dot(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    # Characterization: "7." is the firmware-family check, unanchored, so a
    # timestamp or a bare "17.5" passes the handshake. See issue #34.
    fake = _FakeSerial(lines=["\n", "boot at 17.5s\n"])
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.firmware_version == "boot at 17.5s"
    finally:
        broker.close()


def test_try_open_accepts_a_real_version_string(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSerial(lines=["\n", "7.4.1\n"])
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.firmware_version == "7.4.1"
    finally:
        broker.close()


def test_try_open_accepts_a_ready_banner_even_without_a_version(
    broker: SerialBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSerial(lines=["Ready\n", "\n"])
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.firmware_version == "Unknown"
    finally:
        broker.close()


def test_try_open_reads_the_ready_banner_out_of_the_read_all_tail(
    broker: SerialBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The banner is `readline()` + `read_all()`, and boards are chatty.

    `readline()` returns at the first newline, so on a multi-line boot banner
    the "Ready" line arrives in the `read_all()` tail — the realistic path.
    """
    fake = _FakeSerial(lines=["booting...\n", "\n"], read_all_text="init ok\nReady\n")
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.firmware_version == "Unknown"
    finally:
        broker.close()


def test_try_open_ready_banner_check_is_case_sensitive(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    # Characterization: the banner check is `"Ready" in banner` on the RAW
    # text, while the version check immediately above it lowercases first. So a
    # board announcing itself in lowercase is rejected. See issue #34.
    fake = _FakeSerial(lines=["ready\n", "\n"])
    _patch_serial(monkeypatch, fake)
    assert broker.try_open() is False
    assert broker.is_connected is False


def test_try_open_rejects_an_unrecognized_board(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSerial(lines=["garbage\n", "3.2.1\n"])
    _patch_serial(monkeypatch, fake)
    assert broker.try_open() is False
    assert broker.is_connected is False
    assert fake.closed is True
    assert broker._sp is None, "the rejected port handle is dropped, not just closed"


def test_try_open_without_require_serial_ready_accepts_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSerial(lines=["garbage\n", "3.2.1\n"])
    _patch_serial(monkeypatch, fake)
    broker = SerialBroker(port="/dev/null-not-opened", require_serial_ready=False)
    try:
        assert broker.try_open() is True
    finally:
        broker.close()


def test_try_open_returns_false_when_the_port_cannot_be_opened(
    broker: SerialBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**_kwargs: Any) -> Any:
        raise serial_broker.serial.SerialException("no such port")

    monkeypatch.setattr(serial_broker.serial, "Serial", _boom)
    assert broker.try_open() is False
    assert broker.is_connected is False
