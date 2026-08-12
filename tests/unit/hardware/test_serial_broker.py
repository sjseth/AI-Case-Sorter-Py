"""Tests for the real SerialBroker line parser and handshake.

`test_serial_emulator.py` exercises the *fake* broker; this file covers the
real `SerialBroker._process_buffer` / `try_open`.

The dispatch chain matches each response as an *anchored token* (issue #34):
the line, lowercased and stripped, equals ``ok``/``done``/``error``/``waiting``
or begins with it followed by a non-alphanumeric delimiter. A token embedded in
a larger word ("br-OK-en", "unDONE") does not match. This file originally
pinned the old unanchored-substring behavior as characterization tests; the
assertions below are the deliberate, reviewable flip of those pins.
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


# ----- anchored token matching (the #34 fix) ---------------------------------


def test_error_line_containing_ok_is_routed_to_on_error(broker: SerialBroker, sink: _Sink) -> None:
    # Under the old unanchored matching, "br-OK-en" won and this fired on_ok —
    # an error silently reported as a success. Anchored matching routes it by
    # its actual `error` prefix. This is the flip issue #34 exists for.
    _feed(broker, "error: broken sensor\n")
    assert sink.error == ["error: broken sensor"]
    assert sink.ok == []


@pytest.mark.parametrize("line", ["token", "bookkeeping"])
def test_word_merely_containing_ok_falls_through_to_on_response(broker: SerialBroker, sink: _Sink, line: str) -> None:
    # A token embedded in a larger word is not an acknowledgement. See #34.
    _feed(broker, line + "\n")
    assert sink.response == [line]
    assert sink.ok == []


@pytest.mark.parametrize("line", ["abandoned", "sensor calibration done at 12:00", "undone"])
def test_line_merely_containing_done_falls_through_to_on_response(broker: SerialBroker, sink: _Sink, line: str) -> None:
    # A line that merely mentions "done" must not satisfy a pending feed/sort
    # wait. "undone" is the semantic *inversion* — a board reporting an
    # operation was reversed must not read as one that completed. See #34.
    _feed(broker, line + "\n")
    assert sink.response == [line]
    assert sink.done == []


@pytest.mark.parametrize(
    ("line", "attr"),
    [
        ("error: sensor 3", "error"),
        ("done.", "done"),
        ("ok 123", "ok"),
        ("waiting for case", "waiting"),
        ("ERROR: Sensor 3", "error"),
    ],
)
def test_token_followed_by_a_delimiter_still_matches(broker: SerialBroker, sink: _Sink, line: str, attr: str) -> None:
    # Anchoring must not lose the legitimate "token plus detail" shape.
    _feed(broker, line + "\n")
    assert getattr(sink, attr) == [line]
    assert sink.response == []


def test_a_diagnostic_line_does_not_satisfy_a_pending_feed_one(
    broker: SerialBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line that only *mentions* "done" no longer completes a feed/sort wait.

    This is the consequence that made the old unanchored matching matter rather
    than merely being untidy. `feed_one()` / `sort_and_move()` /
    `force_sort_and_move()` all return by waiting on `on_done`, so a mere
    mention of "done" reported a physical operation as complete when nothing
    completed. A real `done` line afterwards must still satisfy it. See #34.
    """
    fake = _WritableSerial()
    monkeypatch.setattr(broker, "_sp", fake)
    # Keep the "wait must expire" half fast; the timeout's value is not under test.
    monkeypatch.setattr(serial_broker, "FEED_TIMEOUT_S", 0.3)

    def feed_when_awaited(lines: list[str]) -> None:
        # _await_topic appends its handler after send_command returns; wait for
        # it to land rather than sleeping a fixed amount, so this stays fast
        # and deterministic.
        for _ in range(2000):
            if broker.on_done:
                break
            time.sleep(0.001)
        for line in lines:
            _feed(broker, line)

    waiter = threading.Thread(target=feed_when_awaited, args=(["sensor calibration done at 12:00\n"],))
    waiter.start()
    try:
        assert broker.feed_one() is False, "a mere mention of 'done' satisfied the wait"
    finally:
        waiter.join()

    waiter = threading.Thread(target=feed_when_awaited, args=(["sensor calibration done at 12:00\n", "done\n"],))
    waiter.start()
    try:
        assert broker.feed_one() is True, "a real 'done' line no longer satisfies the wait"
    finally:
        waiter.join()

    assert fake.written == [b"xf:0\n", b"xf:0\n"]


def test_error_prefix_beats_everything(broker: SerialBroker, sink: _Sink) -> None:
    # Anchored prefixes mean at most one branch can match a line, but `error`
    # is deliberately checked first: a line carrying both readings must be
    # treated as the failure it is, whatever the matcher becomes. See #34.
    _feed(broker, "error: done\n")
    assert sink.error == ["error: done"]
    assert sink.done == sink.ok == sink.waiting == []


def test_a_line_opening_with_done_still_fires_on_done(broker: SerialBroker, sink: _Sink) -> None:
    _feed(broker, "done ok error waiting\n")
    assert sink.done == ["done ok error waiting"]
    assert sink.ok == sink.error == sink.waiting == []


def test_json_config_payload_containing_ok_reaches_on_response(broker: SerialBroker, sink: _Sink) -> None:
    # get_config() captures JSON from on_response. Under unanchored matching a
    # board config whose keys contain "ok" ("lookahead") was swallowed by the
    # ack branches and the getconfig call timed out. See #34.
    _feed(broker, '{"feedspeed": 60, "lookahead": 2}\n')
    assert sink.response == ['{"feedspeed": 60, "lookahead": 2}']
    assert sink.ok == []


# ----- the real firmware's vocabulary ----------------------------------------
#
# Everything above tests the parser against lines chosen to stress it. This
# section tests it against the lines a real board actually emits.
#
# The anchored matcher's blast radius is whatever the firmware prints, and the
# emulator only ever emits the clean tokens the parser wants — so it can never
# disprove a parser bug. This is the only place the fix is checked against the
# real vocabulary rather than against itself.
#
# Derived by reading every uncommented `Serial.print*` in the board firmware,
# which is upstream and stays there — this repo does not vendor a copy:
#
#   https://github.com/sjseth/AI-Case-Sorter-CS7.2/blob/
#     cd2d01ae3c6ef78a0eebcac13619e11fd5c7ca53/MicroController/
#     CS72_Firmware_V1.7/CS72_Firmware_V1.7.ino   (FIRMWARE_VERSION 7.2.260128.7.1)
#
# The commit is pinned so a future reader can diff forward and see whether the
# vocabulary moved. If it did, fix these cases against the newer firmware.


@pytest.mark.parametrize(
    ("line", "attr"),
    [
        # Boot banner and acks.
        ("Ready", "response"),
        ("ok", "ok"),
        (" ok", "ok"),  # the `ping` reply — note the leading space
        ("7.2.260128.7.1", "response"),  # `version`; try_open matches this separately
        # The two lines a run actually waits on.
        ("done", "done"),
        ("waiting for brass", "waiting"),
        # Every failure the board can report. All `error:`-prefixed, no space
        # after the colon — the exact shape anchored matching is written for.
        ("error:feed overtravel detected", "error"),
        ("error:feed stallguard", "error"),
        ("error:feed stallguard (homing)", "error"),
        ("error:sort stallguard", "error"),
        ("error:sort stallguard (homing)", "error"),
        ("error:sort offset stallguard (homing)", "error"),
        # Stall diagnostics, printed just before the error line. A feed stall
        # splits across two lines (println twice), a sort stall does not.
        ("STALL FEED SG_RESULT=112", "response"),
        (", DIAG=0", "response"),
        ("STALL SORT SG_RESULT=97, DIAG=1", "response"),
        # `status`.
        ("SORT microsteps: 16", "response"),
        ("SORT current: 900", "response"),
        ("SORT Stealth: 1", "response"),
        ("FEED microsteps: 16", "response"),
        ("FEED current: 1200", "response"),
        ("FEED Stealth: 0", "response"),
        # Test cycles.
        ("testing started", "response"),
        ("3 - 5", "response"),
        ("3 - Sorting to: 5", "response"),
        ("Sort Test Completed", "response"),
    ],
)
def test_real_firmware_line_routes_to_the_expected_callback(
    broker: SerialBroker, sink: _Sink, line: str, attr: str
) -> None:
    _feed(broker, line + "\n")
    assert getattr(sink, attr) == [line.strip()]


def test_real_getconfig_payload_reaches_on_response(broker: SerialBroker, sink: _Sink) -> None:
    """The board's real `getconfig` reply, verbatim in shape (one line, no ack).

    `get_config()` captures it from `on_response`, so any ack branch that
    swallowed it would hang the Serial tab's config load.
    """
    payload = (
        '{"FeedMotorCurrent":1200,"FeedMotorSpeed":60,"FeedCycleSteps":200,'
        '"SortMotorCurrent":900,"SortMotorSpeed":80,"SortSteps":400,'
        '"NotificationDelay":50,"SlotDropDelay":100,"AirDropEnabled":1,'
        '"AirDropPostDelay":10,"AirDropPreDelay":20,"AirDropSignalTime":30,'
        '"FeedHomingOffset":0,"SortHomingOffset":0,"AutoMotorStandbyTimeout":600,'
        '"CaseFanSpeedEnabled":1,"CaseFanLevel":128,"CameraLEDLevel":200,'
        '"DebounceTimeout":250,"DebouncePauseTime":40}'
    )
    _feed(broker, payload + "\n")
    assert sink.response == [payload]
    assert sink.ok == sink.done == sink.error == sink.waiting == []


def test_a_real_stall_reports_as_an_error_after_its_diagnostic(broker: SerialBroker, sink: _Sink) -> None:
    """A feed stall arrives as three lines, and only the last one is the error.

    `triggerFeedStall` prints `STALL FEED SG_RESULT=` + the value with
    `println`, so the `, DIAG=` half lands on its own line, then the
    `error:…` message. The two diagnostic lines must stay noise — routing
    either of them as an ack would let a stalled board look like a working one.
    """
    _feed(broker, "STALL FEED SG_RESULT=112\n, DIAG=0\nerror:feed stallguard\n")
    assert sink.response == ["STALL FEED SG_RESULT=112", ", DIAG=0"]
    assert sink.error == ["error:feed stallguard"]
    assert sink.done == sink.ok == []


def test_the_real_ping_ack_is_an_ok_despite_its_leading_space(broker: SerialBroker, sink: _Sink) -> None:
    # The firmware answers `ping` with `" ok\n"`. The parser's strip is what
    # makes that an ack rather than an unrecognized line, so pin it: the ping
    # thread runs on every idle connection, so this is the most frequently
    # exercised line in the whole protocol.
    _feed(broker, " ok\n")
    assert sink.ok == ["ok"]
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


def test_try_open_accepts_an_ok_version_reply(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSerial(lines=["\n", "OK\n"])
    kwargs = _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.is_connected is True
        assert broker.firmware_version == "OK"
        assert fake.written == [b"version\n"]
        # The port is opened at the (user-configurable) probe timeout and only
        # relaxed to the steady-state read timeout once the board answers.
        assert kwargs["port"] == "/dev/null-not-opened"
        assert kwargs["baudrate"] == broker.baud
        assert kwargs["timeout"] == broker.handshake_timeout_s
        assert fake.timeout == serial_broker.READ_TIMEOUT_S
    finally:
        broker.close()


def test_try_open_rejects_a_version_line_merely_containing_ok(
    broker: SerialBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under unanchored matching the handshake accepted ANY version reply
    # containing the substring "ok" — including one reporting a problem. See #34.
    fake = _FakeSerial(lines=["\n", "not ok, firmware broken\n"])
    _patch_serial(monkeypatch, fake)
    assert broker.try_open() is False
    assert broker.is_connected is False


def test_try_open_rejects_a_line_merely_containing_7_dot(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    # "7." is the firmware-family check, now anchored to the start of the
    # version reply, so a timestamp or a bare "17.5" no longer passes. See #34.
    fake = _FakeSerial(lines=["\n", "boot at 17.5s\n"])
    _patch_serial(monkeypatch, fake)
    assert broker.try_open() is False
    assert broker.is_connected is False


def test_try_open_accepts_a_real_version_string(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSerial(lines=["\n", "7.4.1\n"])
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.firmware_version == "7.4.1"
    finally:
        broker.close()


def test_try_open_accepts_the_real_boards_boot_banner_and_version(
    broker: SerialBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handshake as a real CS7.2 board actually plays it.

    `setup()` prints `Ready`, then `version` answers with the firmware's
    `FIRMWARE_VERSION`. Pinning the literal version keeps the `7.`-prefix
    check honest: it is a firmware *series* match, and a future 8.x board
    would fall back to the banner.
    """
    fake = _FakeSerial(lines=["Ready\n", "7.2.260128.7.1\n"])
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
        assert broker.firmware_version == "7.2.260128.7.1"
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


# ----- handshake visibility (issue #76) --------------------------------------


def test_try_open_reports_the_handshake_through_the_callbacks(
    broker: SerialBroker, sink: _Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe talks to the port directly, so it has to narrate itself.

    Without this the serial monitor is blank for exactly the connection that
    failed — the one worth reading.
    """
    fake = _FakeSerial(lines=["Ready\n", "7.2.260128.7.1\n"])
    _patch_serial(monkeypatch, fake)
    sent: list[str] = []
    broker.on_sent.append(sent.append)
    try:
        assert broker.try_open() is True
    finally:
        broker.close()
    assert sink.received == ["Ready", "7.2.260128.7.1"]
    assert sent == ["version"]


def test_try_open_reports_a_multi_line_banner_and_a_failed_handshake(
    broker: SerialBroker, sink: _Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeSerial(lines=["booting...\n", "3.2.1\n"], read_all_text="init ok\n\n")
    _patch_serial(monkeypatch, fake)
    assert broker.try_open() is False
    # Blank lines are dropped; everything the board actually printed survives.
    assert sink.received == ["booting...", "init ok", "3.2.1"]


def test_try_open_survives_a_callback_that_raises(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_line: str) -> None:
        raise RuntimeError("subscriber bug")

    broker.on_received.append(_boom)
    broker.on_sent.append(_boom)
    fake = _FakeSerial(lines=["Ready\n", "7.4.1\n"])
    _patch_serial(monkeypatch, fake)
    try:
        assert broker.try_open() is True
    finally:
        broker.close()


# ----- send_raw ---------------------------------------------------------------


def test_send_raw_writes_the_text_verbatim(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    # The monitor's line-ending selector owns the terminator; send_raw must
    # neither add one (as send_command does) nor drop the one it was given.
    fake = _WritableSerial()
    monkeypatch.setattr(broker, "_sp", fake)
    sent: list[str] = []
    broker.on_sent.append(sent.append)

    broker.send_raw("version\r\n")
    broker.send_raw("getconfig")

    assert fake.written == [b"version\r\n", b"getconfig"]
    # The echo is for display, so it carries no line ending either way.
    assert sent == ["version", "getconfig"]


def test_send_raw_is_a_noop_without_an_open_port(broker: SerialBroker) -> None:
    sent: list[str] = []
    broker.on_sent.append(sent.append)
    broker.send_raw("version\n")
    assert sent == []


def test_send_command_still_owns_its_newline(broker: SerialBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    # The protocol helpers are pinned to the firmware's vocabulary; send_raw
    # landing next to them must not have changed what they put on the wire.
    fake = _WritableSerial()
    monkeypatch.setattr(broker, "_sp", fake)
    broker.send_command("xf:0")
    assert fake.written == [b"xf:0\n"]
