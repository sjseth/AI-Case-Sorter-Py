"""dialog_install_torch installs via `uv pip install`, not `pip` directly --
a uv-managed venv (bootstrap.py) doesn't ship pip by default. Needs a Tk
display; skips cleanly without one."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import threading
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("tkinter")

import tkinter as tk

from sorter.ui import dialog_install_torch
from sorter.ui.dialog_install_torch import TorchInstallDialog

# Derived from this test file's own location, deliberately NOT from
# dialog_install_torch.__file__ — an independent path to the same
# pyproject.toml is what lets these tests catch a wrong-depth resolve
# in ml_pin_targets() instead of repeating it.
REPO_ROOT = Path(__file__).resolve().parents[3]


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


def test_pin_targets_resolve_from_pyproject() -> None:
    """The runtime install targets ARE pyproject.toml's [ml] extra — one
    source of truth (#67). A wrong-depth path resolve or a parse regression
    in ml_pin_targets() shows up here as a mismatch against the real file."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        expected = tuple(tomllib.load(fh)["project"]["optional-dependencies"]["ml"])
    assert dialog_install_torch.ml_pin_targets() == expected


def test_ml_extra_is_exact_torch_pins() -> None:
    """The [ml] entries go verbatim onto a pip/uv command line, so they must
    stay exact `==` pins: a floating spec would reintroduce the
    latest-at-install-time moving target the pins exist to prevent."""
    targets = dialog_install_torch.ml_pin_targets()
    assert targets is not None
    assert [t.partition("==")[0] for t in targets] == ["torch", "torchvision"]
    for target in targets:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", target), target


def test_pin_targets_none_when_pyproject_missing_or_foreign(tmp_path, monkeypatch) -> None:
    fake_module = tmp_path / "src" / "sorter" / "ui" / "dialog_install_torch.py"
    fake_module.parent.mkdir(parents=True)
    monkeypatch.setattr(dialog_install_torch, "__file__", str(fake_module))

    # No pyproject.toml at the tree root at all.
    assert dialog_install_torch.ml_pin_targets() is None

    # A pyproject.toml belonging to some other project entirely.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "someone-elses-app"\n', encoding="utf-8")
    assert dialog_install_torch.ml_pin_targets() is None

    # The real shape resolves.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ai-case-sorter"\n'
        '[project.optional-dependencies]\nml = ["torch==9.9.9", "torchvision==9.9.9"]\n',
        encoding="utf-8",
    )
    assert dialog_install_torch.ml_pin_targets() == ("torch==9.9.9", "torchvision==9.9.9")


def test_install_without_pins_fails_without_spawning_a_process(root, monkeypatch) -> None:
    """An unreadable pyproject.toml must surface as a console message, not as
    an install of some guessed version."""
    monkeypatch.setattr(dialog_install_torch, "ml_pin_targets", lambda: None)
    spawn_attempted = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: spawn_attempted.append(1))

    dlg = TorchInstallDialog(root)
    dlg._start_install(use_gpu=False)

    assert not spawn_attempted
    assert dlg._installing is False
    assert "pyproject.toml" in dlg.console.get("1.0", tk.END)
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
