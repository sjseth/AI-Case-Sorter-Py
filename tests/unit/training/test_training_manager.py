"""Tests for the training subprocess manager.

Uses a stub training script (not the real torch-backed one) so the tests are
fast and require no GPU/torch install.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sorter.control.events import EventBus
from sorter.data.models import TrainingConfig
from sorter.training.manager import TrainingJob, TrainingManager, build_command


def _write_stub(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _collect(bus: EventBus, topic: str, sink: list) -> None:
    bus.subscribe(topic, lambda payload: sink.append(payload))


def _run(
    tmp_path: Path, script_body: str, *, cfg_overrides: dict | None = None
) -> tuple[TrainingManager, EventBus, dict[str, list]]:
    bus = EventBus()
    sinks: dict[str, list] = {
        "start": [],
        "epoch": [],
        "done": [],
        "log": [],
        "error": [],
        "failed": [],
        "cancelled": [],
    }
    for k in sinks:
        _collect(bus, f"training/{k}", sinks[k])

    script = _write_stub(tmp_path / "stub_train.py", script_body)
    cfg = TrainingConfig()
    for k, v in (cfg_overrides or {}).items():
        setattr(cfg, k, v)

    job = TrainingJob(
        image_dir=tmp_path / "imgs",
        output_model=tmp_path / "out.pth",
        config=cfg,
    )
    mgr = TrainingManager(bus)
    mgr.spawn(job, script=script)
    mgr.wait(timeout=30)
    # Pump bus once so subscribers receive everything.
    bus.drain(max_items=256)
    return mgr, bus, sinks


def test_build_command_includes_all_required_flags(tmp_path: Path) -> None:
    cfg = TrainingConfig(model_name="convnext_base", epochs=5, batch_size=8, use_swa=True)
    job = TrainingJob(image_dir=tmp_path / "imgs", output_model=tmp_path / "out.pth", config=cfg)
    cmd = build_command(job)
    assert "--model_name" in cmd and "convnext_base" in cmd
    assert "--epochs" in cmd and "5" in cmd
    assert "--use_swa" in cmd


def test_progress_markers_dispatch_to_bus_topics(tmp_path: Path) -> None:
    script_body = """
        import json, sys
        def emit(event, **payload):
            payload["event"] = event
            sys.stdout.write("[PROGRESS] " + json.dumps(payload) + chr(10))
            sys.stdout.flush()
        emit("start", epochs=2, classes=3, images=10)
        emit("epoch", epoch=1, train_loss=0.5, train_acc=0.7, val_acc=0.6)
        emit("epoch", epoch=2, train_loss=0.3, train_acc=0.8, val_acc=0.7)
        emit("done", best_val_acc=0.7, best_val_loss=0.3)
    """
    mgr, _, sinks = _run(tmp_path, script_body)
    assert len(sinks["start"]) == 1 and sinks["start"][0]["epochs"] == 2
    assert len(sinks["epoch"]) == 2
    assert sinks["epoch"][1]["epoch"] == 2
    assert len(sinks["done"]) == 1
    assert mgr.last_result() == {"best_val_acc": 0.7, "best_val_loss": 0.3}


def test_non_progress_stdout_goes_to_log(tmp_path: Path) -> None:
    script_body = """
        import sys
        print("hello world", flush=True)
        print("[PROGRESS] not json{", flush=True)
        print("[PROGRESS] " + '{"event":"done"}', flush=True)
    """
    _, _, sinks = _run(tmp_path, script_body)
    # "hello world" goes to log; the malformed progress line is *also* logged.
    log_lines = [s for s in sinks["log"] if s]
    assert "hello world" in log_lines
    assert any("not json{" in s for s in log_lines)
    assert len(sinks["done"]) == 1


def test_nonzero_exit_emits_failed(tmp_path: Path) -> None:
    script_body = """
        import sys
        sys.stderr.write("kaboom" + chr(10))
        sys.exit(7)
    """
    _, _, sinks = _run(tmp_path, script_body)
    assert any("kaboom" in s for s in sinks["error"])
    assert len(sinks["failed"]) == 1
    assert sinks["failed"][0]["return_code"] == 7


def test_cancel_emits_cancelled(tmp_path: Path) -> None:
    import time

    script_body = """
        import time, sys
        sys.stdout.write("[PROGRESS] " + '{"event":"start","epochs":99}' + chr(10))
        sys.stdout.flush()
        # Sleep long enough that cancel() arrives first
        time.sleep(30)
    """
    bus = EventBus()
    sinks: dict[str, list] = {"cancelled": [], "start": []}
    for k in sinks:
        _collect(bus, f"training/{k}", sinks[k])
    script = _write_stub(tmp_path / "stub.py", script_body)
    mgr = TrainingManager(bus)
    mgr.spawn(
        TrainingJob(
            image_dir=tmp_path / "imgs",
            output_model=tmp_path / "out.pth",
            config=TrainingConfig(),
        ),
        script=script,
    )
    # Wait for the start marker to confirm the child is running.
    deadline = time.time() + 5
    while time.time() < deadline:
        bus.drain(max_items=64)
        if sinks["start"]:
            break
        time.sleep(0.05)
    assert sinks["start"], "child never emitted start; cannot test cancel"
    mgr.cancel()
    mgr.wait(timeout=10)
    bus.drain(max_items=64)
    assert len(sinks["cancelled"]) == 1


def test_cannot_spawn_two_concurrent_jobs(tmp_path: Path) -> None:
    script_body = """
        import time
        time.sleep(5)
    """
    bus = EventBus()
    script = _write_stub(tmp_path / "stub.py", script_body)
    mgr = TrainingManager(bus)
    job = TrainingJob(
        image_dir=tmp_path / "imgs",
        output_model=tmp_path / "out.pth",
        config=TrainingConfig(),
    )
    mgr.spawn(job, script=script)
    try:
        with pytest.raises(RuntimeError):
            mgr.spawn(job, script=script)
    finally:
        mgr.cancel()
        mgr.wait(timeout=10)
