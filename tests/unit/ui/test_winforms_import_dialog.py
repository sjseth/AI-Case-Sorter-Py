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
# A second model, so "pick a couple out of the junk" has something to pick from.
_MODEL_OTHER = {
    "Id": 4,
    "Name": "223 Remington",
    "CartridgeId": 5,
    "ModelType": 0,
    "ModelMode": 0,
}


def _write_install(
    root: Path,
    *,
    images: list[str] | None = None,
    with_defaults: bool = True,
    models: list[dict[str, Any]] | None = None,
    images_by_id: dict[int, list[str]] | None = None,
    headstamps: list[dict[str, Any]] | None = None,
) -> Path:
    """A synthetic legacy install. `images` is sugar for model 3's folder."""
    (root / "Data").mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "Models": models if models is not None else [_MODEL],
        "Cartridges": [{"Id": 2, "Name": "9mm"}, {"Id": 5, "Name": "223"}],
        "Headstamps": headstamps if headstamps is not None else [{"Id": 1, "Name": "GECO", "Model_Id": 3}],
        "HeadStampParents": [],
        "HeadStampParentLinks": [],
        "SlotConfigs": [],
        "Defaults": {"DefaultSerialPort": "COM3", "SlotQuantity": 8} if with_defaults else {},
    }
    (root / winforms_import.CONFIG_DB_NAME).write_text(json.dumps(document), encoding="utf-8-sig")
    per_model = dict(images_by_id or {})
    per_model.setdefault(3, list(images or []))
    for legacy_id, names in per_model.items():
        folder = root / "training" / "images" / str(legacy_id)
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"\xff\xd8\xff\xe0jpeg-ish")
    return root


def _label(item: Any) -> str:
    """Both columns of a row — what it is, then what it would bring."""
    return f"{item.text(0)} — {item.text(1)}"


def _checked(item: Any) -> bool:
    from PySide6.QtCore import Qt

    return bool(item.checkState(0) == Qt.CheckState.Checked)


def _partial(item: Any) -> bool:
    from PySide6.QtCore import Qt

    return bool(item.checkState(0) == Qt.CheckState.PartiallyChecked)


def _set_checked(item: Any, checked: bool) -> None:
    """Tick a row the way a click does — through the signal, so it propagates."""
    from PySide6.QtCore import Qt

    item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)


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
    """Each part row carries the count for the tick that decides it."""
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg", "GECO__2.jpg"])
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    assert _label(dialog.part_items[(3, winforms_import.PART_IMAGES)]) == "Training images — 2"
    assert _label(dialog.part_items[(3, winforms_import.PART_HEADSTAMPS)]).endswith("— 1")
    # And on the model's own row, so an abandoned experiment is recognisable
    # without expanding it.
    assert "2 image(s)" in _label(dialog.model_items[3])
    assert dialog.import_button.isEnabled()
    dialog.close()


def test_every_model_gets_its_own_branch(qapp, window, tmp_path: Path) -> None:
    """The point of the tree: two models, picked apart from one another."""
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL, _MODEL_OTHER],
        images_by_id={3: ["GECO__1.jpg"], 4: ["FC__1.jpg", "FC__2.jpg"]},
        headstamps=[{"Id": 1, "Name": "GECO", "Model_Id": 3}],
    )
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    assert set(dialog.model_items) == {3, 4}
    assert "9mm Base Model" in _label(dialog.model_items[3])
    # Model 4 has images but no headstamps, so it gets no headstamp row at all.
    assert (4, winforms_import.PART_IMAGES) in dialog.part_items
    assert (4, winforms_import.PART_HEADSTAMPS) not in dialog.part_items
    assert (3, winforms_import.PART_HEADSTAMPS) in dialog.part_items
    dialog.close()


def test_a_part_the_model_has_nothing_for_gets_no_row(qapp, window, tmp_path: Path) -> None:
    """A tick that would import nothing is worse than no tick at all."""
    root = _write_install(tmp_path / "legacy", with_defaults=False)
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    # No images on disk and no torch checkpoint — only headstamps to offer.
    assert (3, winforms_import.PART_IMAGES) not in dialog.part_items
    assert (3, winforms_import.PART_CHECKPOINT) not in dialog.part_items
    assert (3, winforms_import.PART_HEADSTAMPS) in dialog.part_items
    # Same rule one level up, for the app-level settings rows.
    assert dialog.items[winforms_import.ITEM_SERIAL].isDisabled()
    assert dialog.items[winforms_import.ITEM_AI_CONFIG].isDisabled()
    dialog.close()


def test_unticking_a_model_takes_its_branch_with_it(qapp, window, tmp_path: Path) -> None:
    """The inheritance is the tree's shape, not a rule the dialog polices."""
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL, _MODEL_OTHER],
        images_by_id={3: ["GECO__1.jpg"], 4: ["FC__1.jpg"]},
    )
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    _set_checked(dialog.model_items[3], False)

    assert not _checked(dialog.part_items[(3, winforms_import.PART_IMAGES)])
    assert _checked(dialog.part_items[(4, winforms_import.PART_IMAGES)])
    # One of two models left ticked, so the parent says so.
    assert _partial(dialog.items[winforms_import.ITEM_MODELS])
    assert set(dialog.selected_options().per_model or {}) == {4}
    dialog.close()


def test_unticking_the_models_row_clears_every_model(qapp, window, tmp_path: Path) -> None:
    root = _write_install(tmp_path / "legacy", models=[_MODEL, _MODEL_OTHER], images=["GECO__1.jpg"])
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    _set_checked(dialog.items[winforms_import.ITEM_MODELS], False)

    assert not any(_checked(row) for row in dialog.model_items.values())
    assert dialog.selected_options().per_model == {}
    assert not dialog.selected_options().any_selected()
    dialog.close()


def test_select_none_then_select_all_walks_the_whole_branch(qapp, window, tmp_path: Path) -> None:
    """Two models out of fifteen must not cost thirteen clicks."""
    root = _write_install(tmp_path / "legacy", models=[_MODEL, _MODEL_OTHER], images=["GECO__1.jpg"])
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    dialog._set_all_models(False)
    assert dialog.selected_options().per_model == {}

    dialog._set_all_models(True)
    assert set(dialog.selected_options().per_model or {}) == {3, 4}
    assert _checked(dialog.part_items[(3, winforms_import.PART_IMAGES)])
    dialog.close()


def test_a_model_can_come_without_its_checkpoint(qapp, window, tmp_path: Path) -> None:
    """The 200 MB item is the one a user most wants to decline individually."""
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg"])
    _torch_zip(root / "training" / "models" / "3.zip")
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)

    _set_checked(dialog.part_items[(3, winforms_import.PART_CHECKPOINT)], False)
    chosen = dialog.selected_options()

    assert chosen.per_model is not None
    assert chosen.per_model[3].checkpoint is False
    assert chosen.per_model[3].images is True
    # The model is still coming, just not its checkpoint.
    assert _partial(dialog.model_items[3])
    dialog.close()


def test_the_model_row_says_what_it_would_do_to_the_library(qapp, window, tmp_path: Path, monkeypatch) -> None:
    """Will this tread on what I already have? — answered per model, up front."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "appdata"))
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg"])
    first = WinFormsImportDialog(window, root, window)
    seen = _quiet(first)
    assert "new model here" in _label(first.model_items[3])
    _run_and_wait(qapp, first, seen)
    first.close()

    again = WinFormsImportDialog(window, root, window)
    _quiet(again)
    assert "updates '9mm Base Model'" in _label(again.model_items[3])
    again.close()


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
    assert _label(dialog.part_items[(3, winforms_import.PART_IMAGES)]) == "Training images — 1"
    dialog.close()


def test_the_selection_line_totals_what_is_ticked(qapp, window, tmp_path: Path) -> None:
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL, _MODEL_OTHER],
        images_by_id={3: ["GECO__1.jpg"], 4: ["FC__1.jpg", "FC__2.jpg"]},
    )
    dialog = WinFormsImportDialog(window, root, window)
    _quiet(dialog)
    assert "2 of 2 model(s)" in dialog.selection_label.text()
    assert "3 image(s)" in dialog.selection_label.text()

    _set_checked(dialog.model_items[4], False)

    assert "1 of 2 model(s)" in dialog.selection_label.text()
    assert "1 image(s)" in dialog.selection_label.text()
    dialog.close()


def test_nothing_ticked_is_refused_rather_than_run(qapp, window, tmp_path: Path) -> None:
    root = _write_install(tmp_path / "legacy")
    dialog = WinFormsImportDialog(window, root, window)
    seen = _quiet(dialog)
    dialog._set_all_models(False)
    for key in (winforms_import.ITEM_IMAGE_PROC, winforms_import.ITEM_SERIAL, winforms_import.ITEM_AI_CONFIG):
        _set_checked(dialog.items[key], False)

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


def test_only_the_ticked_models_are_imported(qapp, window, tmp_path: Path, monkeypatch) -> None:
    """sjseth's ask on #125: bring across a couple, leave the junk behind."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "appdata"))
    root = _write_install(
        tmp_path / "legacy",
        models=[_MODEL, _MODEL_OTHER],
        images_by_id={3: ["GECO__1.jpg"], 4: ["FC__1.jpg", "FC__2.jpg"]},
    )
    dialog = WinFormsImportDialog(window, root, window)
    seen = _quiet(dialog)
    _set_checked(dialog.model_items[4], False)

    _run_and_wait(qapp, dialog, seen)

    result = dialog.result_summary
    assert result is not None
    assert result.models_imported == 1
    assert result.images_copied == 1  # model 4's two images stayed behind
    names = [m.name for m in ModelRepo(window.db).list()]
    assert "9mm Base Model" in names
    assert "223 Remington" not in names
    dialog.close()


def test_a_declined_checkpoint_is_not_copied(qapp, window, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "appdata"))
    root = _write_install(tmp_path / "legacy", images=["GECO__1.jpg"])
    _torch_zip(root / "training" / "models" / "3.zip")
    dialog = WinFormsImportDialog(window, root, window)
    seen = _quiet(dialog)
    _set_checked(dialog.part_items[(3, winforms_import.PART_CHECKPOINT)], False)

    _run_and_wait(qapp, dialog, seen)

    result = dialog.result_summary
    assert result is not None
    assert result.models_imported == 1
    assert result.images_copied == 1
    assert result.checkpoints_copied == 0
    imported = next(m for m in ModelRepo(window.db).list() if m.name == "9mm Base Model")
    assert not imported.model_path
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
