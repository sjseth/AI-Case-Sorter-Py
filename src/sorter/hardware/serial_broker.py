"""Serial broker for the Case Sorter control board.

Ports the legacy app's serial behavior (open/handshake/ping/reader/dispatch) and
the public command surface (feed_one, force_sort_and_move, sort_and_move,
get_config, update_init_settings, etc.).

Threading model: one reader thread, one ping thread, writes serialized with a lock.
Each callback is invoked on the reader thread; UI layers should post into an
EventBus and drain it from the Tk main loop.
"""

from __future__ import annotations

import json
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


Callback = Callable[[str], None]


def list_serial_ports() -> list[str]:
    return [p.device for p in list_ports.comports()]


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

        # Handshake: read banner, write "version", inspect reply.
        try:
            banner = self._sp.readline().decode("ascii", errors="ignore")
        except Exception:
            banner = ""
        try:
            banner += self._sp.read_all().decode("ascii", errors="ignore")
        except Exception:
            pass

        try:
            self._sp.write(b"version\n")
            version_line = self._sp.readline().decode("ascii", errors="ignore").strip()
        except Exception:
            version_line = ""

        self.firmware_version = version_line or "Unknown"
        normalized = version_line.lower()

        connected = False
        if "ok" in normalized or "7." in normalized:
            connected = True
        elif "Ready" in banner or not self.require_serial_ready:
            connected = True

        if connected:
            self._sp.timeout = READ_TIMEOUT_S
            self.is_connected = True
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
        self.is_connected = False
        self._stop_event.set()
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

    # ----- low-level send/read ------------------------------------------------

    def send_command(self, command: str) -> None:
        if not command.endswith("\n"):
            command += "\n"
        with self._write_lock:
            if self._sp is None or not self._sp.is_open:
                return
            try:
                self._sp.write(command.encode("ascii", errors="ignore"))
                self._sp.flush()
            except (serial.SerialException, OSError):
                self.is_connected = False
                return
            self._last_activity = time.monotonic()
        stripped = command.rstrip("\n")
        for cb in list(self.on_sent):
            try:
                cb(stripped)
            except Exception:
                pass

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
            except (serial.SerialException, OSError, TypeError, AttributeError):
                # TypeError/AttributeError covers the pyserial race where
                # stop() closes the port (self.fd -> None) while readline()
                # is mid-read — os.read(None, ...) raises TypeError instead
                # of OSError. Treat it as a clean disconnect so the reader
                # thread doesn't crash on shutdown / reconnect.
                self.is_connected = False
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
            if "done" in normalized:
                self._fire(self.on_done, line)
                continue
            if "ok" in normalized:
                self._fire(self.on_ok, line)
                continue
            if "error" in normalized:
                self._fire(self.on_error, line)
                continue
            if "waiting" in normalized:
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
        done = threading.Event()

        def _hit(_payload: str) -> None:
            done.set()

        topic_handlers.append(_hit)
        try:
            return done.wait(timeout=timeout_s)
        finally:
            try:
                topic_handlers.remove(_hit)
            except ValueError:
                pass

    def feed_one(self) -> bool:
        """xf:0 — feed a single case. Returns True on done."""
        self.send_command("xf:0")
        return self._await_topic(self.on_done, FEED_TIMEOUT_S)

    def force_sort_and_move(self, slot: int) -> bool:
        """xf:<slot> — force feed to a specific slot."""
        self.send_command(f"xf:{int(slot)}")
        return self._await_topic(self.on_done, FORCE_FEED_TIMEOUT_S)

    def sort_and_move(self, slot: int) -> bool:
        """<slot> — sort the just-fed case to the given slot."""
        self.send_command(str(int(slot)))
        return self._await_topic(self.on_done, SORT_TIMEOUT_S)

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
