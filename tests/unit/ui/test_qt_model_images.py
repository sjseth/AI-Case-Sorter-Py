"""Training-image browser + preview: per-class counts/filter, reclassify and
delete hitting real files on disk, lazy thumbnail decode, and preview
navigation.

Thumbnails load on a background ``QThread``; ``_wait_for_thumbnails`` is the
pattern for that (mirrors ``conftest.drain_until``'s bounded poll, but
pumping ``QApplication.processEvents()`` instead of the event bus — this
dialog has no bus, only queued Qt signals).
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from sorter.data import image_store
from sorter.ui.dialog_image_preview import ImagePreviewDialog
from sorter.ui.dialog_model_images import ModelImagesDialog

from .conftest import seed_model


def _write_image(path: Path, color: int = 30, size: int = 24) -> None:
    import cv2

    frame = np.full((size, size, 3), color, dtype=np.uint8)
    cv2.imwrite(str(path), frame)


def _seed_images(images_dir: Path, counts: dict[str, int], start_ticks: int = 1) -> list[Path]:
    """Write synthetic ``{label}__{ticks}.jpg`` files, image_store's filename convention."""
    images_dir.mkdir(parents=True, exist_ok=True)
    ticks = start_ticks
    paths = []
    for label, n in counts.items():
        for _ in range(n):
            path = images_dir / f"{label}__{ticks}.jpg"
            _write_image(path)
            paths.append(path)
            ticks += 1
    return paths


def _wait_for_thumbnails(qapp, dialog: ModelImagesDialog, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if not dialog.loading:
            break
        time.sleep(0.005)
    for _ in range(5):  # flush signals queued right before the worker finished
        qapp.processEvents()


def _fail_notify(title: str, text: str) -> None:
    pytest.fail(f"unexpected dialog: {title} — {text}")


# ----- ModelImagesDialog: listing, filtering, counts ------------------------


def test_lists_images_with_per_class_counts_and_filter(qapp, config, tmp_path) -> None:
    model_id = seed_model(config, {"WIN": 1, "REM": 2, "FED": 0})
    images_dir = tmp_path / "images"
    _seed_images(images_dir, {"WIN": 4, "REM": 3, "FED": 2})
    _write_image(images_dir / "ZZZ__900.jpg")  # unrecognized headstamp
    _write_image(images_dir / "stray.jpg")  # no `__` separator at all

    dialog = ModelImagesDialog(None, config, model_id, images_dir=images_dir)
    dialog.notify = _fail_notify
    try:
        _wait_for_thumbnails(qapp, dialog)

        assert dialog.counts_label.text() == "11 images"
        assert len(dialog.tiles) == 11
        # every tile's thumbnail round-tripped through the worker thread
        assert all(not tile.image_label.pixmap().isNull() for tile in dialog.tiles.values())

        dialog.filter_combo.setCurrentText("WIN")
        _wait_for_thumbnails(qapp, dialog)
        assert dialog.counts_label.text() == "4 images"
        assert len(dialog.tiles) == 4
        assert all(tile.headstamp == "WIN" for tile in dialog.tiles.values())

        dialog.filter_combo.setCurrentText(image_store.UNKNOWN_FILTER)
        _wait_for_thumbnails(qapp, dialog)
        assert dialog.counts_label.text() == "2 images"
        assert len(dialog.tiles) == 2

        dialog.filter_combo.setCurrentText(image_store.ALL_FILTER)
        _wait_for_thumbnails(qapp, dialog)
        assert len(dialog.tiles) == 11
    finally:
        dialog.close()


def test_no_images_shows_empty_state(qapp, config, tmp_path) -> None:
    model_id = seed_model(config, {"WIN": 1})
    images_dir = tmp_path / "images"

    dialog = ModelImagesDialog(None, config, model_id, images_dir=images_dir)
    try:
        assert dialog.counts_label.text() == "0 images"
        assert not dialog.tiles
        assert not dialog.empty_label.isHidden()
    finally:
        dialog.close()


# ----- ModelImagesDialog: reclassify / delete -------------------------------


def test_reclassify_selected_renames_file_and_refreshes_grid(qapp, config, tmp_path) -> None:
    model_id = seed_model(config, {"WIN": 1, "REM": 2})
    images_dir = tmp_path / "images"
    paths = _seed_images(images_dir, {"WIN": 2})

    dialog = ModelImagesDialog(None, config, model_id, images_dir=images_dir)
    dialog.notify = _fail_notify
    try:
        _wait_for_thumbnails(qapp, dialog)
        target = str(paths[0])
        tile = dialog.tiles[target]
        QTest.mouseClick(tile, Qt.MouseButton.LeftButton)
        assert tile.selected
        assert dialog.selection_label.text() == "1 selected"

        dialog.target_combo.setCurrentText("REM")
        QTest.mouseClick(dialog.reclassify_button, Qt.MouseButton.LeftButton)
        _wait_for_thumbnails(qapp, dialog)

        on_disk = image_store.list_images(images_dir)
        assert not (images_dir / Path(target).name).exists()
        assert [image_store.parse_headstamp(p) for p in on_disk].count("REM") == 1
        assert [image_store.parse_headstamp(p) for p in on_disk].count("WIN") == 1
        assert len(dialog.tiles) == 2
        assert dialog.selection_label.text() == "0 selected"  # refresh rebuilds tiles, unselected
    finally:
        dialog.close()


def test_delete_selected_respects_confirm_hook(qapp, config, tmp_path) -> None:
    model_id = seed_model(config, {"WIN": 1})
    images_dir = tmp_path / "images"
    _seed_images(images_dir, {"WIN": 3})

    dialog = ModelImagesDialog(None, config, model_id, images_dir=images_dir)
    dialog.notify = _fail_notify
    try:
        _wait_for_thumbnails(qapp, dialog)
        tile = next(iter(dialog.tiles.values()))
        QTest.mouseClick(tile, Qt.MouseButton.LeftButton)

        dialog.confirm = lambda title, text: False
        QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
        assert len(image_store.list_images(images_dir)) == 3  # refused -> file untouched
        assert len(dialog.tiles) == 3

        dialog.confirm = lambda title, text: True
        QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
        _wait_for_thumbnails(qapp, dialog)
        assert len(image_store.list_images(images_dir)) == 2
        assert len(dialog.tiles) == 2
    finally:
        dialog.close()


def test_context_menu_reclassify_and_delete_one(qapp, config, tmp_path) -> None:
    """The single-tile path (right-click menu), bypassing the multi-select flow."""
    model_id = seed_model(config, {"WIN": 1, "REM": 1})
    images_dir = tmp_path / "images"
    paths = _seed_images(images_dir, {"WIN": 1})

    dialog = ModelImagesDialog(None, config, model_id, images_dir=images_dir)
    dialog.notify = _fail_notify
    dialog.confirm = lambda title, text: True
    try:
        _wait_for_thumbnails(qapp, dialog)
        tile = dialog.tiles[str(paths[0])]

        dialog._reclassify_one(tile, "REM")
        on_disk = image_store.list_images(images_dir)
        assert len(on_disk) == 1
        assert image_store.parse_headstamp(on_disk[0]) == "REM"

        renamed_tile = next(iter(dialog.tiles.values()))
        dialog._delete_one(renamed_tile)
        assert not image_store.list_images(images_dir)
        assert not dialog.tiles
    finally:
        dialog.close()


# ----- ImagePreviewDialog: navigation ----------------------------------------


def test_preview_navigates_in_order(qapp, tmp_path) -> None:
    images_dir = tmp_path / "images"
    paths = _seed_images(images_dir, {"WIN": 3})

    dialog = ImagePreviewDialog(None, paths, 0, ["WIN", "REM"])
    try:
        assert dialog.current_path == paths[0]
        assert not dialog.prev_button.isEnabled()
        assert dialog.next_button.isEnabled()

        dialog.show_next()
        assert dialog.current_path == paths[1]
        assert dialog.prev_button.isEnabled()
        assert dialog.next_button.isEnabled()

        dialog.show_next()
        assert dialog.current_path == paths[2]
        assert not dialog.next_button.isEnabled()

        dialog.show_next()  # already at the end -- no-op
        assert dialog.current_path == paths[2]

        dialog.show_prev()
        assert dialog.current_path == paths[1]
    finally:
        dialog.close()


# ----- ImagePreviewDialog: reclassify / delete hit disk ----------------------


def test_preview_reclassify_and_delete_hit_disk(qapp, tmp_path) -> None:
    images_dir = tmp_path / "images"
    paths = _seed_images(images_dir, {"WIN": 2})
    changes: list[dict] = []

    dialog = ImagePreviewDialog(None, paths, 0, ["WIN", "REM"], on_changed=changes.append)
    dialog.notify = _fail_notify
    try:
        dialog.target_combo.setCurrentText("REM")
        dialog.reclassify()

        assert changes[-1]["action"] == "reclassified"
        assert changes[-1]["new_headstamp"] == "REM"
        assert not paths[0].exists()
        new_path = Path(changes[-1]["new_path"])
        assert new_path.exists()
        assert dialog.current_path == new_path  # stays open, showing the renamed file

        dialog.confirm = lambda title, text: True
        dialog.delete_current()
        assert changes[-1]["action"] == "deleted"
        assert not new_path.exists()
        assert dialog.current_path == paths[1]  # one image left -- dialog advances, doesn't close
        assert dialog.result() != QDialog.DialogCode.Accepted

        dialog.delete_current()
        assert changes[-1]["action"] == "deleted"
        assert not paths[1].exists()
        assert dialog.result() == QDialog.DialogCode.Accepted  # nothing left -- dialog closes itself
    finally:
        dialog.close()


def test_preview_delete_refused_without_confirm(qapp, tmp_path) -> None:
    images_dir = tmp_path / "images"
    paths = _seed_images(images_dir, {"WIN": 1})
    changes: list[dict] = []

    dialog = ImagePreviewDialog(None, paths, 0, ["WIN"], on_changed=changes.append)
    dialog.confirm = lambda title, text: False
    try:
        dialog.delete_current()
        assert paths[0].exists()
        assert not changes
    finally:
        dialog.close()
