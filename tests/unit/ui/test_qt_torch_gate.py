"""The Qt PyTorch install gate: who gets prompted, what gets installed, and how
the subprocess output reaches the console.

Three properties are pinned here, and each has cost real breakage before:

* an AI Config user is never prompted (the gate short-circuits on
  ``local_inference.is_installed`` — a ``find_spec`` probe, never an import);
* every argv the dialog can build is assembled from pyproject's ``[ml]`` extra,
  read at install time (CLAUDE.md §8), so a version bump can't drift the
  runtime install away from the declared one;
* worker output crosses to the widgets through a queue drained by a main-thread
  timer, never from the worker thread itself (CLAUDE.md §8).

No real install and no real modal runs here: ``build_command`` points the
streaming path at a throwaway ``python -c`` process, and nothing is ``exec()``ed.
"""

from __future__ import annotations

import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from sorter import paths
from sorter.ml import local_inference
from sorter.ml.gpu_detect import GpuInfo
from sorter.ui import torch_gate
from sorter.ui.dialog_install_torch import (
    _CUDA_INDEX,
    _CUDA_INDEX_BY_OS,
    TorchInstallDialog,
    build_install_command,
    ml_pin_targets,
)
from sorter.ui.torch_gate import TorchGate

# A loop that keeps printing until it is killed — the stand-in for a long pip.
LOOP_SCRIPT = "import time\nfor i in range(10000):\n    print('line', i, flush=True)\n    time.sleep(0.01)\n"


def pump_until(qapp: Any, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Bounded poll on the Qt event loop — the stand-in for a mainloop.

    The drain timer only fires while events are processed, so a test that wants
    worker output must pump for it. No unconditional sleeps: the loop exits the
    moment the predicate holds.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


class FakeDialog:
    """Records what the gate asked for; opens nothing."""

    instances: list[FakeDialog] = []

    def __init__(self, parent: Any = None, *, on_success=None, on_cancel=None, reason=None) -> None:
        self.parent = parent
        self.on_success = on_success
        self.on_cancel = on_cancel
        self.reason = reason
        self.opened = False
        FakeDialog.instances.append(self)

    def open(self) -> None:
        self.opened = True


@pytest.fixture
def fake_dialogs():
    FakeDialog.instances = []
    yield FakeDialog.instances
    FakeDialog.instances = []


@pytest.fixture
def no_torch(monkeypatch):
    monkeypatch.setattr(local_inference, "is_installed", lambda: False)


@pytest.fixture
def dialog_factory(qapp):
    """Build dialogs with a stubbed GPU probe; close them all at teardown."""
    dialogs: list[TorchInstallDialog] = []

    def _make(*, gpu: GpuInfo | None = None, **kwargs: Any) -> TorchInstallDialog:
        dialog = TorchInstallDialog(detect=lambda: gpu, **kwargs)
        dialogs.append(dialog)
        return dialog

    yield _make
    for dialog in dialogs:
        dialog.cancel()
        dialog.deleteLater()
    qapp.processEvents()


# ----- the gate --------------------------------------------------------------


def test_installed_torch_short_circuits_without_a_dialog(monkeypatch, fake_dialogs) -> None:
    monkeypatch.setattr(local_inference, "is_installed", lambda: True)
    calls: list[int] = []

    gate = TorchGate(None, dialog_factory=FakeDialog)

    assert gate(lambda: calls.append(1), reason="Sorting needs PyTorch") is True
    assert fake_dialogs == []
    # The gate itself never runs the action; its True says "carry on".
    assert calls == []


def test_missing_torch_opens_the_dialog_and_returns_false(no_torch, fake_dialogs) -> None:
    gate = TorchGate("parent-window", dialog_factory=FakeDialog)
    proceed = lambda: None  # noqa: E731

    assert gate(proceed, reason="Sorting needs PyTorch") is False
    (dialog,) = fake_dialogs
    assert dialog.opened is True
    assert dialog.parent == "parent-window"
    assert dialog.on_success is proceed
    assert dialog.reason == "Sorting needs PyTorch"


def test_success_callback_is_the_gated_action(no_torch, fake_dialogs) -> None:
    calls: list[int] = []
    gate = TorchGate(None, dialog_factory=FakeDialog)
    gate(lambda: calls.append(1))

    fake_dialogs[0].on_success()
    assert calls == [1]


def test_offer_remembers_a_decline_for_the_session(no_torch, fake_dialogs) -> None:
    """Train's Feed offers rather than gates, so "no thanks" must stick."""
    resumed: list[str] = []
    gate = TorchGate(None, dialog_factory=FakeDialog)

    assert gate.offer(lambda: resumed.append("feed"), key="predictions") is False
    assert len(fake_dialogs) == 1
    # Declining re-enters the action — that is what makes the feed still happen.
    fake_dialogs[0].on_cancel()
    assert resumed == ["feed"]

    assert gate.offer(lambda: resumed.append("feed"), key="predictions") is True
    assert len(fake_dialogs) == 1


def test_offer_is_remembered_per_gate_not_globally(no_torch, fake_dialogs) -> None:
    TorchGate(None, dialog_factory=FakeDialog).offer(lambda: None)
    TorchGate(None, dialog_factory=FakeDialog).offer(lambda: None)
    assert len(fake_dialogs) == 2


def test_hard_gate_asks_again_after_a_decline(no_torch, fake_dialogs) -> None:
    """Start can't run without torch, so it re-asks every press."""
    gate = TorchGate(None, dialog_factory=FakeDialog)
    gate(lambda: None)
    fake_dialogs[0].on_cancel = None
    gate(lambda: None)
    assert len(fake_dialogs) == 2


def test_module_level_ensure_torch_is_the_documented_front_door(no_torch, monkeypatch, fake_dialogs) -> None:
    monkeypatch.setattr(torch_gate, "TorchInstallDialog", FakeDialog)
    assert torch_gate.ensure_torch("win", lambda: None, reason="Training needs PyTorch") is False
    assert fake_dialogs[0].reason == "Training needs PyTorch"


# ----- the install command ---------------------------------------------------


def _pyproject_ml_pins() -> list[str]:
    with (paths.app_root() / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return list(data["project"]["optional-dependencies"]["ml"])


def test_the_pin_reader_returns_the_pyproject_ml_extra() -> None:
    """CLAUDE.md §8: the [ml] extra is the only place the torch version lives."""
    targets = ml_pin_targets()
    assert targets is not None
    assert list(targets) == _pyproject_ml_pins()


def test_the_cuda_index_table_covers_every_platform_the_gpu_flow_reaches() -> None:
    """The GPU build only ever runs where nvidia-smi does, i.e. these two.

    ``tests/integration/test_torch_wheel_index.py`` resolves each pin against
    each index for real; this is the offline half — that the table has an
    entry per platform and that the module-level pick comes out of it.
    """
    assert set(_CUDA_INDEX_BY_OS) == {"linux", "win32"}
    assert _CUDA_INDEX in _CUDA_INDEX_BY_OS.values()


def test_cpu_command_prefers_uv_over_bare_pip() -> None:
    cmd = build_install_command(use_gpu=False, uv="/fake/.uv/bin/uv", pip_available=True)
    assert cmd == ["/fake/.uv/bin/uv", "pip", "install", "--python", sys.executable, *_pyproject_ml_pins()]


def test_cuda_command_appends_the_wheel_index() -> None:
    cmd = build_install_command(use_gpu=True, uv="/fake/.uv/bin/uv", pip_available=True)
    assert cmd == [
        "/fake/.uv/bin/uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        *_pyproject_ml_pins(),
        "--index-url",
        _CUDA_INDEX,
    ]


def test_falls_back_to_pip_when_uv_is_absent() -> None:
    cmd = build_install_command(use_gpu=False, uv=None, pip_available=True)
    assert cmd == [sys.executable, "-u", "-m", "pip", "install", *_pyproject_ml_pins()]


def test_no_installer_at_all_builds_no_command() -> None:
    assert build_install_command(use_gpu=False, uv=None, pip_available=False) is None


def test_an_unreadable_pin_builds_no_command(monkeypatch) -> None:
    """A tree whose pyproject.toml is gone installs nothing rather than a guess."""
    from sorter.ui import dialog_install_torch as qt_dialog

    monkeypatch.setattr(qt_dialog, "ml_pin_targets", lambda: None)
    assert build_install_command(use_gpu=False, uv="/fake/.uv/bin/uv", pip_available=True) is None


def test_the_pin_reader_rejects_another_projects_pyproject(monkeypatch, tmp_path: Path) -> None:
    """The guard is `project.name` — parents[3] could be anything on disk."""
    from sorter.ui import dialog_install_torch as qt_dialog

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "something-else"\n[project.optional-dependencies]\nml = ["torch==1.0.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(qt_dialog, "__file__", str(tmp_path / "src" / "sorter" / "ui" / "dialog_install_torch.py"))
    assert qt_dialog.ml_pin_targets() is None


# ----- the dialog ------------------------------------------------------------


def test_gpu_probe_runs_off_the_main_thread_and_lands_through_the_queue(qapp, dialog_factory) -> None:
    gpu = GpuInfo(name="NVIDIA GeForce RTX 4090", compute_capability=8.9)
    dialog = dialog_factory(gpu=gpu)

    # Nothing installable until the probe answers: what to offer depends on it.
    assert dialog.gpu_known is False
    assert dialog.cpu_button.isEnabled() is False

    assert pump_until(qapp, lambda: dialog.gpu_known)
    assert dialog.gpu == gpu
    assert "RTX 4090" in dialog.gpu_label.text()
    assert "8.9" in dialog.gpu_label.text()
    assert dialog.gpu_button.isVisible() is False  # never shown; visibility follows the parent
    assert dialog.gpu_button.isEnabled() is True
    assert dialog.cpu_button.text() == "CPU only"


def test_without_a_gpu_only_the_cpu_build_is_offered(qapp, dialog_factory) -> None:
    dialog = dialog_factory(gpu=None)
    assert pump_until(qapp, lambda: dialog.gpu_known)
    assert dialog.cpu_button.text() == "Install PyTorch"
    assert dialog.cpu_button.isEnabled() is True
    assert dialog.gpu_button.isVisibleTo(dialog) is False


def test_gpu_button_asks_for_the_cuda_build(qapp, dialog_factory) -> None:
    dialog = dialog_factory(gpu=GpuInfo(name="RTX 3090", compute_capability=8.6))
    assert pump_until(qapp, lambda: dialog.gpu_known)
    asked: list[bool] = []
    dialog.build_command = lambda use_gpu: asked.append(use_gpu) or None

    dialog.gpu_button.click()
    dialog.cpu_button.click()
    assert asked == [True, False]


def test_output_streams_into_the_console_and_cancel_terminates(qapp, dialog_factory) -> None:
    dialog = dialog_factory(gpu=None)
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.build_command = lambda use_gpu: [sys.executable, "-u", "-c", LOOP_SCRIPT]

    dialog.start_install(use_gpu=False)
    proc = dialog._proc
    assert proc is not None

    # Lines only reach the widget through the queue + drain timer, so pumping
    # the event loop is what makes them appear.
    assert pump_until(qapp, lambda: "line 1" in dialog.console.toPlainText())
    assert dialog._installing is True

    dialog.cancel()
    assert pump_until(qapp, lambda: proc.poll() is not None)
    assert proc.poll() is not None
    assert dialog._installing is False


def test_a_successful_install_re_enters_the_caller_exactly_once(qapp, dialog_factory) -> None:
    calls: list[int] = []
    dialog = dialog_factory(gpu=None, on_success=lambda: calls.append(1))
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.build_command = lambda use_gpu: [sys.executable, "-c", "print('ok')"]

    dialog.start_install(use_gpu=False)
    assert pump_until(qapp, lambda: calls == [1])
    # Keep pumping: a second call would mean the exit event was handled twice.
    pump_until(qapp, lambda: False, timeout_s=0.2)
    assert calls == [1]
    assert "ok" in dialog.console.toPlainText()
    assert dialog.result() == int(dialog.DialogCode.Accepted)


def test_a_failed_install_leaves_the_dialog_open(qapp, dialog_factory) -> None:
    calls: list[int] = []
    dialog = dialog_factory(gpu=None, on_success=lambda: calls.append(1))
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.build_command = lambda use_gpu: [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"]

    dialog.start_install(use_gpu=False)
    assert pump_until(qapp, lambda: "Install failed" in dialog.console.toPlainText())
    assert calls == []
    assert "boom" in dialog.console.toPlainText()
    assert dialog._installing is False
    # Retryable: the buttons come back rather than the dialog closing.
    assert dialog.cpu_button.isEnabled() is True
    assert dialog.cpu_button.text() == "Install PyTorch"


def test_cancel_before_installing_reports_the_decline(qapp, dialog_factory) -> None:
    declined: list[int] = []
    dialog = dialog_factory(gpu=None, on_cancel=lambda: declined.append(1))
    assert pump_until(qapp, lambda: dialog.gpu_known)

    dialog.cancel()
    assert declined == [1]
    # Idempotent: Esc after the button must not double-report.
    dialog.cancel()
    assert declined == [1]


def test_cancel_during_an_install_is_not_a_decline_of_the_action(qapp, dialog_factory) -> None:
    declined: list[int] = []
    dialog = dialog_factory(gpu=None, on_cancel=lambda: declined.append(1))
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.build_command = lambda use_gpu: [sys.executable, "-u", "-c", LOOP_SCRIPT]

    dialog.start_install(use_gpu=False)
    proc = dialog._proc
    assert proc is not None
    dialog.cancel()
    assert pump_until(qapp, lambda: proc.poll() is not None)
    assert declined == []


def test_no_uv_and_no_pip_reports_instead_of_spawning(qapp, dialog_factory, monkeypatch) -> None:
    import importlib.util

    from sorter.ui import dialog_install_torch as qt_dialog

    monkeypatch.setattr(qt_dialog, "find_uv", lambda: None)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "pip" else real_find_spec(name, *a, **k),
    )
    spawned: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))

    dialog = dialog_factory(gpu=None)
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.start_install(use_gpu=False)

    assert spawned == []
    assert dialog._installing is False
    assert "Could not find uv or pip" in dialog.console.toPlainText()


def test_an_unreadable_pin_reports_instead_of_spawning(qapp, dialog_factory, monkeypatch) -> None:
    from sorter.ui import dialog_install_torch as qt_dialog

    monkeypatch.setattr(qt_dialog, "ml_pin_targets", lambda: None)
    spawned: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))

    dialog = dialog_factory(gpu=None)
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.start_install(use_gpu=False)

    assert spawned == []
    assert dialog._installing is False
    assert "Could not read the pinned PyTorch version" in dialog.console.toPlainText()


def test_a_command_that_cannot_spawn_fails_without_hanging(qapp, dialog_factory, tmp_path: Path) -> None:
    dialog = dialog_factory(gpu=None)
    assert pump_until(qapp, lambda: dialog.gpu_known)
    dialog.build_command = lambda use_gpu: [str(tmp_path / "definitely-not-here")]

    dialog.start_install(use_gpu=False)
    assert "Failed to spawn" in dialog.console.toPlainText()
    assert "Install failed" in dialog.console.toPlainText()
    assert dialog._installing is False
