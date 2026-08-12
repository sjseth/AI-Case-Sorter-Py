"""In-process fake broker so the rest of the stack runs without hardware.

Mirrors the public surface of SerialBroker — every command logs and fires
on_done after ~100ms.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

EMULATED_PORT = "Emulated"


class EmulatorBroker:
    def __init__(self, *, response_delay_s: float = 0.10) -> None:
        self.port = EMULATED_PORT
        self.baud = 9600
        self.require_serial_ready = False
        self.use_legacy_comms = False
        self.firmware_version = "Emulator-1.0"
        self.is_connected = False
        self._response_delay_s = response_delay_s

        self.on_done: list[Callable[[str], None]] = []
        self.on_ok: list[Callable[[str], None]] = []
        self.on_error: list[Callable[[str], None]] = []
        self.on_waiting: list[Callable[[str], None]] = []
        self.on_response: list[Callable[[str], None]] = []
        self.on_received: list[Callable[[str], None]] = []
        self.on_sent: list[Callable[[str], None]] = []

    # ----- lifecycle ----------------------------------------------------------

    def try_open(self) -> bool:
        self.is_connected = True
        return True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.is_connected = False

    def close(self) -> None:
        self.stop()

    # ----- protocol -----------------------------------------------------------

    def send_command(self, command: str) -> None:
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

    def send_raw(self, text: str) -> None:
        """Parity with SerialBroker.send_raw — the terminator is the caller's."""
        self.send_command(text.rstrip("\r\n"))

    def _fire_response_for(self, cmd: str) -> None:
        lower = cmd.lower()
        if lower in ("ping", "version"):
            self._dispatch("ok")
            return
        if lower == "stop":
            self._dispatch("ok")
            return
        if lower == "getconfig":
            self._dispatch('{"feedspeed":60,"sortspeed":70,"slotquantity":6}')
            return
        if ":" in lower and not lower.startswith("xf:"):
            self._dispatch("ok")
            return
        # xf:* (feed/force feed) and bare slot numbers complete with "done".
        self._dispatch("done")

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

        def _hit(_payload: str) -> None:
            done.set()

        handlers.append(_hit)
        try:
            self.send_command(command)
            return done.wait(timeout=timeout_s)
        finally:
            try:
                handlers.remove(_hit)
            except ValueError:
                pass

    def feed_one(self) -> bool:
        return self._send_and_await("xf:0", self.on_done, 2.0)

    def force_sort_and_move(self, slot: int) -> bool:
        return self._send_and_await(f"xf:{int(slot)}", self.on_done, 3.0)

    def sort_and_move(self, slot: int) -> bool:
        return self._send_and_await(str(int(slot)), self.on_done, 20.0)

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
