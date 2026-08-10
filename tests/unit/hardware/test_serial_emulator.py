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
