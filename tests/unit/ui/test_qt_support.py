"""The support package: report content, redaction, the ZIP, and the dialog.

Redaction is the load-bearing part — a raw API key or an absolute path in the
report is exactly what must never reach Discord — so those assertions run
against a config that *has* a secret and a checkpoint path set.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication

from sorter import paths
from sorter.data.repository import ModelRepo
from sorter.ui.dialog_support import SupportDialog, build_support_dialog, open_support_dialog
from sorter.ui.support_bundle import (
    CONFIG_MEMBER,
    REPORT_MEMBER,
    collect_data,
    collect_report,
    write_bundle,
)

from .conftest import seed_model

SECRET = "sk-super-secret-key-123"


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Pin the data root so path-redaction assertions have a known base."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


def _set_api_key(config: Any, value: str) -> None:
    config.api["api_key"] = value
    config.save()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))


# ----- collect_report ---------------------------------------------------------


def test_report_has_expected_sections(config) -> None:
    seed_model(config, {"9mm FC": 1})
    report = collect_report(config, config.db)
    for header in (
        "== Application ==",
        "== Active model ==",
        "== Run options ==",
        "== Training configuration ==",
        "== Image processing ==",
        "== Serial ==",
        "== Camera ==",
        "== AI Config (HTTP classification) ==",
    ):
        assert header in report
    assert "Test model" in report
    assert "convnext_tiny" in report
    assert "user-owned" in report


def test_report_never_contains_the_api_key(config) -> None:
    _set_api_key(config, SECRET)
    report = collect_report(config, config.db)
    assert SECRET not in report
    assert "api_key: set" in report


def test_report_masks_an_empty_key_as_not_set(config) -> None:
    _set_api_key(config, "")
    assert "api_key: not set" in collect_report(config, config.db)


def test_report_in_ai_config_mode_says_so(config) -> None:
    report = collect_report(config, config.db)
    assert "None — AI Config mode (HTTP classification)" in report
    assert "(no active local model)" in report


def test_report_paths_are_data_root_relative(config, tmp_path: Path) -> None:
    model_id = seed_model(config, {"9mm FC": 1})
    repo = ModelRepo(config.db)
    model = repo.get(model_id)
    assert model is not None
    checkpoint = paths.model_trained_path(model_id)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_bytes(b"stub")
    model.model_path = str(checkpoint)
    model.training_config.image_directory = "/home/someone/private/images"
    repo.update(model)

    report = collect_report(config, config.db)
    assert str(tmp_path / "data") not in report
    assert str(checkpoint.relative_to(paths.app_data_dir())) in report
    assert "/home/someone/private/images" not in report
    assert "(outside data root)" in report
    assert "checkpoint: present" in report


def test_collect_data_masks_the_key_in_json_too(config) -> None:
    _set_api_key(config, SECRET)
    data = collect_data(config, config.db)
    assert data["ai_config"]["api_key"] == "set"
    assert SECRET not in json.dumps(data)


# ----- write_bundle -----------------------------------------------------------


def test_write_bundle_zip_members(config, tmp_path: Path) -> None:
    _set_api_key(config, SECRET)
    out = write_bundle(tmp_path / "pkg.zip", config, config.db)
    assert out == tmp_path / "pkg.zip"
    assert not (tmp_path / "pkg.zip.tmp").exists()
    with zipfile.ZipFile(out) as archive:
        assert set(archive.namelist()) == {REPORT_MEMBER, CONFIG_MEMBER}
        report = archive.read(REPORT_MEMBER).decode("utf-8")
        data = json.loads(archive.read(CONFIG_MEMBER))
    assert SECRET not in report
    assert data["ai_config"]["api_key"] == "set"
    assert data["run_options"]["confidence_floor"] == config.run_confidence_floor


# ----- the dialog -------------------------------------------------------------


def test_copy_button_fills_the_clipboard(window) -> None:
    dialog = build_support_dialog(window)
    QGuiApplication.clipboard().clear()
    dialog.copy_button.click()
    assert QGuiApplication.clipboard().text() == dialog.report_text
    assert "== Run options ==" in dialog.report_text


def test_save_goes_through_the_seam(window, tmp_path: Path) -> None:
    dialog = build_support_dialog(window)
    asked: list[str] = []
    notified = _Recorder()
    dest = tmp_path / "bundle.zip"

    def fake_ask(default_name: str) -> str:
        asked.append(default_name)
        return str(dest)

    dialog.ask_save_path = fake_ask
    dialog.notify = notified
    dialog.save_button.click()

    assert asked and asked[0].endswith(".zip")
    with zipfile.ZipFile(dest) as archive:
        assert set(archive.namelist()) == {REPORT_MEMBER, CONFIG_MEMBER}
    assert notified.calls and notified.calls[0][0] == "Support package saved"


def test_save_cancelled_writes_nothing(window, tmp_path: Path) -> None:
    dialog = build_support_dialog(window)
    notified = _Recorder()
    dialog.ask_save_path = lambda default_name: None
    dialog.notify = notified
    dialog.save_button.click()
    assert notified.calls == []
    assert list(tmp_path.glob("*.zip")) == []


def test_entry_point_builds_against_the_window(window, monkeypatch) -> None:
    """``open_support_dialog`` is what app.py wires to the Help menu."""
    opened: list[SupportDialog] = []
    monkeypatch.setattr(SupportDialog, "exec", lambda self: opened.append(self))
    open_support_dialog(window)
    assert len(opened) == 1
    assert opened[0].parent() is window


def test_the_help_menu_exports_a_support_package(window, monkeypatch) -> None:
    """Seth's route to it: Help → Export support package…, above About."""
    opened: list[SupportDialog] = []
    monkeypatch.setattr(SupportDialog, "exec", lambda self: opened.append(self))
    texts = [a.text() for a in window.menus["Help"].actions() if a.text()]
    assert texts.index("Export support package…") < texts.index("About")

    actions = {a.text(): a for a in window.menus["Help"].actions() if a.text()}
    actions["Export support package…"].trigger()

    assert len(opened) == 1
    assert opened[0].parent() is window
