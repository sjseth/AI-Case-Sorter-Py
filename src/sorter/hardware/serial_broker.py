"""Serial broker for the Case Sorter control board.

Ports the legacy app's serial behavior (open/handshake/ping/reader/dispatch) and
the public command surface (feed_one, force_sort_and_move, sort_and_move,
get_config, update_init_settings, etc.).

Threading model: one reader thread, one ping thread, writes serialized with a lock.
Callbacks are invoked on the reader thread — except `on_disconnect`, which fires
on whichever thread first noticed the link die (the reader, or a caller whose
write failed). UI layers should post into an EventBus and drain it on the UI
thread, so the difference doesn't reach a widget either way.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import serial
from serial.tools import list_ports

READ_TIMEOUT_S = 0.5  # short so the reader thread can react to stop()
PING_INTERVAL_S = 1.0
PING_IDLE_THRESHOLD_S = 2.0
HANDSHAKE_READ_TIMEOUT_S = 4.0  # configurable via Serial Config -> Probe timeout
DISCOVERY_HANDSHAKE_TIMEOUT_S = 1.5  # legacy, retained for callers
FEED_TIMEOUT_S = 2.0
FORCE_FEED_TIMEOUT_S = 3.0
SORT_TIMEOUT_S = 20.0

# End-of-brass detection. With the feed sensor enabled, the firmware prints
# "waiting for brass" about once per second while a scheduled feed sits on a
# dry proximity gate — so this many consecutive lines on one sort ack means
# the hopper has been empty for that many seconds, not a case mid-fall.
FEEDER_EMPTY_WAITS = 6
# How long cancel_pending_feed() listens after `stop` for the race where a
# case landed on the sensor just before the stop went out: the waiting feed
# fires first and its real `done` arrives ahead of the stop's ack. Must
# outlast one full feed cycle.
CANCEL_LISTEN_S = 3.0

# sort_and_move_watched outcomes.
SORT_DONE = "done"
SORT_EMPTY = "empty"  # the feeder is dry (FEEDER_EMPTY_WAITS consecutive waits)
SORT_ERROR = "error"  # the board reported an error line (jam, stall)
SORT_FAILED = "failed"  # timeout or link loss — ask is_connected which


Callback = Callable[[str], None]


def _matches_token(line: str, token: str) -> bool:
    """True when `line` *is* the response `token`, not merely contains it.

    Anchored at the start: the line equals the token, or begins with it
    followed by a non-alphanumeric delimiter ("error: sensor 3", "done.").
    A token embedded in a larger word ("broken", "undone") never matches —
    see issue #34 for how the old unanchored `in` test misrouted those.
    Caller passes `line` already lowercased/stripped.
    """
    if not line.startswith(token):
        return False
    rest = line[len(token) :]
    return not rest or not rest[0].isalnum()


def list_serial_ports() -> list[str]:
    return [p.device for p in list_ports.comports()]


def is_probe_candidate(device: str) -> bool:
    """Whether the startup auto-connect should handshake this port unprompted.

    Only restrictive on macOS, where `comports()` returns pseudo-ports that
    can never be the board — `cu.Bluetooth-Incoming-Port`, `cu.debug-console`,
    and a `cu.<name>` node per paired Bluetooth headset. Probing those is
    worse than noise: each one waits out the handshake timeout, and opening a
    Bluetooth serial port can wake the radio link to the paired device. A
    real board arrives via a USB serial adapter, and every macOS driver for
    those puts "usb" in the node name (`cu.usbmodem*`, `cu.usbserial-*`,
    `cu.wchusbserial*`, `cu.SLAB_USBtoUART`).

    This gates only the *automatic* walk: Settings → Serial still lists every
    port for manual connection, and a port the user saved is always probed.
    """
    if sys.platform != "darwin":
        return True
    return "usb" in device.lower()


class SerialBroker:
    """Thread-safe wrapper around pyserial."""

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        require_serial_ready: bool = True,
        handshake_timeout_s: float = HANDSHAKE_READ_TIMEOUT_S,
    ) -> None:
        self.port = port
        self.baud = baud
        self.require_serial_ready = require_serial_ready
        self.handshake_timeout_s = handshake_timeout_s

        self.firmware_version = "Unknown"
        self.is_connected = False

        self._sp: serial.Serial | None = None
        self._write_lock = threading.Lock()
        self._port_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Set when a disconnect has been *announced* and not yet handshaked
        # away again. `is_connected` can't stand in for it: a broker that
        # never opened a port has it False too, and that is not a link that
        # died under a pending command.
        self._link_lost = False
        self._last_activity = time.monotonic()

        self._reader_thread: threading.Thread | None = None
        self._ping_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._buf = ""
        self._most_recent_response = ""

        # Event hooks (fired on the reader thread).
        self.on_done: list[Callback] = []
        self.on_ok: list[Callback] = []
        self.on_error: list[Callback] = []
        self.on_waiting: list[Callback] = []
        self.on_response: list[Callback] = []
        self.on_received: list[Callback] = []  # every line, raw
        self.on_sent: list[Callback] = []  # every outbound command (with newline stripped)
        self.on_disconnect: list[Callback] = []  # the link died; payload is why

    # ----- lifecycle ----------------------------------------------------------

    def try_open(self) -> bool:
        with self._port_lock:
            if self._sp is not None:
                try:
                    if self._sp.is_open:
                        self._sp.close()
                except Exception:
                    pass
                self._sp = None

            try:
                self._sp = serial.Serial(
                    port=self.port,
                    baudrate=self.baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self.handshake_timeout_s,
                    dsrdtr=False,
                )
                self._sp.dtr = True
            except (serial.SerialException, OSError):
                self._sp = None
                self.is_connected = False
                return False

        # Handshake: read banner, write "version", inspect reply. It talks to
        # the port directly rather than through send_command/_reader_loop, so
        # it has to report itself to the callbacks by hand — otherwise a probe
        # that never handshakes leaves the serial monitor blank, which is
        # exactly when its contents matter (issue #76).
        try:
            banner = self._sp.readline().decode("ascii", errors="ignore")
        except Exception:
            banner = ""
        try:
            banner += self._sp.read_all().decode("ascii", errors="ignore")
        except Exception:
            pass
        for line in banner.splitlines():
            line = line.strip("\r\n\t ")
            if line:
                self._fire(self.on_received, line)

        try:
            self._sp.write(b"version\n")
            self._fire(self.on_sent, "version")
            version_line = self._sp.readline().decode("ascii", errors="ignore").strip()
        except Exception:
            version_line = ""
        if version_line:
            self._fire(self.on_received, version_line)

        self.firmware_version = version_line or "Unknown"
        normalized = version_line.lower()

        connected = False
        if _matches_token(normalized, "ok") or normalized.startswith("7."):
            connected = True
        elif "Ready" in banner or not self.require_serial_ready:
            connected = True

        if connected:
            self._sp.timeout = READ_TIMEOUT_S
            self.is_connected = True
            self._link_lost = False
            return True

        try:
            self._sp.close()
        except Exception:
            pass
        self._sp = None
        self.is_connected = False
        return False

    def start(self) -> None:
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="SerialReader", daemon=True)
        self._ping_thread = threading.Thread(target=self._ping_loop, name="SerialPing", daemon=True)
        self._reader_thread.start()
        self._ping_thread.start()

    def stop(self) -> None:
        # Set before the port closes: the reader is very likely mid-read and
        # will fail on the close, and this is what tells `_mark_disconnected`
        # that failure was asked for rather than the cable coming out.
        self._stop_event.set()
        self.is_connected = False
        with self._port_lock:
            if self._sp is not None:
                try:
                    if self._sp.is_open:
                        self._sp.close()
                except Exception:
                    pass
                self._sp = None

    def close(self) -> None:
        self.stop()

    def _mark_disconnected(self, reason: str) -> None:
        """Flip to disconnected and announce it — at most once, and never for a
        `stop()` we asked for.

        Both the reader thread and a failing write can notice the same dropped
        link, so the transition is what fires the callbacks, not the noticing.
        """
        with self._state_lock:
            was_connected = self.is_connected
            self.is_connected = False
            announce = was_connected and not self._stop_event.is_set()
            if announce:
                self._link_lost = True
        if announce:
            self._fire(self.on_disconnect, reason)

    # ----- low-level send/read ------------------------------------------------

    def _write(self, payload: str) -> bool:
        """Write `payload` verbatim under the write lock. False if the link died.

        The disconnect is announced *after* the lock is released — an
        `on_disconnect` handler is free to talk to the broker, and firing it
        from inside the lock would let it deadlock against this write.
        """
        failure = ""
        with self._write_lock:
            if self._sp is None or not self._sp.is_open:
                return False
            try:
                self._sp.write(payload.encode("ascii", errors="ignore"))
                self._sp.flush()
            except (serial.SerialException, OSError) as exc:
                failure = f"write failed on {self.port}: {exc}"
            else:
                self._last_activity = time.monotonic()
        if failure:
            self._mark_disconnected(failure)
            return False
        return True

    def send_command(self, command: str) -> bool:
        """Send one protocol command. False when it never reached the wire.

        The return value is what keeps the helpers below from waiting on a
        board that was never asked: a write that failed has no answer coming,
        and waiting out the timeout for it is the "generic timeout" issue #35
        is about.
        """
        if not command.endswith("\n"):
            command += "\n"
        if not self._write(command):
            return False
        self._fire(self.on_sent, command.rstrip("\n"))
        return True

    def send_raw(self, text: str) -> None:
        """Write `text` byte-for-byte — nothing appended, nothing normalised.

        For the serial monitor's line-ending selector. Every protocol helper
        goes through `send_command`, which owns the trailing newline the
        firmware expects; keep it that way.
        """
        if not self._write(text):
            return
        self._fire(self.on_sent, text.rstrip("\r\n"))

    def purge_responses(self) -> None:
        time.sleep(0.2)
        self._buf = ""
        self._most_recent_response = ""
        if self._sp is not None and self._sp.is_open:
            try:
                self._sp.reset_input_buffer()
            except Exception:
                pass

    def read_line(self) -> str:
        line = self._most_recent_response
        self._most_recent_response = ""
        return line

    # ----- reader / ping loops ------------------------------------------------

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._sp is None or not self._sp.is_open:
                time.sleep(0.3)
                continue
            try:
                line = self._sp.readline().decode("ascii", errors="ignore")
                if not line:
                    continue
                self._buf += line if line.endswith("\n") else line + "\n"
                self._process_buffer()
            except (serial.SerialException, OSError, TypeError, AttributeError) as exc:
                # TypeError/AttributeError covers the pyserial race where
                # stop() closes the port (self.fd -> None) while readline()
                # is mid-read — os.read(None, ...) raises TypeError instead
                # of OSError. Treat it as a clean disconnect so the reader
                # thread doesn't crash on shutdown / reconnect; `stop()` sets
                # the stop event first, so that case announces nothing.
                self._mark_disconnected(f"read failed on {self.port}: {exc}")
                time.sleep(0.3)

    def _ping_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(PING_INTERVAL_S)
            if self._stop_event.is_set():
                return
            if (time.monotonic() - self._last_activity) > PING_IDLE_THRESHOLD_S:
                if self.is_connected and self._sp is not None and self._sp.is_open:
                    self.send_command("ping")

    def _process_buffer(self) -> None:
        self._last_activity = time.monotonic()
        while "\n" in self._buf:
            line, sep, rest = self._buf.partition("\n")
            self._buf = rest
            line = line.strip("\r\n\t ")
            if not line:
                continue
            self._most_recent_response = line
            self._fire(self.on_received, line)

            normalized = line.lower()
            # Anchored prefixes can only ever match one branch, but keep
            # `error` first anyway: a line carrying both readings must be
            # treated as the failure it is, whatever the matcher becomes.
            if _matches_token(normalized, "error"):
                self._fire(self.on_error, line)
                continue
            if _matches_token(normalized, "done"):
                self._fire(self.on_done, line)
                continue
            if _matches_token(normalized, "ok"):
                self._fire(self.on_ok, line)
                continue
            if _matches_token(normalized, "waiting"):
                self._fire(self.on_waiting, line)
                continue
            self._fire(self.on_response, line)

    @staticmethod
    def _fire(handlers: Iterable[Callback], payload: str) -> None:
        for h in list(handlers):
            try:
                h(payload)
            except Exception:
                pass

    # ----- command helpers ----------------------------------------------------

    def _await_event(self, event: threading.Event, timeout_s: float) -> bool:
        return event.wait(timeout=timeout_s)

    def _await_topic(self, topic_handlers: list[Callback], timeout_s: float) -> bool:
        """Wait for one response line, or for the link to die.

        A disconnect wakes the wait immediately and returns False: a dead port
        will never answer, and sitting out the full timeout is what made an
        unplugged cable indistinguishable from a slow board (issue #35).
        """
        done = threading.Event()
        hit = False

        def _hit(_payload: str) -> None:
            nonlocal hit
            hit = True
            done.set()

        def _abandon(_reason: str) -> None:
            done.set()

        topic_handlers.append(_hit)
        self.on_disconnect.append(_abandon)
        try:
            # The transition is announced exactly once, so a link that died
            # between the write going out and this registration has nothing
            # left to wake us with — and the wait below would sit out the full
            # timeout, which is the symptom the whole change is about.
            if self._link_lost:
                return False
            done.wait(timeout=timeout_s)
            return hit
        finally:
            for handlers, handler in ((topic_handlers, _hit), (self.on_disconnect, _abandon)):
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass

    def _await_sort_outcome(self, timeout_s: float, empty_wait_limit: int) -> tuple[str, str]:
        """Wait out a sort ack, watching the feed gate as well as the result.

        Returns ``(outcome, detail)`` where outcome is one of the SORT_*
        constants and detail carries the board's error line for SORT_ERROR.
        A `done` is the normal completion; an `error:` line is a jam/stall;
        `empty_wait_limit` consecutive "waiting for brass" lines mean the
        hopper is dry (the firmware prints one per second while a scheduled
        feed waits on the proximity sensor); silence or a dead link is
        SORT_FAILED, exactly as _await_topic reports it.
        """
        done = threading.Event()
        outcome = SORT_FAILED
        detail = ""
        waits = 0

        def _done(_payload: str) -> None:
            nonlocal outcome
            outcome = SORT_DONE
            done.set()

        def _error(payload: str) -> None:
            nonlocal outcome, detail
            outcome = SORT_ERROR
            detail = payload
            done.set()

        def _waiting(_payload: str) -> None:
            nonlocal outcome, waits
            waits += 1
            if waits >= empty_wait_limit:
                outcome = SORT_EMPTY
                done.set()

        def _abandon(_reason: str) -> None:
            done.set()

        registrations = (
            (self.on_done, _done),
            (self.on_error, _error),
            (self.on_waiting, _waiting),
            (self.on_disconnect, _abandon),
        )
        for handlers, handler in registrations:
            handlers.append(handler)
        try:
            # Same reasoning as _await_topic: a link that died before this
            # registration has nothing left to wake us with.
            if self._link_lost:
                return SORT_FAILED, ""
            done.wait(timeout=timeout_s)
            return outcome, detail
        finally:
            for handlers, handler in registrations:
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass

    def feed_one(self) -> bool:
        """xf:0 — feed a single case. Returns True on done."""
        if not self.send_command("xf:0"):
            return False
        return self._await_topic(self.on_done, FEED_TIMEOUT_S)

    def force_sort_and_move(self, slot: int) -> bool:
        """xf:<slot> — force feed to a specific slot."""
        if not self.send_command(f"xf:{int(slot)}"):
            return False
        return self._await_topic(self.on_done, FORCE_FEED_TIMEOUT_S)

    def sort_and_move(self, slot: int) -> bool:
        """<slot> — sort the just-fed case to the given slot."""
        if not self.send_command(str(int(slot))):
            return False
        return self._await_topic(self.on_done, SORT_TIMEOUT_S)

    def sort_and_move_watched(self, slot: int) -> tuple[str, str]:
        """<slot> — sort like sort_and_move, but tell the caller *why* it ended.

        The continuous run uses this so an empty hopper is a state, not a
        20-second timeout: the bare sort's feed half waits on the proximity
        sensor, and FEEDER_EMPTY_WAITS consecutive "waiting for brass" lines
        mean the hopper is dry with up to three cases still in the wheel —
        which cancel_pending_feed() + flush_sort_and_move() then place. An
        `error:` reply (jam, stall) is reported as such instead of riding out
        the timeout. Returns ``(outcome, detail)`` — see the SORT_* constants.
        """
        if not self.send_command(str(int(slot))):
            return SORT_FAILED, ""
        return self._await_sort_outcome(SORT_TIMEOUT_S, FEEDER_EMPTY_WAITS)

    def cancel_pending_feed(self) -> str:
        """stop — cancel a feed that is waiting on a dry proximity gate.

        First move of the end-of-brass flush, and the racy one: if a case
        landed on the sensor just before the stop went out, the waiting feed
        fires FIRST and its real `done` arrives ahead of the stop's ack (the
        firmware always answers `stop` with exactly one `done` via
        FeedCycleComplete). So two `done` lines inside the listen window mean
        the sort completed for real — return "resumed" and keep sorting. One
        or zero mean the wait died dry — "clean", safe to start flushing.
        Any `error:` line or a dead link is "failed".
        """
        done_count = 0
        finished = threading.Event()
        outcome = "clean"

        def _done(_payload: str) -> None:
            nonlocal done_count, outcome
            done_count += 1
            if done_count >= 2:
                outcome = "resumed"
                finished.set()

        def _error(_payload: str) -> None:
            nonlocal outcome
            outcome = "failed"
            finished.set()

        def _abandon(_reason: str) -> None:
            nonlocal outcome
            outcome = "failed"
            finished.set()

        registrations = (
            (self.on_done, _done),
            (self.on_error, _error),
            (self.on_disconnect, _abandon),
        )
        for handlers, handler in registrations:
            handlers.append(handler)
        try:
            if not self.send_command("stop") or self._link_lost:
                return "failed"
            finished.wait(timeout=CANCEL_LISTEN_S)
            return outcome
        finally:
            for handlers, handler in registrations:
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass

    def flush_sort_and_move(self, prev_slot: int, slot: int) -> bool:
        """sortto:<prev> + xf:<slot> — one end-of-brass flush cycle.

        Issued only after cancel_pending_feed() came back "clean". The
        `sortto:` moves the arm exactly where the cancelled bare sort would
        have (and collapses the firmware's two-deep queue, killing any stale
        skew), so the case at the drop port falls into `prev_slot` — its true
        slot. The `xf:` is then a zero-travel forced feed that executes the
        drop without waiting on the (dry) proximity gate, walking the next
        straggler into the camera. Returns True on done; False on an error
        reply, a timeout, or a dead link — a jam mid-flush must stop the
        sequence before anything drops blind.
        """
        if not self.send_command(f"sortto:{int(prev_slot)}"):
            return False
        if not self.send_command(f"xf:{int(slot)}"):
            return False
        outcome, _detail = self._await_sort_outcome(SORT_TIMEOUT_S, FEEDER_EMPTY_WAITS)
        return outcome == SORT_DONE

    def move_sorter_to_slot(self, slot: int) -> None:
        """sortto:<slot> — move sorter only, no sort cycle."""
        self.send_command(f"sortto:{int(slot)}")

    def stop_run(self) -> None:
        """stop — abort current operation (best-effort)."""
        self.send_command("stop")

    def use_feed_sensor(self, enabled: bool) -> None:
        self.send_command(f"usefeedsensor:{1 if enabled else 0}")

    def get_config(self, timeout_s: float = 3.0) -> dict[str, Any] | None:
        """getconfig — board returns a JSON dict on a single line."""
        result: dict[str, Any] | None = None
        done = threading.Event()

        def _capture(payload: str) -> None:
            nonlocal result
            try:
                result = json.loads(payload)
                done.set()
            except json.JSONDecodeError:
                pass

        self.on_response.append(_capture)
        try:
            self.purge_responses()
            self.send_command("getconfig")
            done.wait(timeout=timeout_s)
        finally:
            try:
                self.on_response.remove(_capture)
            except ValueError:
                pass
        return result

    def update_init_settings(self, settings: dict[str, Any]) -> None:
        """Push each key:value pair to the board."""
        for key, value in settings.items():
            if isinstance(value, bool):
                value = 1 if value else 0
            self.send_command(f"{key}:{value}")
            time.sleep(0.03)
