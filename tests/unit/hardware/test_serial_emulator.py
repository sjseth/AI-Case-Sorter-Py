"""Tests for the in-process serial emulator (and the broker's await/timeout pattern)."""

from __future__ import annotations

import time

from sorter.hardware.serial_emulator import EmulatorBroker


def test_emulator_feed_one_returns_done() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    assert em.feed_one() is True


def test_emulator_sort_returns_done() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    assert em.sort_and_move(3) is True


def test_emulator_get_config_returns_dict() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    cfg = em.get_config(timeout_s=1.0)
    assert isinstance(cfg, dict)
    assert "feedspeed" in cfg


def test_emulator_fires_on_sent_for_every_command() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    seen: list[str] = []
    em.on_sent.append(seen.append)
    em.send_command("ping")
    em.send_command("xf:0")
    # Give the timer thread a tick to dispatch the response so it doesn't blow up.
    time.sleep(0.05)
    assert "ping" in seen
    assert "xf:0" in seen


def test_emulator_update_init_settings_pushes_each_key() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    seen: list[str] = []
    em.on_sent.append(seen.append)
    em.update_init_settings({"feedspeed": 60, "airdropenabled": True})
    # Allow the timer thread to drain.
    time.sleep(0.1)
    assert "feedspeed:60" in seen
    assert "airdropenabled:1" in seen


# ----- disconnect parity (issue #35) ------------------------------------------


def test_simulate_disconnect_announces_once() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    reasons: list[str] = []
    em.on_disconnect.append(reasons.append)

    em.simulate_disconnect("cable pulled")
    em.simulate_disconnect("cable pulled again")

    assert reasons == ["cable pulled"]
    assert em.is_connected is False


def test_stop_announces_nothing() -> None:
    # Parity with SerialBroker: a stop we asked for is not a disconnect.
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    reasons: list[str] = []
    em.on_disconnect.append(reasons.append)
    em.stop()
    assert reasons == []


def test_a_pulled_cable_fails_the_next_sort_immediately() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    em.simulate_disconnect()

    started = time.monotonic()
    assert em.sort_and_move(3) is False
    # Would otherwise sit out the 20s sort timeout.
    assert time.monotonic() - started < 1.0


# ----- simulated hopper (end-of-brass) ----------------------------------------


def test_bottomless_hopper_is_the_default() -> None:
    em = EmulatorBroker(response_delay_s=0.01)
    em.try_open()
    for _ in range(5):
        assert em.sort_and_move(3) is True


def test_dry_hopper_answers_a_sort_with_waiting_lines() -> None:
    from sorter.hardware.serial_broker import SORT_DONE, SORT_EMPTY

    em = EmulatorBroker(response_delay_s=0.01, hopper=2)
    em.try_open()
    assert em.sort_and_move_watched(3) == (SORT_DONE, "")
    assert em.sort_and_move_watched(4) == (SORT_DONE, "")
    outcome, _ = em.sort_and_move_watched(5)
    assert outcome == SORT_EMPTY
    em.stop()  # kill the waiting chatter


def test_stop_cancels_the_dry_wait_with_a_done(monkeypatch) -> None:
    from sorter.hardware import serial_broker

    monkeypatch.setattr(serial_broker, "CANCEL_LISTEN_S", 0.2)
    em = EmulatorBroker(response_delay_s=0.01, hopper=0)
    em.try_open()
    outcome, _ = em.sort_and_move_watched(3)
    assert outcome == "empty"
    # The stop's single done ack = the wait died dry = clean.
    assert em.cancel_pending_feed() == "clean"
    # The waiting chatter stops once cancelled.
    waits: list[str] = []
    em.on_waiting.append(waits.append)
    time.sleep(0.1)
    assert waits == []


def test_forced_feed_completes_on_an_empty_hopper() -> None:
    em = EmulatorBroker(response_delay_s=0.01, hopper=0)
    em.try_open()
    # xf: bypasses the proximity gate, exactly like the firmware.
    assert em.feed_one() is True
    assert em.force_sort_and_move(2) is True


def test_flush_sort_and_move_sends_both_commands() -> None:
    em = EmulatorBroker(response_delay_s=0.01, hopper=0)
    em.try_open()
    seen: list[str] = []
    em.on_sent.append(seen.append)
    assert em.flush_sort_and_move(2, 5) is True
    assert seen == ["sortto:2", "xf:5"]


def test_set_hopper_refills_a_dry_feeder() -> None:
    from sorter.hardware.serial_broker import SORT_DONE, SORT_EMPTY

    em = EmulatorBroker(response_delay_s=0.01, hopper=0)
    em.try_open()
    outcome, _ = em.sort_and_move_watched(3)
    assert outcome == SORT_EMPTY
    em.cancel_pending_feed()
    em.set_hopper(1)
    assert em.sort_and_move_watched(3) == (SORT_DONE, "")
