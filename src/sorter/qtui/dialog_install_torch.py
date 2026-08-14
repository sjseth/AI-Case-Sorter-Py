"""Install PyTorch + torchvision into the running venv.

Every worker — the output pump and the ``nvidia-smi`` probe — only puts a
message on a ``queue.Queue``, and a main-thread ``QTimer`` drains it into the
widgets (CLAUDE.md §8). Same contract the event bus gives the rest of the app,
minus the bus (a dialog has no access to one).

Callers reach this through ``torch_gate`` rather than constructing it; pass
``reason`` to say which action is asking, since "Training needs PyTorch" is the
wrong headline when the user just pressed Start on the Sort page.

GPU detection shells out (up to a 5 s timeout), so the install buttons stay
disabled until the probe lands — what to offer depends on its answer.
"""

from __future__ import annotations

import importlib.util
import queue
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..ml.gpu_detect import GpuInfo, detect_supported_nvidia_gpu
from ..paths import find_uv

# The torch/torchvision pins live in pyproject.toml's [ml] extra and are read
# from there at install time, never re-declared here (CLAUDE.md §8). Only the
# per-OS CUDA index for the GPU build is decided in code: upstream never builds
# Windows cu129 wheels, so the two platforms differ.
# tests/integration/test_torch_wheel_index.py resolves each pin against each.
_CUDA_INDEX_BY_OS = {
    "linux": "https://download.pytorch.org/whl/cu129",
    "win32": "https://download.pytorch.org/whl/cu130",
}
_CUDA_INDEX = _CUDA_INDEX_BY_OS.get(sys.platform, _CUDA_INDEX_BY_OS["linux"])


def ml_pin_targets() -> tuple[str, ...] | None:
    """The exact requirement strings pyproject.toml's [ml] extra pins.

    ``None`` on any failure — an unreadable pyproject installs nothing rather
    than a guess. The ``project.name`` guard matters: ``parents[3]`` is
    whatever directory the tree happens to sit in.
    """
    path = Path(__file__).resolve().parents[3] / "pyproject.toml"
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        if data["project"]["name"] != "ai-case-sorter":
            return None
        targets = data["project"]["optional-dependencies"]["ml"]
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, KeyError, TypeError):
        return None
    if not isinstance(targets, list) or not targets or not all(isinstance(t, str) for t in targets):
        return None
    return tuple(targets)


# Shown when the caller doesn't name the action. Deliberately generic: this
# dialog fronts inference as well as training.
DEFAULT_REASON = "This model needs PyTorch"

BODY_TEXT = (
    "Running or training a model on this computer requires PyTorch and "
    "torchvision. The download is large and only happens once. "
    "(Models classified by an AI Config server don't need it.)"
)
NO_GPU_TEXT = (
    "No supported NVIDIA GPU detected — the CPU build will be installed. "
    "(For NVIDIA GPUs requires Ampere or newer; compute capability ≥ 8.0.)"
)
PROBING_TEXT = "Checking for a supported GPU…"
NO_INSTALLER_TEXT = (
    "Could not find uv or pip. uv should have been installed by bootstrap.py "
    "on first launch — try restarting the app via start.sh/start.bat, or "
    "install uv yourself from https://docs.astral.sh/uv/.\n"
)
NO_PINS_TEXT = (
    "Could not read the pinned PyTorch version from pyproject.toml "
    "(expected next to bootstrap.py, at the top of the app folder). "
    "The install can't proceed without it — restart via start.sh/start.bat, "
    "or reinstall the app if the file is really gone.\n"
)

DRAIN_MS = 50
# Grace given to a cancelled install between SIGTERM and SIGKILL.
TERMINATE_GRACE_S = 5.0


def build_install_command(*, use_gpu: bool, uv: str | None, pip_available: bool) -> list[str] | None:
    """The argv for one install, or None when it can't be built.

    Two ways there is no command: the pins are unreadable, or neither installer
    is available. uv first, because a uv-managed venv (the launcher's default)
    has no pip in it at all. But a plain ``python -m venv`` + ``pip install -e .``
    checkout is a documented way to run this app, and there uv may legitimately
    be absent while pip is right there — so fall back rather than refusing to
    install.
    """
    targets = ml_pin_targets()
    if targets is None:
        return None
    if uv is not None:
        cmd = [uv, "pip", "install", "--python", sys.executable, *targets]
    elif pip_available:
        cmd = [sys.executable, "-u", "-m", "pip", "install", *targets]
    else:
        return None
    if use_gpu:
        cmd.extend(["--index-url", _CUDA_INDEX])
    return cmd


def _restyle(widget: QWidget, object_name: str) -> None:
    """Swap a widget's QSS selector after the stylesheet was applied."""
    widget.setObjectName(object_name)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class TorchInstallDialog(QDialog):
    """Non-blocking modal: opened with ``open()``, never ``exec()``.

    ``build_command`` and ``detect`` are the test seams — replacing
    ``build_command`` runs an arbitrary process through the same streaming
    path without installing anything.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_success: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        reason: str | None = None,
        detect: Callable[[], GpuInfo | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Install PyTorch")
        self.setMinimumSize(640, 460)
        self.resize(720, 520)

        self._on_success = on_success
        self._on_cancel = on_cancel
        self._reason = reason or DEFAULT_REASON
        self._detect = detect or detect_supported_nvidia_gpu
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._proc: subprocess.Popen[str] | None = None
        self._installing = False
        self._closed = False
        self.gpu: GpuInfo | None = None
        self.gpu_known = False
        # Instance attribute so a test can point the install at its own process.
        self.build_command: Callable[[bool], list[str] | None] = self._default_command

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(DRAIN_MS)
        threading.Thread(target=self._probe_gpu, daemon=True).start()

    # ----- UI build -----------------------------------------------------------

    def _build_ui(self) -> None:
        column = QVBoxLayout(self)

        header = QLabel(self._reason, self)
        header_font = QFont(header.font())
        header_font.setBold(True)
        header.setFont(header_font)
        header.setWordWrap(True)
        column.addWidget(header)

        body = QLabel(BODY_TEXT, self)
        body.setObjectName("dialogHint")
        body.setWordWrap(True)
        column.addWidget(body)

        self.gpu_label = QLabel(PROBING_TEXT, self)
        self.gpu_label.setObjectName("mutedLabel")
        self.gpu_label.setWordWrap(True)
        column.addWidget(self.gpu_label)

        self.console = QPlainTextEdit(self)
        self.console.setObjectName("installConsole")
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Enough scrollback to see what failed, bounded so a long install
        # can't grow the document without limit.
        self.console.setMaximumBlockCount(5000)
        self.console.setFont(QFont("Monospace"))
        column.addWidget(self.console, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.gpu_button = QPushButton("Install with GPU support", self)
        self.gpu_button.setVisible(False)
        self.gpu_button.clicked.connect(lambda: self.start_install(use_gpu=True))
        row.addWidget(self.gpu_button)

        self.cpu_button = QPushButton("Install PyTorch", self)
        self.cpu_button.clicked.connect(lambda: self.start_install(use_gpu=False))
        row.addWidget(self.cpu_button)

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.cancel)
        row.addWidget(self.cancel_button)
        column.addLayout(row)

        # Nothing to install until the probe says which builds to offer.
        self.cpu_button.setEnabled(False)

    # ----- GPU probe ----------------------------------------------------------

    def _probe_gpu(self) -> None:
        """Worker: ``nvidia-smi`` shells out, so it never runs on the UI thread."""
        try:
            gpu = self._detect()
        except Exception:
            gpu = None
        self._events.put(("gpu", gpu))

    def _apply_gpu(self, gpu: GpuInfo | None) -> None:
        self.gpu = gpu
        self.gpu_known = True
        if gpu is not None:
            self.gpu_label.setText(
                f"Detected supported GPU: {gpu.name} (compute capability {gpu.compute_str}).\n"
                "Pick GPU build for fast training, or CPU only if you'd rather skip the "
                "larger download / CUDA driver requirements."
            )
            _restyle(self.gpu_label, "aiResultLabel")
            self.gpu_button.setVisible(True)
            _restyle(self.gpu_button, "action")
            self.cpu_button.setText("CPU only")
        else:
            self.gpu_label.setText(NO_GPU_TEXT)
            _restyle(self.cpu_button, "action")
        self._set_buttons_enabled(True)

    # ----- install flow -------------------------------------------------------

    def _default_command(self, use_gpu: bool) -> list[str] | None:
        """The real argv — and, when there is none, the console line saying why.

        The reason lives here rather than in ``start_install`` because only the
        real builder knows which precondition failed; a replaced
        ``build_command`` speaks for itself.
        """
        if ml_pin_targets() is None:
            self._append(NO_PINS_TEXT)
            return None
        uv = find_uv()
        pip_available = importlib.util.find_spec("pip") is not None
        if uv is None:
            self._append(
                "uv not found; falling back to pip in the running interpreter.\n"
                if pip_available
                else NO_INSTALLER_TEXT
            )
        return build_install_command(use_gpu=use_gpu, uv=uv, pip_available=pip_available)

    def start_install(self, *, use_gpu: bool) -> None:
        if self._installing:
            return
        cmd = self.build_command(use_gpu)
        if cmd is None:
            return  # the builder already put the reason on the console
        self._installing = True
        self._set_buttons_enabled(False)
        (self.gpu_button if (use_gpu and self.gpu is not None) else self.cpu_button).setText("Installing…")
        self._append("$ " + " ".join(cmd) + "\n")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._append(f"Failed to spawn {cmd[0]}: {exc}\n")
            self._finish(success=False)
            return
        self._proc = proc
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        """Worker: reads the child's output; the queue does the marshalling."""
        rc = -1
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self._events.put(("line", line))
            rc = proc.wait()
        except Exception as exc:
            self._events.put(("line", f"\n[install] {exc}\n"))
        self._events.put(("exit", rc))

    def _drain(self) -> None:
        """Main thread: the only place worker output reaches a widget."""
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                return
            if kind == "gpu":
                self._apply_gpu(payload)
            elif kind == "line":
                self._append(payload)
            elif kind == "exit":
                self._finish(success=payload == 0)

    def _finish(self, *, success: bool) -> None:
        self._installing = False
        self._proc = None
        if not success:
            self._append("\nInstall failed. See output above.\n")
            self._reset_buttons()
            return
        self._append("\nInstall complete.\n")
        self._timer.stop()
        self._closed = True
        self.accept()
        # After accept(), so the re-entered action doesn't run behind a dialog
        # that is on its way out. Safe to call inline: the gate opens this with
        # open(), so there is no nested event loop to re-enter.
        if self._on_success is not None:
            self._on_success()

    def cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        was_installing = self._installing
        if was_installing and proc is not None and proc.poll() is None:
            self._append("\nCancelled.\n")
            self._terminate(proc)
        self._installing = False
        self._timer.stop()
        # A cancel *during* an install is a cancel of the install, not of the
        # action that asked for it, so the caller isn't re-entered.
        if self._on_cancel is not None and not was_installing:
            self._on_cancel()
        super().reject()

    def reject(self) -> None:
        """Esc and the window's close button both land here."""
        self.cancel()

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        try:
            proc.terminate()
        except Exception:
            return

        def _escalate() -> None:
            try:
                proc.wait(timeout=TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

        threading.Thread(target=_escalate, daemon=True).start()

    # ----- widget helpers -----------------------------------------------------

    def _append(self, text: str) -> None:
        self.console.appendPlainText(text.rstrip("\n"))
        bar = self.console.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self.cpu_button.setEnabled(enabled)
        self.gpu_button.setEnabled(enabled)

    def _reset_buttons(self) -> None:
        self.cpu_button.setText("CPU only" if self.gpu is not None else "Install PyTorch")
        self.gpu_button.setText("Install with GPU support")
        self._set_buttons_enabled(self.gpu_known)
