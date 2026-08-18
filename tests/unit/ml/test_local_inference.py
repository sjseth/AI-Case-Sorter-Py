"""Unit tests for `_pick_device` — CUDA, then MPS, then CPU (#36).

torch is the optional `[ml]` extra and genuinely absent from this dev/CI
environment, so every branch is exercised through a fake torch module. What
these pin: the probe order, the probe-then-commit fallback on both GPU
branches (an MPS op gap raises at runtime, so the probe is load-bearing),
and that a torch build without an MPS backend reads as "no MPS" rather than
raising.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from sorter.ml import local_inference


def _fake_torch(
    *,
    cuda_available: bool = False,
    mps_backend: bool = True,
    mps_available: bool = False,
    probe_fails_on: str | None = None,
) -> Any:
    """A torch stand-in serving exactly what `_pick_device` asks of it.

    `mps_backend=False` models a build with no `torch.backends.mps` at all
    (CPU-only / CUDA wheels); `probe_fails_on` makes `randn` raise for that
    device string, simulating a probe allocation failure.
    """

    def randn(*_shape: int, device: str | None = None) -> Any:
        if probe_fails_on is not None and device == probe_fails_on:
            raise RuntimeError(f"fake {device} probe failure")
        return SimpleNamespace(sum=lambda: SimpleNamespace(item=lambda: 0.0))

    backends = SimpleNamespace()
    if mps_backend:
        backends.mps = SimpleNamespace(is_available=lambda: mps_available)
    return SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: cuda_available,
            get_device_name=lambda _i: "Fake GPU",
        ),
        backends=backends,
        device=lambda kind: SimpleNamespace(type=kind),
        randn=randn,
    )


@pytest.fixture(autouse=True)
def _fresh_device_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """_pick_device writes the module-level cache; keep tests independent."""
    monkeypatch.setattr(local_inference, "_device_cache", None)


def test_cuda_wins_over_mps() -> None:
    device = local_inference._pick_device(_fake_torch(cuda_available=True, mps_available=True))
    assert device.type == "cuda"


def test_mps_is_picked_when_cuda_is_absent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=local_inference.__name__):
        device = local_inference._pick_device(_fake_torch(mps_available=True))
    assert device.type == "mps"
    assert "MPS ok" in caplog.text


def test_a_failing_mps_probe_falls_back_to_cpu(caplog: pytest.LogCaptureFixture) -> None:
    """MPS op gaps raise at runtime, not at load — commit only after the probe."""
    with caplog.at_level(logging.INFO, logger=local_inference.__name__):
        device = local_inference._pick_device(_fake_torch(mps_available=True, probe_fails_on="mps"))
    assert device.type == "cpu"
    assert "MPS probe failed" in caplog.text


def test_a_failing_cuda_probe_falls_back_to_cpu() -> None:
    device = local_inference._pick_device(_fake_torch(cuda_available=True, probe_fails_on="cuda"))
    assert device.type == "cpu"


def test_no_gpu_at_all_is_cpu(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=local_inference.__name__):
        device = local_inference._pick_device(_fake_torch())
    assert device.type == "cpu"
    assert "no CUDA or MPS" in caplog.text


def test_a_torch_build_without_an_mps_backend_reads_as_no_mps() -> None:
    """CPU-only and CUDA wheels may lack torch.backends.mps entirely."""
    device = local_inference._pick_device(_fake_torch(mps_backend=False))
    assert device.type == "cpu"


def test_the_pick_is_cached_for_classify() -> None:
    """classify() reads `_device_cache` directly; the pick must land there."""
    local_inference._pick_device(_fake_torch(mps_available=True))
    assert local_inference._device_cache.type == "mps"


# ----- device_description (the status-bar indicator's data source) -------------


def test_no_pick_yet_means_no_description() -> None:
    assert local_inference.device_description() is None


def test_cpu_description(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_inference, "_device_cache", SimpleNamespace(type="cpu"))
    assert local_inference.device_description() == "CPU"


def test_mps_description_names_the_chip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_inference, "_device_cache", SimpleNamespace(type="mps"))
    monkeypatch.setattr(local_inference, "_apple_chip_name", lambda: "Apple M4 Pro")
    assert local_inference.device_description() == "MPS · Apple M4 Pro"


def test_cuda_description_names_the_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_inference, "_device_cache", SimpleNamespace(type="cuda"))
    monkeypatch.setattr(local_inference, "_torch_mod", _fake_torch(cuda_available=True))
    assert local_inference.device_description() == "CUDA · Fake GPU"


def test_cuda_description_survives_a_nameless_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """A get_device_name failure degrades to the bare kind, never raises."""

    def boom(_i: int) -> str:
        raise RuntimeError("no device 0")

    fake = _fake_torch(cuda_available=True)
    fake.cuda.get_device_name = boom
    monkeypatch.setattr(local_inference, "_device_cache", SimpleNamespace(type="cuda"))
    monkeypatch.setattr(local_inference, "_torch_mod", fake)
    assert local_inference.device_description() == "CUDA"
