"""The model evaluator dialog, offscreen: mapping, run, report, history.

No torch anywhere. The seam is ``evaluate_folder``'s injectable
``classify_fn`` (``dialog.classify_fn``), so the REAL evaluator walks the
folder, derives ground truth from the filenames, scores and summarizes — only
the classification itself is canned, per image, in ``list_images`` order. The
auto-suggest assertions likewise drive the REAL token scorer against synthetic
folder names, and the HTML report is written by the REAL ``ml.eval_report``
into ``tmp_path`` (the open hook is patched, the file is not).

``CASESORTER_DATA_DIR`` is redirected autouse: the dialog writes reports into
the model's data directory and lists that directory as its history, so an
unset data root would have these tests writing into the developer's library.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from sorter import paths
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, HeadstampRepo, ModelRepo, SettingsRepo
from sorter.ml import evaluator
from sorter.ui.dialog_model_evaluator import (
    UNMAPPED,
    ClassMapperDialog,
    ModelEvaluatorDialog,
    mapping_key,
)


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Keep every path this dialog touches inside the test's tmp dir."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


class _Recorder:
    """Stand-in for ``notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]


# ----- fixtures / helpers ------------------------------------------------------


MODEL_CLASSES = ("9mm FC", "223 REM")


def _write_image(path: Path, color: int = 40, size: int = 24) -> None:
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((size, size, 3), color, dtype=np.uint8))


def _seed_folder(folder: Path, counts: dict[str, int], start_ticks: int = 1) -> list[Path]:
    """Write ``{label}__{ticks}.jpg`` files — the training filename convention."""
    ticks = start_ticks
    written = []
    for label, n in counts.items():
        for _ in range(n):
            path = folder / f"{label}__{ticks}.jpg"
            _write_image(path)
            written.append(path)
            ticks += 1
    return written


@pytest.fixture
def model(config) -> Model:
    """A trainable model with headstamps and a checkpoint file on disk.

    The checkpoint is never loaded (classification is injected), but the
    dialog's preflight refuses to run without one.
    """
    cartridge = CartridgeRepo(config.db).list()[0]
    created = ModelRepo(config.db).create(Model(name="Range brass", cartridge_id=cartridge.id))
    for slot, name in enumerate(MODEL_CLASSES, start=1):
        HeadstampRepo(config.db).add(created.id, name, slot)
    trained = paths.model_trained_dir(created.id)
    trained.mkdir(parents=True, exist_ok=True)
    checkpoint = trained / f"{created.id}.pth"
    checkpoint.write_bytes(b"not-really-a-checkpoint")
    created.model_path = str(checkpoint)
    ModelRepo(config.db).update(created)
    stored = ModelRepo(config.db).get(created.id)
    assert stored is not None
    return stored


@pytest.fixture
def dialog(qapp, config, model):
    """The dialog on a bare ``Config`` — no window, no bus, nothing modal."""
    built = ModelEvaluatorDialog(None, config, model)
    built.notify = _Recorder()
    built.confirm = lambda _title, _text: True
    built.ask_folder = lambda _title, _start: pytest.fail("unexpected folder dialog")
    built.open_url = lambda _path: True
    # Increment 7's gate isn't wired to a bare Config; pin it so the dev venv's
    # torch (or lack of it) can never decide whether these tests run.
    built.ensure_torch = lambda _parent, _proceed, reason=None: True
    yield built
    built.close()


def _classify_by_name(folder: Path, table: dict[str, tuple[str, float]]) -> Any:
    """Canned per-image predictions, handed out in ``list_images`` order."""
    order = [p.name for p in evaluator.list_images(folder)]
    canned = iter([table[name] for name in order])

    def classify(_image: Any, _model_path: str, *, image_size: int | None = None) -> tuple[str, float]:
        return next(canned)

    return classify


def _pump(qapp, predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    for _ in range(5):  # flush signals queued right before the worker finished
        qapp.processEvents()
    return predicate()


def _run(qapp, dialog: ModelEvaluatorDialog) -> None:
    """Start an evaluation and pump the event loop until it has landed."""
    dialog.run_evaluation()
    assert _pump(qapp, lambda: dialog.status_label.text().startswith(("Done", "Stopped", "Evaluation"))), (
        f"evaluation never finished (status: {dialog.status_label.text()!r})"
    )


def _stats(dialog: ModelEvaluatorDialog) -> dict[str, str]:
    return {key: label.text() for key, label in dialog.stat_labels.items()}


def _column(tree: Any, column: int) -> list[str]:
    return [tree.topLevelItem(i).text(column) for i in range(tree.topLevelItemCount())]


# ----- folder pick + class mapping ---------------------------------------------


def test_browse_sets_the_folder_and_defaults_to_the_models_images_dir(dialog, tmp_path, model) -> None:
    assert dialog.folder_edit.text() == str(paths.model_images_dir(model.id))
    folder = tmp_path / "labelled"
    folder.mkdir()
    dialog.ask_folder = lambda _title, _start: str(folder)

    dialog.browse()

    assert dialog.folder_edit.text() == str(folder)


def test_the_mapper_preselects_the_real_auto_suggestions(dialog, tmp_path) -> None:
    # Folder labels deliberately spelled differently from the model's classes:
    # the token scorer in ml/evaluator has to bridge the separators, and give
    # up on the one that matches nothing.
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9MM_FC": 1, "223-REM": 1, "WEIRD": 1})
    dialog.folder_edit.setText(str(folder))

    mapper = dialog.build_mapper()

    assert mapper is not None
    assert {target: combo.currentText() for target, combo in mapper.combos.items()} == {
        "223-REM": "223 REM",
        "9MM_FC": "9mm FC",
        "WEIRD": UNMAPPED,
    }
    mapper.close()


def test_a_saved_mapping_wins_over_the_suggestion_and_survives_a_reopen(dialog, qapp, config, model, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9MM_FC": 1})
    dialog.folder_edit.setText(str(folder))

    dialog.apply_mapping({"9MM_FC": "223 REM"})

    assert SettingsRepo(config.db).get(mapping_key(model.id)) == {"9MM_FC": "223 REM"}
    reopened = ModelEvaluatorDialog(None, config, model)
    try:
        reopened.folder_edit.setText(str(folder))
        mapper = reopened.build_mapper()
        assert mapper is not None
        assert mapper.combos["9MM_FC"].currentText() == "223 REM"
        mapper.close()
    finally:
        reopened.close()


def test_the_mapper_refuses_a_folder_with_no_labelled_images(dialog, tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    dialog.folder_edit.setText(str(empty))

    assert dialog.build_mapper() is None
    assert dialog.notify.titles == ["No classes"]

    dialog.folder_edit.setText(str(tmp_path / "nope"))
    assert dialog.build_mapper() is None
    assert dialog.notify.titles == ["No classes", "No folder"]


def test_auto_map_all_fills_every_row_it_can(qapp, tmp_path) -> None:
    mapper = ClassMapperDialog(None, ["9MM_FC", "WEIRD"], list(MODEL_CLASSES), {})
    mapper.notify = _Recorder()
    try:
        mapper.combos["9MM_FC"].setCurrentText(UNMAPPED)

        mapper.auto_map()

        assert mapper.combos["9MM_FC"].currentText() == "9mm FC"
        assert mapper.combos["WEIRD"].currentText() == UNMAPPED
        assert mapper.notify.calls == [("Auto-map", "Auto-mapped 1 of 2 classes.")]
    finally:
        mapper.close()


def test_saving_an_incomplete_mapping_needs_the_confirmation(qapp) -> None:
    mapper = ClassMapperDialog(None, ["9MM_FC", "WEIRD"], list(MODEL_CLASSES), {})
    try:
        mapper.confirm = lambda _title, _text: False
        mapper.save()
        assert mapper.result() != mapper.DialogCode.Accepted

        mapper.confirm = lambda _title, _text: True
        mapper.save()
        assert mapper.result() == mapper.DialogCode.Accepted
        assert mapper.mapping() == {"9MM_FC": "9mm FC"}  # the unmapped row is dropped
    finally:
        mapper.close()


# ----- preflight ----------------------------------------------------------------


def test_the_preflight_refuses_without_a_folder_images_or_a_checkpoint(dialog, config, model, tmp_path) -> None:
    dialog.folder_edit.setText(str(tmp_path / "missing"))
    dialog.run_evaluation()

    empty = tmp_path / "empty"
    empty.mkdir()
    dialog.folder_edit.setText(str(empty))
    dialog.run_evaluation()

    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 1})
    dialog.folder_edit.setText(str(folder))
    model.model_path = None
    ModelRepo(config.db).update(model)
    untrained = ModelEvaluatorDialog(None, config, model)
    untrained.notify = _Recorder()
    untrained.ensure_torch = lambda *_a, **_k: True
    try:
        untrained.folder_edit.setText(str(folder))
        untrained.run_evaluation()
        assert untrained.notify.titles == ["Not trained"]
    finally:
        untrained.close()

    assert dialog.notify.titles == ["No folder", "No images"]
    assert dialog.results == []


def test_the_windows_torch_gate_is_what_fronts_a_run(qapp, config, model, tmp_path) -> None:
    """A window (increment 7) supplies ``ensure_torch``; the dialog just asks it."""
    import types

    calls: list[dict[str, Any]] = []
    window = types.SimpleNamespace(
        db=config.db,
        ensure_torch=lambda parent, proceed, reason=None: calls.append({"proceed": proceed, "reason": reason}) or False,
    )
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 1})
    built = ModelEvaluatorDialog(None, window, model)
    built.notify = _Recorder()
    try:
        built.folder_edit.setText(str(folder))

        built.run_evaluation()

        # Declined: nothing ran, and the gate got the callback that re-enters.
        assert [call["reason"] for call in calls] == ["Evaluating needs PyTorch"]
        assert calls[0]["proceed"] == built.run_evaluation
        assert built.results == [] and not built.running
    finally:
        built.close()


def test_without_a_gate_a_missing_torch_refuses_instead_of_raising(dialog, monkeypatch, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 1})
    dialog.folder_edit.setText(str(folder))
    dialog.ensure_torch = None
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: False)

    dialog.run_evaluation()

    assert dialog.notify.titles == ["PyTorch required"]
    assert dialog.results == []


# ----- the run ------------------------------------------------------------------


def test_a_run_scores_the_folder_and_renders_the_summary(dialog, qapp, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 2, "223 REM": 2})
    dialog.folder_edit.setText(str(folder))
    # list_images order: 223 REM__3, 223 REM__4, 9mm FC__1, 9mm FC__2 — one miss.
    dialog.classify_fn = _classify_by_name(
        folder,
        {
            "223 REM__3.jpg": ("223 REM", 91.0),
            "223 REM__4.jpg": ("9mm FC", 55.0),
            "9mm FC__1.jpg": ("9mm FC", 99.0),
            "9mm FC__2.jpg": ("9mm FC", 95.0),
        },
    )

    _run(qapp, dialog)

    assert dialog.notify.calls == []
    assert len(dialog.results) == 4
    assert _stats(dialog) == {
        "total": "4",
        "total_acc": "75.0% (3/4)",
        "known_acc": "N/A",  # no mapping in play, so nothing counts as "known"
        "avg_conf": "85.0%",
    }
    assert dialog.count_label.text() == "4 of 4 shown"
    assert _column(dialog.result_tree, 4).count("Mismatch") == 1
    # Per-class rows are keyed by predicted class, sorted case-insensitively.
    assert _column(dialog.summary_tree, 0) == ["223 REM", "9mm FC"]
    assert _column(dialog.summary_tree, 1) == ["1", "3"]
    assert dialog.status_label.text().startswith("Done — 4 image(s) evaluated.")
    # The progress bar tracked the folder and the buttons went back to idle.
    assert (dialog.progress_bar.maximum(), dialog.progress_bar.value()) == (4, 4)
    assert dialog.run_button.isEnabled() and not dialog.cancel_button.isVisible()


def test_a_mapping_makes_a_differently_labelled_folder_score(dialog, qapp, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9MM_FC": 2})
    dialog.folder_edit.setText(str(folder))
    dialog.apply_mapping({"9MM_FC": "9mm FC"})
    dialog.classify_fn = _classify_by_name(
        folder,
        {"9MM_FC__1.jpg": ("9mm FC", 90.0), "9MM_FC__2.jpg": ("223 REM", 40.0)},
    )

    _run(qapp, dialog)

    assert [r["original"] for r in dialog.results] == ["9mm FC", "9mm FC"]
    assert _stats(dialog)["known_acc"] == "50.0% (1/2)"


def test_progress_events_drive_the_bar_and_the_status_line(dialog) -> None:
    dialog._on_progress(2, 5, "9mm FC__2.jpg")

    assert (dialog.progress_bar.maximum(), dialog.progress_bar.value()) == (5, 2)
    assert dialog.status_label.text() == "Classifying 2/5: 9mm FC__2.jpg"

    dialog._on_progress(0, 0, "Building report…")  # status-only message

    assert dialog.status_label.text() == "Building report…"
    assert dialog.progress_bar.value() == 2


def test_cancelling_stops_the_walk_and_keeps_what_was_scored(dialog, qapp, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 5})
    dialog.folder_edit.setText(str(folder))
    canned = _classify_by_name(folder, {f"9mm FC__{i}.jpg": ("9mm FC", 90.0) for i in range(1, 6)})

    def classify_then_cancel(image: Any, model_path: str, *, image_size: int | None = None) -> tuple[str, float]:
        # The stop flag is a threading.Event, not a widget — pressing Cancel
        # from the UI thread is exactly this, plus a label update.
        dialog._stop_event.set()
        return canned(image, model_path, image_size=image_size)

    dialog.classify_fn = classify_then_cancel

    _run(qapp, dialog)

    assert len(dialog.results) == 1
    assert dialog.status_label.text().startswith("Stopped — 1 image(s) evaluated.")
    assert dialog.run_button.isEnabled() and not dialog.cancel_button.isVisible()


def test_a_worker_failure_is_reported_and_the_dialog_recovers(dialog, qapp, monkeypatch, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 1})
    dialog.folder_edit.setText(str(folder))

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("checkpoint is not a checkpoint")

    monkeypatch.setattr(evaluator, "evaluate_folder", boom)

    dialog.run_evaluation()
    assert _pump(qapp, lambda: bool(dialog.notify.calls))

    assert dialog.notify.calls[0] == ("Evaluation failed", "checkpoint is not a checkpoint")
    assert dialog.status_label.text() == "Evaluation failed."
    assert dialog.run_button.isEnabled() and not dialog.cancel_button.isVisible()


# ----- results table ------------------------------------------------------------


def test_the_filters_narrow_the_table_without_losing_the_results(dialog, qapp, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 2, "223 REM": 1})
    (folder / "unlabelled.jpg").write_bytes((folder / "9mm FC__1.jpg").read_bytes())
    dialog.folder_edit.setText(str(folder))
    dialog.classify_fn = _classify_by_name(
        folder,
        {
            "223 REM__3.jpg": ("9mm FC", 50.0),  # mismatch
            "9mm FC__1.jpg": ("9mm FC", 99.0),
            "9mm FC__2.jpg": ("9mm FC", 95.0),
            "unlabelled.jpg": ("9mm FC", 70.0),  # no ground truth
        },
    )
    _run(qapp, dialog)

    dialog.match_combo.setCurrentText("Mismatch")
    assert _column(dialog.result_tree, 0) == ["223 REM__3.jpg"]
    assert dialog.count_label.text() == "1 of 4 shown"

    dialog.match_combo.setCurrentText("No ground truth")
    assert _column(dialog.result_tree, 0) == ["unlabelled.jpg"]

    dialog.match_combo.setCurrentText("All")
    dialog.filter_edit.setText("9mm")
    assert _column(dialog.result_tree, 0) == ["9mm FC__1.jpg", "9mm FC__2.jpg"]

    dialog.filter_edit.setText("")
    assert len(dialog.result_rows_shown()) == 4  # the filter hid, it never deleted


def test_rows_carry_the_match_status_as_a_color(dialog, qapp, tmp_path) -> None:
    """Match status reads as colour: green / red / muted, from the palette."""
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 1, "223 REM": 1})
    dialog.folder_edit.setText(str(folder))
    dialog.classify_fn = _classify_by_name(
        folder,
        {"223 REM__2.jpg": ("9mm FC", 50.0), "9mm FC__1.jpg": ("9mm FC", 99.0)},
    )
    _run(qapp, dialog)

    colors = {
        dialog.result_tree.topLevelItem(row).text(4): dialog.result_tree.topLevelItem(row).foreground(0).color().name()
        for row in range(dialog.result_tree.topLevelItemCount())
    }
    assert colors == {
        "Mismatch": dialog.role_color("error"),
        "Match": dialog.role_color("success"),
    }


def test_a_preview_change_rescores_without_a_rerun(dialog, qapp, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 2})
    dialog.folder_edit.setText(str(folder))
    dialog.classify_fn = _classify_by_name(
        folder,
        {"9mm FC__1.jpg": ("9mm FC", 99.0), "9mm FC__2.jpg": ("223 REM", 40.0)},
    )
    _run(qapp, dialog)
    assert _stats(dialog)["total_acc"] == "50.0% (1/2)"

    # Reclassifying the mispredicted image's ground truth makes it a match.
    mispredicted = next(r for r in dialog.results if r["predicted"] == "223 REM")
    renamed = folder / "223 REM__2.jpg"
    dialog.apply_image_change(
        {
            "action": "reclassified",
            "old_path": mispredicted["filepath"],
            "new_path": str(renamed),
            "new_headstamp": "223 REM",
        }
    )

    assert _stats(dialog)["total_acc"] == "100.0% (2/2)"
    assert _column(dialog.result_tree, 0) == ["223 REM__2.jpg", "9mm FC__1.jpg"]

    dialog.apply_image_change({"action": "deleted", "old_path": str(renamed)})

    assert len(dialog.results) == 1
    assert _stats(dialog) == {
        "total": "1",
        "total_acc": "100.0% (1/1)",
        "known_acc": "N/A",
        "avg_conf": "99.0%",
    }


# ----- report + history ----------------------------------------------------------


def test_the_run_writes_a_real_report_and_the_history_lists_it(dialog, qapp, model, tmp_path) -> None:
    folder = tmp_path / "labelled"
    _seed_folder(folder, {"9mm FC": 1})
    dialog.folder_edit.setText(str(folder))
    dialog.classify_fn = _classify_by_name(folder, {"9mm FC__1.jpg": ("9mm FC", 97.5)})
    opened: list[str] = []
    dialog.open_url = lambda path: bool(opened.append(path)) or True

    _run(qapp, dialog)

    reports = list(paths.model_reports_dir(model.id).glob("*.html"))
    assert len(reports) == 1, "the run left no report in the model's reports dir"
    html = reports[0].read_text(encoding="utf-8")
    # Written by the real ml.eval_report: the results are embedded in the page.
    assert '"filename": "9mm FC__1.jpg"' in html
    assert '"classification": "9mm FC"' in html
    assert '"confidence": 0.975' in html

    assert [Path(p).name for p in dialog.report_paths()] == [reports[0].name]
    assert _column(dialog.history_tree, 0) == [reports[0].name]
    assert dialog.history_tree.currentItem() is not None, "the fresh report wasn't selected"
    assert dialog.status_label.text().endswith("Report saved → History tab.")

    dialog.open_selected_report()
    assert opened == [str(reports[0].resolve())]


def test_history_lists_existing_reports_newest_first(dialog, model) -> None:
    reports_dir = paths.model_reports_dir(model.id)
    reports_dir.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(("modelreport_old.html", "modelreport_new.html")):
        path = reports_dir / name
        path.write_text("<html></html>", encoding="utf-8")
        import os

        os.utime(path, (1_700_000_000 + index * 60, 1_700_000_000 + index * 60))

    dialog.refresh_history()

    assert _column(dialog.history_tree, 0) == ["modelreport_new.html", "modelreport_old.html"]
    # No selection: "Open in browser" falls back to the newest.
    assert dialog.selected_report() == reports_dir / "modelreport_new.html"


def test_history_created_column_sorts_on_raw_mtime_not_locale_text(dialog, model) -> None:
    """A locale-formatted date string doesn't sort lexically the way a raw
    timestamp does (e.g. US ``9/1/26`` vs. ``12/1/25``) — the sort key must
    be the raw mtime, not the displayed text. Filenames are picked so name
    order and mtime order disagree, ruling out a name-based sort as a
    false positive."""
    from PySide6.QtCore import Qt

    reports_dir = paths.model_reports_dir(model.id)
    reports_dir.mkdir(parents=True, exist_ok=True)
    import os

    newest_named_first = reports_dir / "modelreport_a.html"
    oldest_named_last = reports_dir / "modelreport_b.html"
    newest_named_first.write_text("<html></html>", encoding="utf-8")
    oldest_named_last.write_text("<html></html>", encoding="utf-8")
    os.utime(newest_named_first, (1_800_000_000, 1_800_000_000))
    os.utime(oldest_named_last, (1_700_000_000, 1_700_000_000))

    dialog.refresh_history()
    dialog.history_tree.sortItems(1, Qt.SortOrder.AscendingOrder)

    assert _column(dialog.history_tree, 0) == ["modelreport_b.html", "modelreport_a.html"]


def test_opening_a_report_when_there_are_none_says_so(dialog) -> None:
    dialog.open_selected_report()

    assert dialog.notify.titles == ["No report"]
