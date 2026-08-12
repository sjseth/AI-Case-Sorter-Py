"""The (torch version, CUDA index) pair the install dialog uses must resolve.

The GPU build installs pyproject.toml's [ml] pins from a fixed CUDA wheel
index (dialog_install_torch._CUDA_INDEX), and an index only carries the
torch versions actually built for it — cu128 stopped at torch 2.11, so
bumping the pin without moving the index would produce a 404 at install
time, on the user's machine, with nothing catching it earlier (#67). This
resolves each pinned version against the pinned index so that class of bump
fails in CI instead.

Needs the network: skips when download.pytorch.org can't be reached, the
same way the other integration tests skip when their external tool is
missing. A reachable index that lacks the pinned version is a hard fail —
that's the bug this file exists to catch.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
import requests

pytest.importorskip("tkinter")  # dialog_install_torch imports tkinter at module level

from sorter.ui.dialog_install_torch import _CUDA_INDEX, ml_pin_targets

pytestmark = [
    pytest.mark.integration,
]


def _fetch_project_page(name: str) -> str:
    """The PEP 503 listing for one package on the pinned index, unquoted so
    wheel names read as `torch-2.13.0+cu129-...` rather than `%2B`-escaped."""
    url = f"{_CUDA_INDEX}/{name}/"
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        pytest.skip(f"no network access to {url}: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"{url} answered {resp.status_code} — index unavailable, not a pin problem")
    assert resp.status_code == 200, f"{url} answered {resp.status_code} — wrong index URL or package path"
    return unquote(resp.text)


def test_pinned_versions_exist_on_the_cuda_index() -> None:
    targets = ml_pin_targets()
    assert targets is not None, "pyproject.toml's [ml] extra did not resolve"
    for target in targets:
        name, _, version = target.partition("==")
        page = _fetch_project_page(name)
        # A wheel name continues with either `+<local build tag>` or the
        # `-<python tag>` separator, so match both; a bare substring check
        # would also accept e.g. 2.13.0.post1 standing in for 2.13.0.
        assert f"{name}-{version}+" in page or f"{name}-{version}-" in page, (
            f"{name}=={version} has no build on {_CUDA_INDEX} — the [ml] pin in "
            f"pyproject.toml and _CUDA_INDEX in dialog_install_torch.py have "
            f"moved out of step"
        )
