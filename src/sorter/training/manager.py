"""Subprocess-based training launcher.

Spawns `train_convnext.py` as a child process, reads its stdout/stderr line
by line, and surfaces `[PROGRESS]` JSON markers as event-bus events:

    training/start         {"epochs": 15, "classes": 7, "images": 320, ...}
    training/epoch         {"epoch": 3, "train_loss": 0.42, "val_acc": 0.87, ...}
    training/log           "raw stdout line"
    training/error         "raw stderr line"
    training/done          {"best_val_acc": 0.94, "best_val_loss": 0.12, ...}
    training/cancelled     None
    training/failed        "error description"

Cancellation: `cancel()` sends SIGTERM and after 5 s escalates to SIGKILL.
On Windows, `terminate()` is used as the SIGTERM equivalent.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..events import EventBus
from ..models import TrainingConfig

_PROGRESS_PREFIX = "[PROGRESS] "
_KILL_GRACE_S = 5.0


@dataclass
class TrainingJob:
    image_dir: Path
    output_model: Path
    config: TrainingConfig
    extra_env: dict[str, str] = field(default_factory=dict)


def build_command(job: TrainingJob, *, python: str | None = None, script: Path | None = None) -> list[str]:
    """Construct the argv that `manager.spawn` would use. Exposed for tests."""
    script_path = script or Path(__file__).with_name("train_convnext.py")
    py = python or sys.executable
    args = [
        py,
        "-u",
        str(script_path),
        "--image_dir",
        str(job.image_dir),
        "--output_model",
        str(job.output_model),
        "--model_name",
        job.config.model_name,
        "--epochs",
        str(job.config.epochs),
        "--batch_size",
        str(job.config.batch_size),
        "--lr",
        str(job.config.learning_rate),
        "--weight_decay",
        str(job.config.weight_decay),
        "--dropout",
        str(job.config.dropout_rate),
        "--val_split",
        str(job.config.val_split),
        "--imgsize",
        str(job.config.image_size),
        "--max_workers",
        str(job.config.max_workers),
        "--stochastic_depth_prob",
        str(job.config.stochastic_depth_prob),
        "--focal_gamma",
        str(job.config.focal_gamma),
        "--swa_start",
        str(job.config.swa_start),
        "--swa_mode",
        str(job.config.swa_mode),
        "--swa_acc_threshold",
        str(job.config.swa_acc_threshold),
        "--swa_patience",
        str(job.config.swa_patience),
        "--swa_min_epoch",
        str(job.config.swa_min_epoch),
    ]
    if job.config.train_all:
        args.append("--trainall")
    if job.config.freeze_backbone:
        args.append("--freeze_backbone")
    if job.config.use_focal_loss:
        args.append("--use_focal_loss")
    if job.config.use_swa:
        args.append("--use_swa")
    return args


class TrainingManager:
    """Owns at most one active training subprocess."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._proc: subprocess.Popen | None = None
        self._reader_threads: list[threading.Thread] = []
        self._cancel_requested = False
        self._lock = threading.Lock()
        self._completion_event = threading.Event()
        self._last_result: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def spawn(self, job: TrainingJob, *, python: str | None = None, script: Path | None = None) -> subprocess.Popen:
        if self.is_running:
            raise RuntimeError("Training already in progress")

        cmd = build_command(job, python=python, script=script)
        env = dict(os.environ)
        env.update(job.extra_env)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        with self._lock:
            self._proc = proc
            self._cancel_requested = False
            self._last_result = None
            self._completion_event.clear()

        t_out = threading.Thread(
            target=self._pump_stdout,
            args=(proc,),
            daemon=True,
            name="train-stdout",
        )
        t_err = threading.Thread(
            target=self._pump_stderr,
            args=(proc,),
            daemon=True,
            name="train-stderr",
        )
        t_wait = threading.Thread(
            target=self._wait_for_exit,
            args=(proc,),
            daemon=True,
            name="train-wait",
        )
        self._reader_threads = [t_out, t_err, t_wait]
        for t in self._reader_threads:
            t.start()
        return proc

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._cancel_requested = True
        if sys.platform == "win32":
            proc.terminate()
        else:
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return

        # Escalate to SIGKILL after grace period.
        def _escalate() -> None:
            try:
                proc.wait(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                if proc.poll() is None:
                    proc.kill()

        threading.Thread(target=_escalate, daemon=True).start()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the active training run completes."""
        return self._completion_event.wait(timeout=timeout)

    def last_result(self) -> dict[str, Any] | None:
        return self._last_result

    # ----- internal ----------------------------------------------------------

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith(_PROGRESS_PREFIX):
                try:
                    payload = json.loads(line[len(_PROGRESS_PREFIX) :])
                except json.JSONDecodeError:
                    self.bus.post("training/log", line)
                    continue
                event = payload.pop("event", "log")
                if event == "done":
                    self._last_result = payload
                self.bus.post(f"training/{event}", payload)
            else:
                self.bus.post("training/log", line)

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.rstrip("\r\n")
            self.bus.post("training/error", line)

    def _wait_for_exit(self, proc: subprocess.Popen) -> None:
        rc = proc.wait()
        # Drain final threads briefly so any tail output appears before "done".
        for t in self._reader_threads:
            if t is threading.current_thread():
                continue
            t.join(timeout=2.0)
        if self._cancel_requested:
            self.bus.post("training/cancelled", {"return_code": rc})
        elif rc != 0:
            self.bus.post("training/failed", {"return_code": rc})
        elif self._last_result is None:
            # Process exited cleanly but emitted no "done" marker — surface it
            # so the UI can still close the progress dialog.
            self.bus.post("training/done", {})
        with self._lock:
            self._proc = None
        self._completion_event.set()
