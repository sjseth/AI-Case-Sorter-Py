"""Tests for the EventBus — the only sanctioned worker-thread -> UI channel.

`sorter/events.py` had no test module at all. It is small, but every worker
thread in the app funnels through it, so the delivery guarantees (and the
non-guarantees) are worth pinning.
"""

from __future__ import annotations

import threading

from sorter.events import EventBus


def test_subscribe_post_drain_delivers_to_the_subscriber() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe("run/status", seen.append)

    bus.post("run/status", {"state": "running"})
    assert seen == [], "post() alone must not dispatch; drain() does"

    assert bus.drain() == 1
    assert seen == [{"state": "running"}]


def test_payload_defaults_to_none() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe("mode/changed", seen.append)
    bus.post("mode/changed")
    bus.drain()
    assert seen == [None]


def test_delivery_is_scoped_to_the_topic() -> None:
    bus = EventBus()
    run: list[object] = []
    serial: list[object] = []
    bus.subscribe("run/status", run.append)
    bus.subscribe("serial/status", serial.append)

    bus.post("run/status", 1)
    bus.drain()

    assert run == [1]
    assert serial == []


def test_multiple_subscribers_on_one_topic_all_fire() -> None:
    bus = EventBus()
    a: list[object] = []
    b: list[object] = []
    bus.subscribe("run/history", a.append)
    bus.subscribe("run/history", b.append)

    bus.post("run/history", "WIN")
    assert bus.drain() == 1, "one event dispatched, regardless of subscriber count"
    assert a == ["WIN"]
    assert b == ["WIN"]


def test_posting_to_a_topic_with_no_subscribers_is_a_no_op() -> None:
    bus = EventBus()
    bus.post("nobody/listening", 42)
    # The event is still consumed from the queue and counted.
    assert bus.drain() == 1
    assert bus.drain() == 0


def test_drain_returns_the_count_dispatched() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe("t", seen.append)
    for i in range(5):
        bus.post("t", i)

    assert bus.drain() == 5
    assert seen == [0, 1, 2, 3, 4]
    assert bus.drain() == 0


def test_drain_on_an_empty_bus_returns_zero() -> None:
    # Distinct from the trailing `drain() == 0` in the two tests above: this is
    # a bus that has never been posted to, so it covers the very first call the
    # Tk timer makes at startup, before anything has been published.
    assert EventBus().drain() == 0


def test_max_items_caps_a_drain_and_leaves_the_rest_queued() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe("t", seen.append)
    for i in range(10):
        bus.post("t", i)

    assert bus.drain(max_items=4) == 4
    assert seen == [0, 1, 2, 3]

    assert bus.drain(max_items=4) == 4
    assert seen == list(range(8))

    # FIFO order is preserved across the drains.
    assert bus.drain() == 2
    assert seen == list(range(10))


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[object] = []

    def handler(payload: object) -> None:
        seen.append(payload)

    bus.subscribe("t", handler)
    bus.post("t", 1)
    bus.drain()
    assert seen == [1]

    bus.unsubscribe("t", handler)
    bus.post("t", 2)
    assert bus.drain() == 1, "the event is still consumed, just not delivered"
    assert seen == [1]


def test_unsubscribe_removes_only_the_named_handler() -> None:
    bus = EventBus()
    a: list[object] = []
    b: list[object] = []

    def handler_a(payload: object) -> None:
        a.append(payload)

    def handler_b(payload: object) -> None:
        b.append(payload)

    bus.subscribe("t", handler_a)
    bus.subscribe("t", handler_b)
    bus.unsubscribe("t", handler_a)

    bus.post("t", 1)
    bus.drain()
    assert a == []
    assert b == [1]


def test_unsubscribe_of_an_unknown_topic_or_handler_is_harmless() -> None:
    bus = EventBus()
    bus.unsubscribe("never/subscribed", print)
    seen: list[object] = []
    bus.subscribe("t", seen.append)
    bus.unsubscribe("t", print)  # registered handler untouched
    bus.post("t", 1)
    bus.drain()
    assert seen == [1]


def test_a_raising_handler_is_swallowed_and_the_drain_continues() -> None:
    # Characterization: handler exceptions are caught and dropped on the floor
    # with no logging at all, so a broken UI handler fails completely silently.
    # Issue #32 proposes logging the exception here; the swallow itself stays
    # (one bad handler must not take down the Tk drain loop), so only the
    # logging is added and this assertion should keep holding.
    bus = EventBus()
    other: list[object] = []
    later: list[object] = []

    def boom(_payload: object) -> None:
        raise RuntimeError("handler blew up")

    bus.subscribe("t", boom)
    bus.subscribe("t", other.append)
    bus.subscribe("u", later.append)

    bus.post("t", "first")
    bus.post("u", "second")

    # Neither the sibling subscriber nor the next queued event is lost.
    assert bus.drain() == 2
    assert other == ["first"]
    assert later == ["second"]


def test_subscribing_during_a_drain_does_not_disturb_the_dispatch() -> None:
    # drain() iterates a copy of the handler list, so mutating subscriptions
    # from inside a handler is safe.
    bus = EventBus()
    late: list[object] = []

    def subscribe_more(_payload: object) -> None:
        bus.subscribe("t", late.append)

    bus.subscribe("t", subscribe_more)
    bus.post("t", 1)
    bus.drain()
    assert late == [], "the newly added handler must not see the in-flight event"

    bus.post("t", 2)
    bus.drain()
    assert late == [2]


def test_posts_from_worker_threads_are_all_delivered() -> None:
    bus = EventBus()
    seen: list[object] = []

    # NB: collect unconditionally and check the payloads *after* the drain. An
    # `assert` inside the handler would be inert — drain() catches every
    # handler exception (the behavior characterized above), so it could only
    # ever surface indirectly as a missing element.
    bus.subscribe("t", seen.append)

    def worker(base: int) -> None:
        for i in range(20):
            bus.post("t", base + i)

    threads = [threading.Thread(target=worker, args=(base,)) for base in (0, 100, 200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert bus.drain(max_items=1000) == 60
    assert all(isinstance(x, int) for x in seen), "payloads round-trip unchanged"
    numbers = [x for x in seen if isinstance(x, int)]
    assert sorted(numbers) == sorted(list(range(20)) + list(range(100, 120)) + list(range(200, 220)))
