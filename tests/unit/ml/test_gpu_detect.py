"""Tests for the nvidia-smi-backed GPU detection helper."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from sorter.ml import gpu_detect


def _fake_run(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["nvidia-smi"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_picks_highest_supported_capability() -> None:
    out = (
        "NVIDIA GeForce GTX 1080, 6.1\n"  # below 8.0 → ignored
        "NVIDIA GeForce RTX 3090, 8.6\n"  # supported
        "NVIDIA GeForce RTX 4090, 8.9\n"  # supported, highest
    )
    with patch.object(subprocess, "run", return_value=_fake_run(out)):
        info = gpu_detect.detect_supported_nvidia_gpu()
    assert info is not None
    assert info.name == "NVIDIA GeForce RTX 4090"
    assert info.compute_capability == pytest.approx(8.9)
    assert info.compute_str == "8.9"


def test_returns_none_when_only_unsupported_gpus() -> None:
    out = "NVIDIA GeForce GTX 1660 Ti, 7.5\nNVIDIA Quadro RTX 4000, 7.5\n"
    with patch.object(subprocess, "run", return_value=_fake_run(out)):
        info = gpu_detect.detect_supported_nvidia_gpu()
    assert info is None


def test_returns_none_when_nvidia_smi_missing() -> None:
    with patch.object(subprocess, "run", side_effect=FileNotFoundError()):
        assert gpu_detect.detect_supported_nvidia_gpu() is None


def test_returns_none_when_nvidia_smi_fails() -> None:
    with patch.object(subprocess, "run", return_value=_fake_run("", returncode=1)):
        assert gpu_detect.detect_supported_nvidia_gpu() is None


def test_returns_none_on_timeout() -> None:
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=5)):
        assert gpu_detect.detect_supported_nvidia_gpu() is None
