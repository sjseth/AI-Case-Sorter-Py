"""The PyTorch install gate: who gets prompted, and who never does.

The rule under test is that torch is installed the first time something
actually needs it and never before — so an AI Config user is never prompted,
and a local-model user is prompted *before* the machine feeds a case rather
than after the run dies on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from sorter.data.db import Database
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, ModelRepo, SettingsRepo
from sorter.ml import classifier, local_inference


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "gate.db")
    db.ensure_initialized()
    return db


def _local_model(db: Database, tmp_path: Path) -> Model:
    """An active model with a checkpoint on disk — i.e. one that needs torch."""
    cart = CartridgeRepo(db).create("9mm-gate")
    ckpt = tmp_path / "m.pth"
    ckpt.write_bytes(b"NOTAREALCHECKPOINT")
    model = ModelRepo(db).create(Model(name="Local", cartridge_id=cart.id))
    model.model_path = str(ckpt)
    ModelRepo(db).update(model)
    SettingsRepo(db).set_active_model_id(model.id)
    return model


# ----- the routing predicate ------------------------------------------------
# These guard the property that makes the gate correct: it must fire in
# exactly the cases where classify_active would reach local_inference.


def test_ai_config_mode_does_not_use_local_inference(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    SettingsRepo(db).clear_active_model()
    assert classifier.uses_local_inference(db) is False


def test_active_model_with_checkpoint_uses_local_inference(tmp_path: Path) -> None:
    db = _seed_db(tmp_path)
    _local_model(db, tmp_path)
    assert classifier.uses_local_inference(db) is True


def test_an_active_model_routes_locally_even_without_a_checkpoint(
    tmp_path: Path,
) -> None:
    """Routing follows the active model, not the checkpoint.

    A missing checkpoint is a failure to report, not a reason to switch to
    HTTP — so this still counts as local, and `checkpoint_problem` is what
    tells the user it won't work.
    """
    db = _seed_db(tmp_path)
    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    assert seed.model_path in (None, "")
    assert classifier.uses_local_inference(db) is True
    assert classifier.has_local_checkpoint(seed) is False
    assert classifier.checkpoint_problem(db) is not None


def test_a_deleted_checkpoint_is_reported_not_routed_to_http(
    tmp_path: Path,
) -> None:
    db = _seed_db(tmp_path)
    model = _local_model(db, tmp_path)
    assert model.model_path is not None  # _local_model always sets one
    Path(model.model_path).unlink()
    assert classifier.uses_local_inference(db) is True
    assert classifier.checkpoint_problem(db) is not None


def test_no_db_does_not_use_local_inference() -> None:
    assert classifier.uses_local_inference(None) is False


# ----- the cheap presence probe ---------------------------------------------


def test_is_installed_does_not_import_torch(monkeypatch) -> None:
    """`is_installed` must not pay the import — it runs on the UI thread.

    Importing torch here would also run the device probe and the environment
    dump's synthetic benchmarks, freezing the UI on every button press.
    """
    called = []

    def _boom():
        called.append(1)
        raise AssertionError("is_installed must not import torch")

    monkeypatch.setattr(local_inference, "_torch", _boom)
    local_inference.is_installed()
    assert not called


def test_is_installed_reports_false_when_torch_absent(monkeypatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert local_inference.is_installed() is False


def test_is_installed_reports_true_when_both_present(monkeypatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert local_inference.is_installed() is True


def test_is_installed_requires_torchvision_too(monkeypatch) -> None:
    """torch alone isn't enough — local_inference imports torchvision.models."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "torch" else None,
    )
    assert local_inference.is_installed() is False


def test_is_installed_survives_a_broken_module(monkeypatch) -> None:
    """find_spec raises for a half-installed package; that means 'no', not a crash."""
    import importlib.util

    def _raise(name):
        raise ValueError("__spec__ is None")

    monkeypatch.setattr(importlib.util, "find_spec", _raise)
    assert local_inference.is_installed() is False


# ----- the gate itself ------------------------------------------------------

pytest.importorskip("tkinter")

import tkinter as tk  # noqa: E402

from sorter.ui import torch_gate  # noqa: E402

# ensure_torch's `parent` only ever needs to be a valid `after()`/dialog
# anchor; these tests replace the dialog with `_FakeDialog`, which never
# touches it, so a real Tk widget (and the display it would require) is
# unnecessary — stand in with `None` typed as the `tk.Misc` it never uses.
_NO_PARENT = cast(tk.Misc, None)


class _FakeDialog:
    """Stands in for TorchInstallDialog — constructing the real one shells out
    to nvidia-smi and needs a display."""

    instances: list[_FakeDialog] = []

    def __init__(self, parent, *, on_success=None, on_cancel=None, reason=None):
        self.parent = parent
        self.on_success = on_success
        self.on_cancel = on_cancel
        self.reason = reason
        _FakeDialog.instances.append(self)


@pytest.fixture
def fake_dialog(monkeypatch):
    _FakeDialog.instances = []
    monkeypatch.setattr(torch_gate, "TorchInstallDialog", _FakeDialog)
    return _FakeDialog


def test_gate_passes_through_when_torch_is_installed(monkeypatch, fake_dialog) -> None:
    monkeypatch.setattr(torch_gate.local_inference, "is_installed", lambda: True)
    assert torch_gate.ensure_torch(_NO_PARENT, lambda: None) is True
    assert fake_dialog.instances == []


def test_gate_blocks_and_offers_install_when_torch_is_missing(
    monkeypatch,
    fake_dialog,
) -> None:
    monkeypatch.setattr(torch_gate.local_inference, "is_installed", lambda: False)
    ran = []
    assert (
        torch_gate.ensure_torch(
            _NO_PARENT,
            lambda: ran.append("proceed"),
            reason="Sorting needs PyTorch",
        )
        is False
    )
    # Blocked: the caller's action must NOT have run yet.
    assert ran == []
    assert len(fake_dialog.instances) == 1
    assert fake_dialog.instances[0].reason == "Sorting needs PyTorch"


def test_gate_runs_the_action_only_after_a_successful_install(
    monkeypatch,
    fake_dialog,
) -> None:
    monkeypatch.setattr(torch_gate.local_inference, "is_installed", lambda: False)
    ran = []
    torch_gate.ensure_torch(_NO_PARENT, lambda: ran.append("proceed"))
    assert ran == []
    fake_dialog.instances[0].on_success()  # pip finished
    assert ran == ["proceed"]


def test_cancelling_the_install_does_not_run_the_action(
    monkeypatch,
    fake_dialog,
) -> None:
    monkeypatch.setattr(torch_gate.local_inference, "is_installed", lambda: False)
    ran = []
    torch_gate.ensure_torch(_NO_PARENT, lambda: ran.append("proceed"))
    dialog = fake_dialog.instances[0]
    if dialog.on_cancel is not None:
        dialog.on_cancel()
    assert ran == []
