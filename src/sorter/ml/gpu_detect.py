"""Lightweight Nvidia GPU detection without depending on torch.

PyTorch itself isn't installed yet when this matters (we run it from the
Install PyTorch dialog), so we can't use torch.cuda.* — we shell out to
``nvidia-smi`` instead. Returns None when no NVIDIA driver is installed,
when nvidia-smi fails, or when no installed GPU meets the minimum compute
capability for the modern CUDA wheels.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

# RTX 30-series (Ampere) is sm_80+ and is the first generation supported by
# the cu128 wheel index. Older cards (Turing sm_75 and below) would need a
# different wheel set and we don't support that flow.
MIN_CUDA_COMPUTE = 8.0


@dataclass
class GpuInfo:
    name: str
    compute_capability: float

    @property
    def compute_str(self) -> str:
        return f"{self.compute_capability:.1f}"


def detect_supported_nvidia_gpu(*, timeout: float = 5.0) -> GpuInfo | None:
    """Return the most-capable supported GPU if one is present, else None."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,compute_cap",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None

    best: GpuInfo | None = None
    for line in out.stdout.strip().splitlines():
        # "NVIDIA GeForce RTX 3090, 8.6"
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        name, cap_str = parts
        try:
            cap = float(cap_str)
        except ValueError:
            continue
        if cap < MIN_CUDA_COMPUTE:
            continue
        if best is None or cap > best.compute_capability:
            best = GpuInfo(name=name, compute_capability=cap)
    return best
