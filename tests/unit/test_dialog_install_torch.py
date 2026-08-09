"""dialog_install_torch installs via `uv pip install`, not `pip` directly --
a uv-managed venv (bootstrap.py) doesn't ship pip by default. Needs a Tk
display; skips cleanly without one."""

from __future__ import annotations

import importlib.util
import subprocess
import threading

import pytest

pytest.importorskip("tkinter")

import tkinter as tk  # noqa: E402

from sorter.ui import dialog_install_torch  # noqa: E402
from sorter.ui.dialog_install_torch import TorchInstallDialog  # noqa: E402


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture(autouse=True)
def _no_gpu(monkeypatch):
    monkeypatch.setattr(dialog_install_torch, "detect_supported_nvidia_gpu", lambda: None)


def test_install_uses_uv_pip_not_bare_pip(root, monkeypatch) -> None:
    """Only checking the constructed command here -- the async completion
    path (_pump -> self.after -> _finish) needs a real mainloop, which this
    synchronous test doesn't run, so the background thread is stubbed out
    rather than actually started."""
    monkeypatch.setattr(dialog_install_torch, "find_uv", lambda: "/fake/.uv/bin/uv")
    captured = {}

    class _FakeProc:
        stdout = iter([])

        def wait(self):
            return 0

    def fake_popen(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    class _NoOpThread:
        def __init__(self, target=None, daemon=None):
            pass

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", _NoOpThread)

    dlg = TorchInstallDialog(root)
    dlg._start_install(use_gpu=False)

    cmd = captured["cmd"]
    assert cmd[0] == "/fake/.uv/bin/uv"
    assert cmd[1:3] == ["pip", "install"]
    assert "--python" in cmd
    assert "-m" not in cmd
    dlg.destroy()


def test_missing_uv_and_pip_fails_without_spawning_a_process(root, monkeypatch) -> None:
    monkeypatch.setattr(dialog_install_torch, "find_uv", lambda: None)
    # pip must be masked explicitly: the dialog legitimately falls back to
    # `python -m pip` when it is importable, and since sqlite-utils (a core
    # dependency) depends on pip, the app venv now always carries it.
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "pip" else real_find_spec(name, *a, **k),
    )
    spawn_attempted = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: spawn_attempted.append(1))

    dlg = TorchInstallDialog(root)
    dlg._start_install(use_gpu=False)

    assert not spawn_attempted
    assert dlg._installing is False
    dlg.destroy()
