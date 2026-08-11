"""Modal that installs PyTorch + torchvision into the active venv.

Every local-model action is gated on this — training *and* inference — so
AI-Config-only users never pay the ~2 GB torch install cost. Callers reach it
through `torch_gate.ensure_torch` rather than constructing it directly; pass
`reason` to say which action is asking, since "Training needs PyTorch" is the
wrong headline when the user just pressed Start on the Run tab.

On open we detect a supported Nvidia GPU (compute capability ≥ 8.0). If one is
present, the user gets to pick between the GPU build (CUDA 12.8 wheels) and the
CPU build; otherwise only the CPU build is offered.

`uv pip install --python <this interpreter>` runs in a subprocess and streams
its output to the dialog's console. Prefers uv over `python -m pip` because a
uv-managed venv (see bootstrap.py) doesn't ship pip by default -- `--python`
targets the running venv explicitly regardless of how it was created. Falls
back to `python -m pip` when uv isn't installed, which is the normal state of
a plain `python -m venv` checkout. On success the caller's `on_success`
callback fires and the gated action proceeds; on cancel/failure the venv is
left as-is.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..ml.gpu_detect import GpuInfo, detect_supported_nvidia_gpu
from ..paths import find_uv
from .theme import PALETTE

# Pin exactly the versions the legacy project validates against. Floating
# versions (`torch>=2.2`) let pip pull the
# latest — which is a moving target and has been observed to regress
# ConvNeXt inference on the RTX 50-series.
_TARGETS = ("torch==2.9.1", "torchvision==0.24.1")
_CPU_TARGETS = _TARGETS
_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"

# Shown when the caller doesn't name the action. Deliberately generic: this
# dialog now fronts inference as well as training.
DEFAULT_REASON = "This model needs PyTorch"


class TorchInstallDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_success: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Install PyTorch")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.geometry("720x520")
        self.minsize(640, 460)
        self.configure(bg=PALETTE["bg_surface"])
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._on_success = on_success
        self._on_cancel = on_cancel
        self._reason = reason or DEFAULT_REASON
        self._proc: subprocess.Popen | None = None
        self._installing = False
        self._gpu: GpuInfo | None = detect_supported_nvidia_gpu()

        # IMPORTANT: pack the button row FIRST with side=BOTTOM so it reserves
        # vertical space before the resizable content above gets it. Without
        # this the console swallows everything and the buttons get clipped.
        btns = ttk.Frame(self, padding=(12, 0, 12, 12))
        btns.pack(side=tk.BOTTOM, fill=tk.X)
        self._build_buttons(btns)

        wrap = ttk.Frame(self, padding=12)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._build_header(wrap)
        self._build_console(wrap)

    # ----- UI build -----------------------------------------------------------

    def _build_header(self, parent: tk.Misc) -> None:
        ttk.Label(parent, text=self._reason, style="Header.TLabel").pack(anchor="w")

        body = (
            "Running or training a model on this computer requires PyTorch and "
            "torchvision. The download is large and only happens once. "
            "(Models classified by an AI Config server don't need it.)"
        )
        ttk.Label(parent, text=body, style="Muted.TLabel", wraplength=660, justify=tk.LEFT).pack(
            anchor="w",
            pady=(6, 6),
            fill=tk.X,
        )

        if self._gpu is not None:
            gpu_msg = (
                f"Detected supported GPU: {self._gpu.name} "
                f"(compute capability {self._gpu.compute_str}).\n\n"
                "Pick GPU build for fast training, or CPU only if you'd rather "
                "skip the larger download / CUDA driver requirements."
            )
            ttk.Label(parent, text=gpu_msg, style="Accent.TLabel", wraplength=660, justify=tk.LEFT).pack(
                anchor="w",
                pady=(0, 8),
                fill=tk.X,
            )
        else:
            ttk.Label(
                parent,
                text=(
                    "No supported NVIDIA GPU detected — the CPU build will be "
                    "installed. (For Nvidia GPUs requires Ampere or newer; "
                    "compute capability ≥ 8.0.)"
                ),
                style="Muted.TLabel",
                wraplength=660,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(0, 8), fill=tk.X)

    def _build_console(self, parent: tk.Misc) -> None:
        console_wrap = ttk.Frame(parent, style="Card.TFrame", padding=4)
        console_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.console = tk.Text(
            console_wrap,
            height=10,
            wrap=tk.NONE,
            state=tk.DISABLED,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text"],
            insertbackground=PALETTE["accent"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Scrollbar(console_wrap, orient=tk.VERTICAL, command=self.console.yview).pack(side=tk.RIGHT, fill=tk.Y)

    def _build_buttons(self, parent: tk.Misc) -> None:
        self.cancel_btn = ttk.Button(parent, text="Cancel", command=self._cancel)
        self.cancel_btn.pack(side=tk.RIGHT)
        if self._gpu is not None:
            self.gpu_btn = ttk.Button(
                parent,
                text="Install with GPU support",
                style="Accent.TButton",
                command=lambda: self._start_install(use_gpu=True),
            )
            self.gpu_btn.pack(side=tk.RIGHT, padx=(0, 8))
            self.cpu_btn = ttk.Button(
                parent,
                text="CPU only",
                command=lambda: self._start_install(use_gpu=False),
            )
            self.cpu_btn.pack(side=tk.RIGHT, padx=(0, 8))
            self._install_buttons = [self.gpu_btn, self.cpu_btn]
        else:
            self.cpu_btn = ttk.Button(
                parent,
                text="Install PyTorch",
                style="Accent.TButton",
                command=lambda: self._start_install(use_gpu=False),
            )
            self.cpu_btn.pack(side=tk.RIGHT, padx=(0, 8))
            self._install_buttons = [self.cpu_btn]

    # ----- console helpers ----------------------------------------------------

    def _append(self, text: str) -> None:
        try:
            self.console.config(state=tk.NORMAL)
            self.console.insert(tk.END, text)
            self.console.see(tk.END)
            self.console.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    # ----- install flow -------------------------------------------------------

    def _start_install(self, *, use_gpu: bool) -> None:
        if self._installing:
            return
        self._installing = True
        for b in self._install_buttons:
            b.config(state=tk.DISABLED)
        # Mark the chosen button as the one in progress.
        active_btn = self.gpu_btn if (use_gpu and self._gpu is not None) else self.cpu_btn
        active_btn.config(text="Installing…")

        # uv first, because a uv-managed venv (the launcher's default) has no
        # pip in it at all. But a plain `python -m venv` + `pip install -e .`
        # checkout is a documented way to run this app, and there uv may
        # legitimately be absent while pip is right there -- so fall back
        # rather than refusing to install. Only give up if neither exists.
        uv = find_uv()
        if uv is not None:
            cmd: list[str] = [uv, "pip", "install", "--python", sys.executable, *list(_CPU_TARGETS)]
        elif importlib.util.find_spec("pip") is not None:
            self._append("uv not found; falling back to pip in the running interpreter.\n")
            cmd = [sys.executable, "-u", "-m", "pip", "install", *list(_CPU_TARGETS)]
        else:
            self._append(
                "Could not find uv or pip. uv should have been installed by "
                "bootstrap.py on first launch -- try restarting the app via "
                "start.sh/start.bat, or install uv yourself from "
                "https://docs.astral.sh/uv/.\n"
            )
            self._finish(success=False)
            return

        if use_gpu:
            cmd.extend(["--index-url", _CUDA_INDEX])
        self._append("$ " + " ".join(cmd) + "\n")

        try:
            self._proc = subprocess.Popen(
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
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self.after(0, self._append, line)
            rc = self._proc.wait()
        except Exception as exc:
            self.after(0, self._append, f"\n[pip pump] {exc}\n")
            rc = -1
        self.after(0, self._finish, rc == 0)

    def _finish(self, success: bool) -> None:
        self._installing = False
        if success:
            self._append("\nInstall complete.\n")
            for b in self._install_buttons:
                b.config(state=tk.DISABLED, text="Done")
            self.cancel_btn.config(text="Close")
            if self._on_success is not None:
                self._on_success()
            self.destroy()
        else:
            self._append("\nInstall failed. See output above.\n")
            for b in self._install_buttons:
                b.config(state=tk.NORMAL)
            if self._gpu is not None:
                self.gpu_btn.config(text="Install with GPU support")
            self.cpu_btn.config(text="CPU only" if self._gpu is not None else "Install PyTorch")

    def _cancel(self) -> None:
        if self._installing and self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._on_cancel is not None and not self._installing:
            self._on_cancel()
        try:
            self.destroy()
        except tk.TclError:
            pass
