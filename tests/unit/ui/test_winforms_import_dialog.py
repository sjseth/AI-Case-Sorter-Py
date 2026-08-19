"""The Windows-app import dialog: what it offers, and what it refuses to offer.

Offscreen, no display. The dialog's `notify` / `ask_directory` seams are
replaced so nothing opens a native modal (§5), and the import itself runs
against a synthetic install tree — nothing here touches `C:\\Program Files`.
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from sorter.data import winforms_import
from sorter.data.repository import ModelRepo, SettingsRepo
from sorter.ui.app import SETTINGS_SECTIONS
from sorter.ui.dialog_winforms_import import (
    IMPORT_BUTTON_ROLE,
    SECTION_NAME,
    WinFormsImportDialog,
    build_winforms_import_section,
    maybe_offer_first_run,
    summarize,
)

_MODEL = {
    "Id": 3,
    "Name": "9mm Base Model",
    "CartridgeId": 2,
    "ModelType": 0,
    "ModelMode": 0,
}


def _write_install(root: Path, *, images: list[str] | None = None, with_defaults: bool = True) -> Path:
    (root / "Data").mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "Models": [_MODEL],
        "Cartridges": [{"Id": 2, "Name": "9mm"}],
        "Headstamps": [{"Id": 1, "Name": "GECO", "Model_Id": 3}],
        "HeadStampParents": [],
        "HeadStampParentLinks": [],
        "SlotConfigs": [],
        "Defaults": {"DefaultSerialPort": "COM3", "SlotQuantity": 8} if with_defaults else {},
    }
    (root / winforms_import.CONFIG_DB_NAME).write_text(json.dumps(document), encoding="utf-8-sig")
    folder = root / "training" / "images" / "3"
    folder.mkdir(parents=True, exist_ok=True)
    for name in images or []:
        (folder / name).write_bytes(b"\xff\xd8\xff\xe0jpeg-ish")
    return root


def _torch_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("trainedmodel/data.pkl", b"\x80\x02}q\x00.")


def _quiet(dialog: WinFormsImportDialog) -> list[tuple[str, str]]:
    """Replace the modal seam and hand back what it was told."""
    seen: list[tuple[str, str]] = []
    dialog.notify = lambda title, text: seen.append((title, text))
    return seen


def _run_and_wait(qapp: Any, dialog: WinFormsImportDialog, seen: list[tuple[str, str]]) -> None:
    """Start the import and pump until the worker's queue has been drained.

    The dialog drains on a 100 ms QTimer that an offscreen test never runs, so
    `_drain` is called directly — the same main-thread entry point the timer
    uses. A failure arrives through `notify` rather than as a result, so `seen`
    is what turns a worker exception into a legible assertion instead of a
    timeout.
    """
    deadline = time.monotonic() + 10.0
    dialog.start_import()
    while time.monotonic() < deadline:
        qapp.processEvents()
        dialog._drain()
        if dialog.result_summary is not None:
            return
        if any(title == "Import failed" for title, _ in seen):
            raise AssertionError(f"import failed: {seen}")
        time.sleep(0.01)
    raise AssertionError(f"import did not finish (notified: {seen})")


# ----- what the dialog offers -------------------------------------------------


def test_counts_come_from_the_survey(qapp, window, tmp_path: Path) -> None:
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg", "GECO__2.jpg"])
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    assert "2 image(s)" in dialog.hints[winforms_import.ITEM_IMAGES].text()
    assert "1 headstamp(s)" in dialog.hints[winforms_import.ITEM_HEADSTAMPS].text()
    assert dialog.import_button.isEnabled()
    dialog.close()


def test_items_the_installation_lacks_are_greyed_out(qapp, window, tmp_path: Path) -> None:
    """Offering a tick that would import nothing is worse than not offering it."""
    root = _write_install(tmp_path / "legacy", with_defaults=False)
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    assert not dialog.checkboxes[winforms_import.ITEM_IMAGES].isEnabled()
    assert not dialog.checkboxes[winforms_import.ITEM_IMAGES].isChecked()
    assert not dialog.checkboxes[winforms_import.ITEM_SERIAL].isEnabled()
    assert not dialog.checkboxes[winforms_import.ITEM_AI_CONFIG].isEnabled()
    assert dialog.checkboxes[winforms_import.ITEM_HEADSTAMPS].isEnabled()
    dialog.close()


def test_unticking_models_disables_its_dependents(qapp, window, tmp_path: Path) -> None:
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg"])
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)
    assert dialog.checkboxes[winforms_import.ITEM_IMAGES].isEnabled()

    dialog.checkboxes[winforms_import.ITEM_MODELS].setChecked(False)

    assert not dialog.checkboxes[winforms_import.ITEM_IMAGES].isEnabled()
    assert not dialog.checkboxes[winforms_import.ITEM_HEADSTAMPS].isEnabled()
    dialog.close()


def test_a_folder_that_is_not_an_installation_says_so(qapp, window, tmp_path: Path) -> None:
    dialog = WinFormsImportDialog(window, tmp_path / "not-it", window)
    _quiet(dialog)
    assert not dialog.import_button.isEnabled()
    assert dialog.warning_label.text()
    dialog.close()


def test_choose_folder_reloads_the_counts(qapp, window, tmp_path: Path) -> None:
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg"])
    dialog = WinFormsImportDialog(window, None, window)
    _quiet(dialog)
    assert not dialog.import_button.isEnabled()

    dialog.ask_directory = lambda: str(root)
    dialog._choose_folder()

    assert dialog.import_button.isEnabled()
    assert "1 image(s)" in dialog.hints[winforms_import.ITEM_IMAGES].text()
    dialog.close()


def test_nothing_ticked_is_refused_rather_than_run(qapp, window, tmp_path: Path) -> None:
    root = _write_install(tmp_path / "legacy")
    dialog = WinFormsImportDialog(window, root, window)
    seen = _quiet(dialog)
    for box in dialog.checkboxes.values():
        box.setChecked(False)

    dialog.start_import()

    assert dialog.result_summary is None
    assert seen and "at least one" in seen[0][1]
    dialog.close()


# ----- running it -------------------------------------------------------------


def test_import_lands_and_the_shell_is_refreshed(qapp, window, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "appdata"))
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg"])
    _torch_zip(root / "training" / "models" / "3.zip")
    dialog = WinFormsImportDialog(window, root, window)
    seen = _quiet(dialog)

    _run_and_wait(qapp, dialog, seen)

    result = dialog.result_summary
    assert result is not None
    assert result.models_imported == 1
    assert result.images_copied == 1
    assert result.checkpoints_copied == 1
    assert any(m.name == "9mm Base Model" for m in ModelRepo(window.db).list())
    assert seen and seen[-1][0] == "Import complete"
    dialog.close()


def test_summary_says_what_landed() -> None:
    result = winforms_import.ImportResult(models_imported=2, images_copied=40, warnings=["a warning from the survey"])
    text = summarize(result)
    assert "2 model(s) imported" in text
    assert "40 training image(s) copied" in text
    assert "a warning from the survey" in text


def test_summary_of_an_import_that_had_nothing_to_do() -> None:
    assert "already here" in summarize(winforms_import.ImportResult())


# ----- the first-run offer ----------------------------------------------------


def test_first_run_offer_stays_silent_with_no_installation(qapp, window, monkeypatch) -> None:
    """The one case that matters most: a user who never ran the Windows app."""
    monkeypatch.setattr(winforms_import, "find_installation", lambda: None)
    assert maybe_offer_first_run(window) is None


def test_first_run_offer_is_marked_as_made(qapp, window, tmp_path: Path, monkeypatch) -> None:
    root = _write_install(tmp_path / "legacy")
    monkeypatch.setattr(winforms_import, "find_installation", lambda: root)
    opened: list[Path | None] = []
    # Not `exec()`: a modal event loop offscreen never returns.
    monkeypatch.setattr(
        "sorter.ui.dialog_winforms_import.open_import_dialog",
        lambda win, root_, **kw: opened.append(root_),
    )

    maybe_offer_first_run(window)

    assert opened == [root]
    assert SettingsRepo(window.db).get(winforms_import.FIRST_RUN_SEEN_KEY) is True
    # Offered once, ever — Settings keeps it reachable after that.
    assert maybe_offer_first_run(window) is None


# ----- the Settings section ---------------------------------------------------


def test_settings_section_is_listed_and_reachable(qapp, window) -> None:
    assert SECTION_NAME in SETTINGS_SECTIONS
    window._open_settings_section(SECTION_NAME)
    assert window.settings_list.currentItem().text() == SECTION_NAME


def test_settings_section_offers_the_import(qapp, window) -> None:
    from PySide6.QtWidgets import QPushButton

    page = build_winforms_import_section(window)
    buttons = [b for b in page.findChildren(QPushButton) if b.property("role") == IMPORT_BUTTON_ROLE]
    assert len(buttons) == 1
