"""In-process fake broker so the rest of the stack runs without hardware.

Mirrors the public surface of SerialBroker — every command logs and fires
on_done after ~100ms. That includes `on_disconnect`, which `simulate_disconnect`
is here to raise: a mid-run link loss is otherwise only reachable by unplugging
real hardware.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from . import serial_broker
from .serial_broker import (
    FEEDER_EMPTY_WAITS,
    SORT_DONE,
    SORT_EMPTY,
    SORT_ERROR,
    SORT_FAILED,
)

EMULATED_PORT = "Emulated"


class EmulatorBroker:
    def __init__(self, *, response_delay_s: float = 0.10, hopper: int | None = None) -> None:
        self.port = EMULATED_PORT
        self.baud = 9600
        self.require_serial_ready = False
        self.use_legacy_comms = False
        self.firmware_version = "Emulator-1.0"
        self.is_connected = False
        self._response_delay_s = response_delay_s
        # Simulated hopper: None = bottomless (the historical behavior). With
        # a count, each bare-slot sort consumes one case; once it hits zero a
        # bare sort answers "waiting for brass" once per delay tick — exactly
        # what the firmware's prox-gated feed does on a dry hopper — until a
        # `stop` cancels it (answered with `done`, as the real firmware does
        # via FeedCycleComplete) or an `xf:` forces the feed through.
        self._hopper = hopper
        self._feed_waiting = False
        self._state_lock = threading.Lock()

        self.on_done: list[Callable[[str], None]] = []
        self.on_ok: list[Callable[[str], None]] = []
        self.on_error: list[Callable[[str], None]] = []
        self.on_waiting: list[Callable[[str], None]] = []
        self.on_response: list[Callable[[str], None]] = []
        self.on_received: list[Callable[[str], None]] = []
        self.on_sent: list[Callable[[str], None]] = []
        self.on_disconnect: list[Callable[[str], None]] = []

    # ----- lifecycle ----------------------------------------------------------

    def try_open(self) -> bool:
        self.is_connected = True
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.is_connected = False
        with self._state_lock:
            self._feed_waiting = False

    def close(self) -> None:
        self.stop()

    def simulate_disconnect(self, reason: str = "emulated disconnect") -> None:
        """Pull the emulated cable — the one thing `stop()` deliberately isn't.

        A disconnect only means something when nobody asked for it, so the
        broker announces it and a `stop()` doesn't. Mirroring that here is what
        makes the mid-run path testable with no hardware (issue #35).
        """
        if not self.is_connected:
            return
        self.is_connected = False
        for cb in list(self.on_disconnect):
            try:
                cb(reason)
            except Exception:
                pass

    # ----- protocol -----------------------------------------------------------

    def send_command(self, command: str) -> bool:
        # A closed port swallows writes in SerialBroker; so does a pulled one
        # here, and the False is what stops a caller waiting for an answer that
        # is never coming.
        if not self.is_connected:
            return False
        cmd = command.rstrip("\n")
        for cb in list(self.on_sent):
            try:
                cb(cmd)
            except Exception:
                pass

        # All commands "complete" successfully after the configured delay.
        timer = threading.Timer(self._response_delay_s, self._fire_response_for, args=(cmd,))
        timer.daemon = True
        timer.start()
        return True

    def send_raw(self, text: str) -> None:
        """Parity with SerialBroker.send_raw — the terminator is the caller's."""
        self.send_command(text.rstrip("\r\n"))

    def set_hopper(self, count: int | None) -> None:
        """Load the simulated hopper (None = bottomless again)."""
        with self._state_lock:
            self._hopper = count

    def _fire_response_for(self, cmd: str) -> None:
        lower = cmd.lower()
        if lower in ("ping", "version"):
            self._dispatch("ok")
            return
        if lower == "stop":
            # The firmware always answers `stop` with exactly one `done`:
            # it sets FeedCycleComplete, and onFeedComplete prints even when
            # idle. It also cancels a feed parked on a dry proximity gate.
            with self._state_lock:
                self._feed_waiting = False
            self._dispatch("done")
            return
        if lower == "getconfig":
            self._dispatch('{"feedspeed":60,"sortspeed":70,"slotquantity":6}')
            return
        if lower.startswith("xf:"):
            # Forced feed: bypasses the proximity gate, so it completes even
            # on an empty hopper (consuming a case when one is there).
            with self._state_lock:
                self._feed_waiting = False
                if self._hopper is not None and self._hopper > 0:
                    self._hopper -= 1
            self._dispatch("done")
            return
        if ":" in lower:
            self._dispatch("ok")
            return
        # Bare slot number: sort + prox-gated feed. Dry hopper -> the feed
        # waits, printing "waiting for brass" once per delay tick until brass
        # arrives or a stop/xf cancels it.
        with self._state_lock:
            if self._hopper is not None and self._hopper <= 0:
                self._feed_waiting = True
            else:
                if self._hopper is not None:
                    self._hopper -= 1
                self._feed_waiting = False
        if self._feed_waiting:
            self._emit_waiting()
            return
        self._dispatch("done")

    def _emit_waiting(self) -> None:
        with self._state_lock:
            if not self._feed_waiting or not self.is_connected:
                return
        self._dispatch("waiting for brass")
        timer = threading.Timer(self._response_delay_s, self._emit_waiting)
        timer.daemon = True
        timer.start()

    def _dispatch(self, line: str) -> None:
        for cb in list(self.on_received):
            try:
                cb(line)
            except Exception:
                pass
        normalized = line.lower()
        if "done" in normalized:
            handlers = self.on_done
        elif "ok" in normalized:
            handlers = self.on_ok
        elif "error" in normalized:
            handlers = self.on_error
        elif "waiting" in normalized:
            handlers = self.on_waiting
        else:
            handlers = self.on_response
        for cb in list(handlers):
            try:
                cb(line)
            except Exception:
                pass

    # ----- helpers (parity with SerialBroker) ---------------------------------

    def purge_responses(self) -> None:
        pass

    def read_line(self) -> str:
        return ""

    def _send_and_await(self, command: str, handlers: list[Callable[[str], None]], timeout_s: float) -> bool:
        """Register the completion handler, THEN send.

        send_command schedules the response on a Timer, so registering after
        sending races: with a short response delay the Timer can fire (and
        dispatch to the not-yet-registered handler) before we start waiting,
        which would then block for the full timeout. Registering first closes
        that window.
        """
        done = threading.Event()
        hit = False

        def _hit(_payload: str) -> None:
            nonlocal hit
            hit = True
            done.set()

        def _abandon(_reason: str) -> None:
            done.set()

        handlers.append(_hit)
        self.on_disconnect.append(_abandon)
        try:
            if not self.send_command(command):
                return False
            done.wait(timeout=timeout_s)
            return hit
        finally:
            for target, handler in ((handlers, _hit), (self.on_disconnect, _abandon)):
                try:
                    target.remove(handler)
                except ValueError:
                    pass

    def feed_one(self) -> bool:
        return self._send_and_await("xf:0", self.on_done, 2.0)

    def force_sort_and_move(self, slot: int) -> bool:
        return self._send_and_await(f"xf:{int(slot)}", self.on_done, 3.0)

    def sort_and_move(self, slot: int) -> bool:
        return self._send_and_await(str(int(slot)), self.on_done, 20.0)

    def sort_and_move_watched(self, slot: int) -> tuple[str, str]:
        """Parity with SerialBroker.sort_and_move_watched."""
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
            if waits >= FEEDER_EMPTY_WAITS:
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
            if not self.send_command(str(int(slot))):
                return SORT_FAILED, ""
            done.wait(timeout=20.0)
            return outcome, detail
        finally:
            for handlers, handler in registrations:
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass

    def cancel_pending_feed(self) -> str:
        """Parity with SerialBroker.cancel_pending_feed."""
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
            if not self.send_command("stop"):
                return "failed"
            # Read at call time so tests can shorten the listen window.
            finished.wait(timeout=serial_broker.CANCEL_LISTEN_S)
            return outcome
        finally:
            for handlers, handler in registrations:
                try:
                    handlers.remove(handler)
                except ValueError:
                    pass

    def flush_sort_and_move(self, prev_slot: int, slot: int) -> bool:
        """Parity with SerialBroker.flush_sort_and_move."""
        if not self.send_command(f"sortto:{int(prev_slot)}"):
            return False
        return self._send_and_await(f"xf:{int(slot)}", self.on_done, 20.0)

    def move_sorter_to_slot(self, slot: int) -> None:
        self.send_command(f"sortto:{int(slot)}")

    def stop_run(self) -> None:
        self.send_command("stop")

    def use_feed_sensor(self, enabled: bool) -> None:
        self.send_command(f"usefeedsensor:{1 if enabled else 0}")

    def get_config(self, timeout_s: float = 3.0) -> dict[str, Any] | None:
        import json

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
            self.send_command("getconfig")
            done.wait(timeout=timeout_s)
        finally:
            try:
                self.on_response.remove(_capture)
            except ValueError:
                pass
        return result

    def update_init_settings(self, settings: dict[str, Any]) -> None:
        for key, value in settings.items():
            if isinstance(value, bool):
                value = 1 if value else 0
            self.send_command(f"{key}:{value}")
            time.sleep(0.005)
