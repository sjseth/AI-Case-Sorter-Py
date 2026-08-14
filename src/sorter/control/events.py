"""Thread-safe pub/sub used to marshal worker-thread events onto the UI thread.

All worker threads (serial reader, camera grabber, HTTP worker) push (topic, payload)
tuples onto a single Queue; the UI drains it on a timer from its own thread.
"""

from __future__ import annotations

import queue
from collections import defaultdict
from collections.abc import Callable
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._subs: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        if handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    def post(self, topic: str, payload: Any = None) -> None:
        """Called from any thread."""
        self._q.put((topic, payload))

    def drain(self, max_items: int = 64) -> int:
        """Called from the UI thread. Returns count dispatched."""
        count = 0
        while count < max_items:
            try:
                topic, payload = self._q.get_nowait()
            except queue.Empty:
                break
            for handler in list(self._subs.get(topic, [])):
                try:
                    handler(payload)
                except Exception:
                    pass
            count += 1
        return count
