"""Every (torch pin, CUDA index, platform) the install dialog can use must resolve.

The GPU build installs pyproject.toml's [ml] pins from a per-OS CUDA wheel
index (dialog_install_torch._CUDA_INDEX_BY_OS), and an index only carries
the torch versions actually built for it — cu128 stopped at torch 2.11, and
CUDA 12.9 wheels were never built for Windows at all — so bumping the pin
without checking the indexes would produce "no matching distribution" at
install time, on the user's machine, with nothing catching it earlier (#67).
This resolves each pinned version against each index and requires a wheel
for that index's platform, so that class of bump fails in CI instead.

Needs the network: skips when download.pytorch.org can't be reached or is
rate-limiting, the same way the other integration tests skip when their
external tool is missing. A reachable index that lacks the pinned version
for its platform is a hard fail — that's the bug this file exists to catch.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

import pytest
import requests

from sorter.ui.dialog_install_torch import _CUDA_INDEX_BY_OS, ml_pin_targets

pytestmark = [
    pytest.mark.integration,
]

# The wheel platform tag that proves "this OS can install from its index".
# The GPU flow only ever runs where nvidia-smi exists, i.e. these two.
_PLATFORM_TAG = {"linux": "linux", "win32": "win_amd64"}


def _fetch_project_page(index: str, name: str) -> str:
    """The PEP 503 listing for one package on one index, unquoted so wheel
    names read as `torch-2.13.0+cu129-...` rather than `%2B`-escaped."""
    url = f"{index}/{name}/"
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        pytest.skip(f"no network access to {url}: {exc}")
    if resp.status_code in (403, 429) or resp.status_code >= 500:
        # CloudFront rate limits and interception proxies answer with these;
        # neither says anything about the pins. A 404 stays a hard fail
        # below — that IS the wrong-index / wrong-package signal.
        pytest.skip(f"{url} answered {resp.status_code} — index unavailable, not a pin problem")
    assert resp.status_code == 200, f"{url} answered {resp.status_code} — wrong index URL or package path"
    return unquote(resp.text)


@pytest.mark.parametrize("os_key", sorted(_CUDA_INDEX_BY_OS))
def test_pinned_versions_exist_on_each_cuda_index(os_key: str) -> None:
    targets = ml_pin_targets()
    assert targets is not None, "pyproject.toml's [ml] extra did not resolve"
    index = _CUDA_INDEX_BY_OS[os_key]
    tag = _PLATFORM_TAG[os_key]
    for target in targets:
        name, _, version = target.partition("==")
        page = _fetch_project_page(index, name)
        # Wheel filenames for the pinned version, whatever local build tag
        # they carry. Presence of *a* build isn't enough — an index can list
        # Linux wheels for a version it never built on Windows (cu129 does
        # exactly that), so the platform this index serves must have one.
        wheels = re.findall(rf"{re.escape(name)}-{re.escape(version)}[+-][^\s\"'<>]*?\.whl", page)
        assert any(tag in wheel for wheel in wheels), (
            f"{name}=={version} has no {tag} build on {index} — the [ml] pin in "
            f"pyproject.toml and _CUDA_INDEX_BY_OS in dialog_install_torch.py "
            f"have moved out of step"
        )
