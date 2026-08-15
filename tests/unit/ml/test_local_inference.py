"""Unit tests for `_pick_device` — CUDA, then MPS, then CPU (#36).

torch is the optional `[ml]` extra and genuinely absent from this dev/CI
environment, so every branch is exercised through a fake torch module. What
these pin: the probe order, the probe-then-commit fallback on both GPU
branches (an MPS op gap raises at runtime, so the probe is load-bearing),
and that a torch build without an MPS backend reads as "no MPS" rather than
raising.
"""

from __future__ import annotations

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


def test_mps_is_picked_when_cuda_is_absent(capsys: pytest.CaptureFixture[str]) -> None:
    device = local_inference._pick_device(_fake_torch(mps_available=True))
    assert device.type == "mps"
    assert "[device] MPS ok" in capsys.readouterr().err


def test_a_failing_mps_probe_falls_back_to_cpu(capsys: pytest.CaptureFixture[str]) -> None:
    """MPS op gaps raise at runtime, not at load — commit only after the probe."""
    device = local_inference._pick_device(_fake_torch(mps_available=True, probe_fails_on="mps"))
    assert device.type == "cpu"
    assert "MPS probe failed" in capsys.readouterr().err


def test_a_failing_cuda_probe_falls_back_to_cpu() -> None:
    device = local_inference._pick_device(_fake_torch(cuda_available=True, probe_fails_on="cuda"))
    assert device.type == "cpu"


def test_no_gpu_at_all_is_cpu(capsys: pytest.CaptureFixture[str]) -> None:
    device = local_inference._pick_device(_fake_torch())
    assert device.type == "cpu"
    assert "no CUDA or MPS" in capsys.readouterr().err


def test_a_torch_build_without_an_mps_backend_reads_as_no_mps() -> None:
    """CPU-only and CUDA wheels may lack torch.backends.mps entirely."""
    device = local_inference._pick_device(_fake_torch(mps_backend=False))
    assert device.type == "cpu"


def test_the_pick_is_cached_for_classify() -> None:
    """classify() reads `_device_cache` directly; the pick must land there."""
    local_inference._pick_device(_fake_torch(mps_available=True))
    assert local_inference._device_cache.type == "mps"
