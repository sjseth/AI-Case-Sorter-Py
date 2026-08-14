"""The Train activity, its two dialogs, and a real training run — offscreen.

Everything runs against the real SQLite-backed ``Config`` from the shared UI
conftest, with ``CASESORTER_DATA_DIR`` pointed at ``tmp_path``: this page writes
training images and a checkpoint path, so an unset data root would have the
tests saving into the developer's own model library.

The board and the camera are stubs (no hardware, no torch), but nothing else
is: images go through ``save_training_image`` onto a real disk, and the
training tests drive a real ``TrainingManager`` against a real subprocess —
a stub trainer script that emits the same ``[PROGRESS]`` markers
``train_convnext.py`` does, the way ``tests/unit/training`` fakes it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QListView

from sorter import paths
from sorter.data.models import TrainingConfig
from sorter.data.repository import HeadstampRepo, ModelRepo, SettingsRepo
from sorter.training.dataset import parse_label
from sorter.ui.dialog_training_config import NUMERIC_FIELDS, TrainingConfigDialog
from sorter.ui.train_page import (
    CAPTURED_TEXT,
    FOREIGN_MODEL_TEXT,
    NO_BROKER_TOOLTIP,
    NO_TORCH_TEXT,
    PREDICTED_TEXT,
    UNAVAILABLE_TITLE_AI,
    UNAVAILABLE_TITLE_FOREIGN,
)

from .conftest import drain_until, seed_model


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Keep every path this page touches inside the test's tmp dir.

    Autouse so it lands before the ``config``/``window`` fixtures build
    anything; the assertion is the proof that it did — this page writes
    training images, and the suite must never reach a real library.
    """
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


# ----- stubs -----------------------------------------------------------------


class _Recorder:
    """Stand-in for ``win.notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]


class _FakeCamera:
    """One fixed frame, no device. ``stop`` is what window teardown calls."""

    def __init__(self, frame: np.ndarray | None) -> None:
        self.frame = frame
        self.captures = 0

    def capture_frame(self) -> np.ndarray | None:
        self.captures += 1
        return self.frame

    def latest_frame(self) -> np.ndarray | None:
        return self.frame

    def stop(self) -> None:
        pass


class _FakeBroker:
    """Records the slot each feed was routed to. ``ok`` fails the feed."""

    def __init__(self, *, ok: bool = True) -> None:
        self.slots: list[int] = []
        self.ok = ok

    def force_sort_and_move(self, slot: int) -> bool:
        self.slots.append(int(slot))
        return self.ok

    def stop(self) -> None:
        pass


def _frame() -> np.ndarray:
    """A dark frame with a bright disc — something the Hough crop can chew on."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[140:340, 220:420] = 200
    return frame


@pytest.fixture
def page(window):
    window.notify = _Recorder()
    window.camera = _FakeCamera(_frame())
    return window.train_page


@pytest.fixture
def connected(page, window):
    """The page with a board attached, refreshed the way a connect does."""
    window.broker = _FakeBroker()
    page.refresh_connection()
    return page


# ----- helpers ---------------------------------------------------------------


def activate(config: Any, assignments: dict[str, int] | None = None, **fields: Any) -> int:
    model_id = seed_model(config, assignments or {}, name=fields.pop("name", "Train model"))
    if fields:
        model = ModelRepo(config.db).get(model_id)
        assert model is not None
        for key, value in fields.items():
            setattr(model, key, value)
        ModelRepo(config.db).update(model)
    return model_id


def do_feed(page: Any, window: Any, **kwargs: Any) -> None:
    page.feed(**kwargs)
    assert drain_until(window, lambda: not page._feed_in_flight), "feed never came back"


def image_names(model_id: int) -> list[str]:
    directory = paths.model_images_dir(model_id)
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# Prepended to every stub trainer: the same `[PROGRESS] {json}` protocol the
# real `train_convnext.py` speaks, plus `arg()` for reading its own argv.
EMIT_PREAMBLE = (
    "import json, sys, time\n"
    "def arg(name):\n"
    "    return sys.argv[sys.argv.index(name) + 1]\n"
    "def emit(event, **payload):\n"
    "    payload['event'] = event\n"
    "    sys.stdout.write('[PROGRESS] ' + json.dumps(payload) + chr(10))\n"
    "    sys.stdout.flush()\n"
)


def write_stub_trainer(tmp_path: Path, body: str) -> Path:
    """A throwaway trainer that talks the manager's protocol and exits."""
    script = tmp_path / "stub_train.py"
    script.write_text(EMIT_PREAMBLE + textwrap.dedent(body), encoding="utf-8")
    return script


# ----- the capture loop ------------------------------------------------------


def test_feed_is_disabled_until_a_board_is_connected(page, config) -> None:
    activate(config)
    page.refresh()

    assert not page.feed_button.isEnabled()
    assert page.feed_button.toolTip() == NO_BROKER_TOOLTIP

    page._win.broker = _FakeBroker()
    page.refresh_connection()

    assert page.feed_button.isEnabled()
    assert page.feed_button.toolTip() == ""


def test_feed_drops_a_case_captures_and_arms_save(connected, window, config) -> None:
    activate(config)
    connected.refresh()

    do_feed(connected, window)

    assert window.broker.slots == [0]  # catch-all: Sort while training is off
    assert window.camera.captures == 1
    assert connected._last_cropped is not None
    assert connected.save_button.isEnabled()
    assert connected.status_label.text() == CAPTURED_TEXT


def test_feed_reports_a_board_timeout_and_captures_nothing(connected, window, config) -> None:
    activate(config)
    window.broker.ok = False

    do_feed(connected, window)

    assert window.camera.captures == 0
    assert connected._last_cropped is None
    assert "timeout" in connected.status_label.text().lower()


def test_sort_while_training_routes_the_case_to_the_labels_slot(connected, window, config) -> None:
    activate(config, {"9mm FC": 3})
    connected.refresh()

    connected.sort_check.setChecked(True)
    connected.label_combo.setCurrentText("9mm FC")
    do_feed(connected, window)

    assert window.broker.slots == [3]
    # The toggle is persisted, not just held in the widget.
    assert config.sort_while_training is True


def test_sort_while_training_uses_the_carried_label_not_the_field(connected, window, config) -> None:
    """A Save chains into the next feed carrying the label it just saved."""
    activate(config, {"9mm FC": 3, "S&B": 4})
    connected.refresh()

    connected.sort_check.setChecked(True)
    connected.label_combo.setCurrentText("9mm FC")
    do_feed(connected, window, sort_label="S&B")

    assert window.broker.slots == [4]


def test_an_unknown_label_falls_through_to_the_catch_all(connected, window, config) -> None:
    activate(config, {"9mm FC": 3})
    connected.refresh()

    connected.sort_check.setChecked(True)
    connected.label_combo.setCurrentText("never seen")
    do_feed(connected, window)

    assert window.broker.slots == [0]


# ----- saving ----------------------------------------------------------------


def test_save_writes_a_correctly_named_training_image(connected, window, config) -> None:
    model_id = activate(config, {"9mm FC": 1})
    connected.refresh()
    do_feed(connected, window)

    dest = connected.save_current("9mm FC")

    assert dest is not None and dest.exists()
    assert dest.parent == paths.model_images_dir(model_id)
    assert parse_label(dest.name) == "9mm FC"
    assert dest.suffix == ".jpg"
    assert image_names(model_id) == [dest.name]


def test_save_clears_the_capture_so_a_case_cannot_be_written_twice(connected, window, config) -> None:
    model_id = activate(config, {"9mm FC": 1})
    connected.refresh()
    do_feed(connected, window)

    connected.save_current("9mm FC")
    connected.save_current("9mm FC")

    assert len(image_names(model_id)) == 1
    assert "Capture first" in window.notify.titles


def test_save_registers_a_brand_new_label_as_a_headstamp(connected, window, config) -> None:
    model_id = activate(config, {"9mm FC": 1})
    connected.refresh()
    do_feed(connected, window)

    connected.save_current("Winchester")

    names = {h.name for h in HeadstampRepo(config.db).list_for_model(model_id)}
    assert "Winchester" in names
    assert connected.counts()["Winchester"] == 1


def test_counts_update_after_each_save(connected, window, config) -> None:
    activate(config, {"9mm FC": 1})
    connected.refresh()

    assert connected.counts() == {"9mm FC": 0}

    for _ in range(2):
        do_feed(connected, window)
        connected.save_current("9mm FC")

    assert connected.counts() == {"9mm FC": 2}


def test_counts_back_fill_labels_found_only_on_disk(page, config) -> None:
    model_id = activate(config, {"9mm FC": 1})
    images = paths.model_images_dir(model_id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "Orphan__123.jpg").write_bytes(b"x")
    (images / "notes.txt").write_bytes(b"x")

    page.refresh()

    assert page.counts() == {"9mm FC": 0, "Orphan": 1}
    # Back-filled into the model, so the Sort grid and the field agree.
    assert "Orphan" in {h.name for h in HeadstampRepo(config.db).list_for_model(model_id)}


def test_save_without_a_label_asks_rather_than_writing(connected, window, config) -> None:
    model_id = activate(config)
    connected.refresh()
    do_feed(connected, window)

    connected.label_combo.setCurrentText("   ")
    connected.save_clicked()

    assert window.notify.titles == ["Missing label"]
    assert image_names(model_id) == []


def test_clicking_a_count_row_saves_under_that_label(connected, window, config) -> None:
    model_id = activate(config, {"9mm FC": 1, "S&B": 2})
    connected.refresh()
    do_feed(connected, window)
    # The chained auto-feed is a queued timer; this asserts the save alone.
    connected.feed = lambda **_kwargs: None

    connected._on_count_clicked(connected.counts_list.item(1))

    assert [parse_label(n) for n in image_names(model_id)] == ["S&B"]


def test_clicking_a_count_row_before_a_feed_says_so(page, config) -> None:
    model_id = activate(config, {"9mm FC": 1})
    page.refresh()

    page._on_count_clicked(page.counts_list.item(0))

    assert "Feed first" in page.status_label.text()
    assert image_names(model_id) == []


def test_a_clicked_row_is_still_visually_selected(connected, config) -> None:
    activate(config, {"9mm FC": 1, "S&B": 2})
    connected.refresh()

    connected.counts_list.setCurrentItem(connected.counts_list.item(1))

    assert connected.counts_list.item(1).isSelected()


# ----- the counts list reflows into columns ----------------------------------
#
# A model can carry 150+ classifications, so the list wraps top-to-bottom into
# columns instead of scrolling forever. QListView's TopToBottom+wrapping flow
# picks how many ROWS fit a column from the viewport's *height*; width then
# decides how many of those columns are visible without a horizontal scroll —
# so "wide" is asserted by the absence of that scrollbar, "narrow" by its
# presence, rather than by an item's column index moving.


def _many_headstamps(n: int) -> dict[str, int]:
    return {f"Label {i:03d}": i % 20 for i in range(n)}


def test_counts_list_uses_wrapping_top_to_bottom_flow(page) -> None:
    assert page.counts_list.flow() == QListView.Flow.TopToBottom
    assert page.counts_list.isWrapping()
    assert page.counts_list.resizeMode() == QListView.ResizeMode.Adjust


def test_a_wide_counts_list_shows_multiple_columns_without_scrolling(qapp, window, config) -> None:
    # The item count is derived from measured geometry, never hard-coded: how
    # many rows one column holds follows the platform font, so a fixed count
    # gave Windows CI a single column where Linux overflowed (or vice versa).
    activate(config, _many_headstamps(1))
    window.train_page.refresh()
    window.show_page("Train")
    window.resize(1600, 250)
    # Set before the first show(): a splitter resize on an already-shown list
    # updates its geometry but not this Qt version's cached item layout, so
    # only the size in place for the first real layout pass is trustworthy.
    # (A model change does relayout, which is what lets the second refresh
    # below take effect.)
    window.train_page.body_splitter.setSizes([200, 1300])
    window.show()
    for _ in range(10):
        qapp.processEvents()

    lst = window.train_page.counts_list
    cell = lst.visualItemRect(lst.item(0))
    rows_per_column = max(1, lst.viewport().height() // cell.height())
    # Enough to spill into a second column; few enough that two columns of any
    # font's width fit the ~1200px pane.
    activate(config, _many_headstamps(rows_per_column + 2))
    window.train_page.refresh()
    for _ in range(10):
        qapp.processEvents()

    first = lst.visualItemRect(lst.item(0))
    later = lst.visualItemRect(lst.item(lst.count() - 1))

    assert later.x() > first.x()  # a later item landed in a further column
    assert later.x() + later.width() <= lst.viewport().width()  # and it's on screen
    assert lst.horizontalScrollBar().maximum() == 0  # every column fits — nothing to scroll


def test_a_narrow_counts_list_needs_a_scroll_past_the_first_column(qapp, window, config) -> None:
    activate(config, _many_headstamps(40))
    window.train_page.refresh()
    window.show_page("Train")
    window.resize(1600, 250)
    window.train_page.body_splitter.setSizes([1300, 200])
    window.show()
    for _ in range(10):
        qapp.processEvents()

    lst = window.train_page.counts_list
    assert lst.horizontalScrollBar().maximum() > 0  # later columns exist but aren't all visible


def test_dragging_the_splitter_widens_the_list_and_the_scrollbar_follows(qapp, window, config) -> None:
    activate(config, _many_headstamps(40))
    window.train_page.refresh()
    window.show_page("Train")
    window.resize(1600, 250)
    splitter = window.train_page.body_splitter
    lst = window.train_page.counts_list
    # Narrow FIRST, set before the first show: only the first layout pass is
    # trustworthy for item geometry offscreen (see the wide test's note), and
    # the narrow state is the one that must prove a scrollbar exists.
    splitter.setSizes([1300, 200])
    window.show()
    for _ in range(10):
        qapp.processEvents()
    narrow_width = lst.width()
    narrow_hbar_max = lst.horizontalScrollBar().maximum()
    # The wide width is measured, not hard-coded: how many columns 40 items
    # need follows the platform font (Windows CI overflowed a fixed 1300px).
    cell = lst.visualItemRect(lst.item(0))
    rows_per_column = max(1, lst.viewport().height() // cell.height())
    # A full spare column of slack: frame widths and scrollbar-gutter
    # arithmetic differ per platform (Windows CI came up exactly 2px short
    # of a fixed +40).
    columns = -(-lst.count() // rows_per_column) + 1
    required = columns * cell.width()

    window.resize(required + 700, window.height())
    splitter.setSizes([600, required + 60])
    for _ in range(10):
        qapp.processEvents()
    wide_width = lst.width()
    wide_hbar_max = lst.horizontalScrollBar().maximum()

    assert wide_width > narrow_width  # dragging left actually widened the list
    assert narrow_hbar_max > 0  # narrow: later columns needed a scroll...
    # ...wide: the reflow followed and the scroll is gone. Not `== 0`:
    # Windows' QListView keeps a few px of contents overhang even when every
    # column fits (frame/gutter arithmetic), so demand "vanishingly small
    # next to a column", not "zero".
    assert wide_hbar_max < cell.width() // 4


# ----- the page reads as two workflows ---------------------------------------
#
# Feed and the sort toggle both govern what the NEXT feed does, so they ride
# together above the preview; "Training settings…"/"Start training" are the
# other workflow on the page and cluster at its foot. Every assertion below is
# derived from measured geometry — button sizes follow the platform font.


@pytest.fixture
def shown(qapp, window, config):
    """The Train page laid out for real — geometry means nothing before this.

    A trainable model is activated first: without one the page shows the
    unavailable explainer instead, and every assertion below would be
    measuring a hidden widget.
    """
    activate(config)
    window.show_page("Train")
    # Tall enough that the fixed-size crop plus its Feed row never squeeze:
    # with a too-small budget QVBoxLayout overlaps the fixed widget instead
    # of scrolling, and geometry assertions measure the overlap, not the design.
    window.resize(1200, 850)
    window.show()
    for _ in range(10):
        qapp.processEvents()
    return window.train_page


def test_the_sort_toggle_sits_with_the_training_settings(shown) -> None:
    # JL: routing cases while you train is a training setting worn as a
    # shortcut — it belongs beside "Training settings…", not beside Feed.
    assert shown.sort_check.parentWidget() is shown.training_group
    check = shown.sort_check.geometry()
    settings = shown.settings_button.geometry()
    assert check.top() < settings.bottom() and settings.top() < check.bottom()  # one row
    assert check.right() <= settings.left()  # toggle reads first, then the dialog


def test_feed_is_centered_directly_under_the_preview(shown) -> None:
    # JL: Feed belongs to the picture it refills — centered under it.
    feed = shown.feed_button.geometry()
    crop = shown.crop_label.geometry()
    save = shown.save_button.geometry()

    assert feed.top() >= crop.bottom()  # under the preview
    assert save.top() >= feed.bottom()  # and above the label/Save row
    assert abs(feed.center().x() - crop.center().x()) <= feed.width()  # centered


def test_the_training_buttons_cluster_at_the_foot_of_the_page(shown) -> None:
    assert shown.settings_button.parentWidget() is shown.training_group
    assert shown.train_button.parentWidget() is shown.training_group
    # …and nothing from the capture loop is in there with them.
    assert shown.feed_button.parentWidget() is shown.capture_group

    settings = shown.settings_button.geometry()
    start = shown.train_button.geometry()

    assert settings.top() < start.bottom() and start.top() < settings.bottom()  # one row
    assert start.left() >= settings.right()
    # Adjacent, not merely on the same row: layout spacing, never a button's width.
    assert start.left() - settings.right() < min(settings.width(), start.width())
    # The whole cluster is below the capture/counts body.
    assert shown.training_group.geometry().top() >= shown.body_splitter.geometry().bottom()


def test_the_splitter_handle_starts_where_the_group_frames_do(shown, window) -> None:
    """The divider used to run up into the header band above the groups (JL)."""
    from PySide6.QtGui import QColor

    splitter = shown.body_splitter
    image = splitter.grab().toImage()
    border = QColor(window.palette_colors["border"]).rgb()

    def first_border_row(x: int) -> int | None:
        return next((y for y in range(image.height()) if image.pixel(x, y) == border), None)

    # Sampled well right of a group's title, which is drawn in the same margin
    # band and breaks the top border wherever it sits.
    group_top = first_border_row(shown.counts_group.geometry().right() - 40)
    handle_top = first_border_row(splitter.handle(1).geometry().center().x())

    assert group_top is not None
    assert handle_top == group_top


# ----- the torch offer -------------------------------------------------------


def with_checkpoint(config: Any, tmp_path: Path) -> int:
    """An active model whose checkpoint exists — the only case torch matters for."""
    checkpoint = tmp_path / "model.pth"
    checkpoint.write_bytes(b"not really a checkpoint")
    return activate(config, {"9mm FC": 1}, model_path=str(checkpoint))


def test_feed_proceeds_without_a_gate_and_says_why_nothing_was_predicted(
    connected, window, config, tmp_path, monkeypatch
) -> None:
    """The gate is optional by contract: a window without one must still feed."""
    with_checkpoint(config, tmp_path)
    connected.refresh()
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: False)
    monkeypatch.delattr(window, "ensure_torch")

    do_feed(connected, window)

    assert window.broker.slots == [0]
    assert connected._last_cropped is not None
    assert connected.status_label.text() == NO_TORCH_TEXT
    assert connected.prediction_label.text() == ""


def test_the_offer_is_made_through_the_windows_gate_once_per_session(
    connected, window, config, tmp_path, monkeypatch
) -> None:
    with_checkpoint(config, tmp_path)
    connected.refresh()
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: False)
    calls: list[dict[str, Any]] = []

    def ensure_torch(proceed, *, reason=None, on_cancel=None):
        calls.append({"reason": reason, "proceed": proceed, "on_cancel": on_cancel})
        return False  # the install dialog is now open; the caller must return

    window.ensure_torch = ensure_torch

    connected.feed()

    assert len(calls) == 1
    assert "PyTorch" in (calls[0]["reason"] or "")
    # A cancel re-enters the feed, which is what makes "no thanks" stick.
    assert calls[0]["on_cancel"] is not None
    assert window.broker.slots == []  # the feed did not happen

    do_feed(connected, window)

    assert len(calls) == 1  # asked once, never again this session
    assert window.broker.slots == [0]


def test_a_gate_without_on_cancel_is_still_called(connected, window, config, tmp_path, monkeypatch) -> None:
    with_checkpoint(config, tmp_path)
    connected.refresh()
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: False)
    seen: list[str | None] = []

    def ensure_torch(_proceed, *, reason=None):
        seen.append(reason)
        return True  # torch turned out to be there after all

    window.ensure_torch = ensure_torch

    do_feed(connected, window)

    assert len(seen) == 1
    assert window.broker.slots == [0]


def test_no_offer_without_a_checkpoint_to_predict_with(connected, window, config, monkeypatch) -> None:
    """An untrained model is the normal path here, not a reason to prompt."""
    activate(config, {"9mm FC": 1})
    connected.refresh()
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: False)
    window.ensure_torch = lambda *_a, **_k: pytest.fail("prompted with nothing to predict")

    do_feed(connected, window)

    assert connected.status_label.text() == CAPTURED_TEXT


def test_a_prediction_prefills_the_label_field(connected, window, config, tmp_path, monkeypatch) -> None:
    with_checkpoint(config, tmp_path)
    connected.refresh()
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: True)
    monkeypatch.setattr("sorter.ml.local_inference.classify", lambda *_a, **_k: ("9mm FC", 91.5))
    monkeypatch.setattr("sorter.ml.local_inference.current_device_label", lambda: "cpu")

    do_feed(connected, window)

    assert connected.label_combo.currentText() == "9mm FC"
    assert "91.5" in connected.prediction_label.text()
    assert connected.status_label.text() == PREDICTED_TEXT
    assert connected.predict_label.text().endswith("cpu")


def test_a_failed_prediction_still_keeps_the_capture(connected, window, config, tmp_path, monkeypatch) -> None:
    with_checkpoint(config, tmp_path)
    connected.refresh()
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: True)

    def boom(*_a, **_k):
        raise RuntimeError("checkpoint is corrupt")

    monkeypatch.setattr("sorter.ml.local_inference.classify", boom)

    do_feed(connected, window)

    assert connected._last_cropped is not None
    assert "checkpoint is corrupt" in connected.status_label.text()


# ----- mode / ownership ------------------------------------------------------


def test_ai_config_mode_empties_the_page(page, window, config) -> None:
    activate(config, {"9mm FC": 1})
    page.refresh()
    assert page.counts()

    SettingsRepo(config.db).clear_active_model()
    window.bus.post("mode/changed", {"active_model_id": None})
    assert drain_until(window, lambda: page.counts_list.count() == 0)

    assert "AI Config" in page.active_label.text()
    assert not page.train_button.isEnabled()


def test_ai_config_mode_explains_why_training_is_unavailable(page, window, config) -> None:
    """The Train button is always in the sidebar now, so a click in AI Config
    mode has to land somewhere that says why (JL)."""
    assert not page.is_available()
    assert page.unavailable_title.text() == UNAVAILABLE_TITLE_AI
    assert "over HTTP" in page.unavailable_text.text()
    assert "Models page" in page.unavailable_text.text()

    activate(config, {"9mm FC": 1})
    window.bus.post("mode/changed", {"active_model_id": 1})

    assert drain_until(window, page.is_available)


def test_a_community_model_explains_the_export_import_route(page, window, config) -> None:
    activate(config, {"9mm FC": 1}, name="Someone else's", model_type="CommunityManaged")
    page.refresh()

    assert not page.is_available()
    assert page.unavailable_title.text() == UNAVAILABLE_TITLE_FOREIGN
    assert page.unavailable_text.text() == FOREIGN_MODEL_TEXT.format(name="Someone else's")


def test_the_explainers_button_jumps_to_the_models_page(page, window) -> None:
    assert not page.is_available()

    page.unavailable_button.click()

    assert window.pages.currentWidget() is window._pages_by_name["Models"]
    assert window.sidebar_buttons["Models"].isChecked()


def test_mode_changed_refreshes_the_active_model(page, window, config) -> None:
    activate(config, {"9mm FC": 1}, name="Range brass")
    window.bus.post("mode/changed", {"active_model_id": None})

    assert drain_until(window, lambda: "Range brass" in page.active_label.text())


def test_a_community_model_cannot_be_trained_here(page, window, config) -> None:
    activate(config, {"9mm FC": 1}, model_type="CommunityManaged")
    page.refresh()

    assert not page.train_button.isEnabled()

    page.start_training()

    assert window.notify.titles == ["Community model"]


def test_training_refuses_without_images(page, window, config) -> None:
    activate(config, {"9mm FC": 1})
    page.refresh()

    page.start_training()

    assert window.notify.titles == ["No images"]
    assert page.progress_dialog is None


# ----- the training-config dialog --------------------------------------------


def test_config_dialog_round_trips_a_training_config(qapp) -> None:
    config = TrainingConfig(epochs=7, batch_size=8, learning_rate=0.0005, use_swa=True, swa_mode="adaptive")
    dialog = TrainingConfigDialog(config)

    values = dialog.values()
    assert values["epochs"] == 7
    assert values["batch_size"] == 8
    assert values["learning_rate"] == pytest.approx(0.0005)
    assert values["use_swa"] is True
    assert values["swa_mode"] == "adaptive"

    dialog.fields["epochs"].setValue(42)
    dialog.fields["use_focal_loss"].setChecked(True)
    dialog.fields["swa_mode"].setCurrentText("scheduled")
    dialog.save()

    # Edited in place — the caller only has to persist the model row.
    assert config.epochs == 42
    assert config.use_focal_loss is True
    assert config.swa_mode == "scheduled"
    assert config.batch_size == 8  # untouched fields survive
    dialog.deleteLater()


# The trainer's argv flag -> the TrainingConfig field behind it. Anything the
# subprocess is handed has to be reachable from this dialog.
ARGV_TO_FIELD = {
    "--epochs": "epochs",
    "--batch_size": "batch_size",
    "--lr": "learning_rate",
    "--weight_decay": "weight_decay",
    "--dropout": "dropout_rate",
    "--val_split": "val_split",
    "--imgsize": "image_size",
    "--max_workers": "max_workers",
    "--stochastic_depth_prob": "stochastic_depth_prob",
    "--focal_gamma": "focal_gamma",
    "--swa_start": "swa_start",
    "--swa_mode": "swa_mode",
    "--swa_acc_threshold": "swa_acc_threshold",
    "--swa_patience": "swa_patience",
    "--swa_min_epoch": "swa_min_epoch",
    "--trainall": "train_all",
    "--freeze_backbone": "freeze_backbone",
    "--use_focal_loss": "use_focal_loss",
    "--use_swa": "use_swa",
}


def test_config_dialog_covers_every_field_the_trainer_takes(qapp) -> None:
    from sorter.training.manager import TrainingJob, build_command

    # Every optional flag on, so the argv is the widest the manager can build.
    config = TrainingConfig(train_all=True, freeze_backbone=True, use_focal_loss=True, use_swa=True)
    job = TrainingJob(image_dir=Path("images"), output_model=Path("out.pth"), config=config)
    argv = set(build_command(job))
    dialog = TrainingConfigDialog(TrainingConfig())

    missing = {flag for flag in argv & set(ARGV_TO_FIELD) if ARGV_TO_FIELD[flag] not in dialog.fields}
    assert not missing
    # model_name is the model's ConvNeXt mode, set from the model row, not here.
    assert argv & set(ARGV_TO_FIELD) == set(ARGV_TO_FIELD)
    dialog.deleteLater()


def test_config_dialog_clamps_out_of_range_values(qapp) -> None:
    dialog = TrainingConfigDialog(TrainingConfig())
    ranges = {key: (lo, hi) for key, _label, lo, hi, _step, _decimals in NUMERIC_FIELDS}

    dialog.fields["epochs"].setValue(10_000)
    dialog.fields["max_workers"].setValue(-99)

    assert dialog.fields["epochs"].value() == ranges["epochs"][1]
    assert dialog.fields["max_workers"].value() == ranges["max_workers"][0]
    dialog.deleteLater()


def test_training_settings_persist_to_the_model_row(page, config, monkeypatch) -> None:
    model_id = activate(config, {"9mm FC": 1})
    page.refresh()
    # exec() would block; the dialog is driven directly instead.
    monkeypatch.setattr(TrainingConfigDialog, "exec", lambda self: self.fields["epochs"].setValue(33) or self.save())

    page.open_training_settings()

    stored = ModelRepo(config.db).get(model_id)
    assert stored is not None and stored.training_config.epochs == 33


# ----- a real training run ---------------------------------------------------


@pytest.fixture
def trainable(page, window, config, monkeypatch):
    """An active model with one image on disk and torch declared present."""
    model_id = activate(config, {"9mm FC": 1})
    images = paths.model_images_dir(model_id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "9mm FC__1.jpg").write_bytes(b"x")
    monkeypatch.setattr("sorter.ml.local_inference.is_installed", lambda: True)
    page.refresh()
    return model_id


def test_training_run_reaches_the_dialog_and_adopts_the_checkpoint(trainable, page, window, config, tmp_path) -> None:
    page.training_script = write_stub_trainer(
        tmp_path,
        """
        open(arg("--output_model"), "wb").write(b"checkpoint")
        emit("start", epochs=2, classes=1, images=1, device="cpu", model="convnext_tiny")
        emit("epoch", epoch=1, total=2, train_loss=0.5, train_acc=0.7, val_acc=0.6)
        emit("epoch", epoch=2, total=2, train_loss=0.3, train_acc=0.9, val_acc=0.8)
        print("plain stdout line")
        emit("done", best_val_acc=0.8, best_val_loss=0.2)
        """,
    )

    page.start_training()
    dialog = page.progress_dialog
    assert dialog is not None
    assert drain_until(window, lambda: dialog.finished_run)

    console = dialog.console_text()
    assert "[start]" in console and "plain stdout line" in console
    assert dialog.progress.maximum() == 2
    assert dialog.progress.value() == 2
    assert dialog.final_payload is not None and dialog.final_payload["best_val_acc"] == 0.8
    assert not dialog.cancel_button.isEnabled()
    assert dialog.close_button.isEnabled()

    # The "done" marker is printed a beat before the process actually exits;
    # a user closes the console well after that, so the test does too.
    assert page.training_manager.wait(timeout=15)
    dialog.close()

    stored = ModelRepo(config.db).get(trainable)
    assert stored is not None
    assert stored.model_path == str(paths.model_trained_path(trainable))
    assert stored.last_training_date
    assert page.progress_dialog is None
    assert page.train_button.isEnabled()


def test_a_failed_run_leaves_the_model_alone(trainable, page, window, config, tmp_path) -> None:
    page.training_script = write_stub_trainer(
        tmp_path,
        """
        sys.stderr.write("boom" + chr(10))
        sys.exit(3)
        """,
    )

    page.start_training()
    dialog = page.progress_dialog
    assert dialog is not None
    assert drain_until(window, lambda: dialog.finished_run)

    assert "rc=3" in dialog.status_label.text()
    assert "boom" in dialog.console_text()

    dialog.close()

    stored = ModelRepo(config.db).get(trainable)
    assert stored is not None and not stored.model_path


def test_cancel_terminates_the_subprocess(trainable, page, window, tmp_path) -> None:
    page.training_script = write_stub_trainer(
        tmp_path,
        """
        emit("start", epochs=99, classes=1, images=1)
        time.sleep(120)
        """,
    )

    page.start_training()
    dialog = page.progress_dialog
    assert dialog is not None
    assert drain_until(window, lambda: "Training" in dialog.status_label.text())

    dialog.cancel()

    assert page.training_manager.wait(timeout=15)
    assert drain_until(window, lambda: dialog.finished_run)
    assert dialog.status_label.text() == "Cancelled."
    assert not page.training_manager.is_running


def test_closing_the_console_mid_run_asks_first(trainable, page, window, tmp_path) -> None:
    page.training_script = write_stub_trainer(
        tmp_path,
        """
        emit("start", epochs=99, classes=1, images=1)
        time.sleep(120)
        """,
    )

    page.start_training()
    dialog = page.progress_dialog
    assert dialog is not None
    assert drain_until(window, lambda: "Training" in dialog.status_label.text())

    dialog.confirm_close = lambda: False
    dialog.close()

    assert page.training_manager.is_running
    assert page.progress_dialog is dialog

    dialog.confirm_close = lambda: True
    dialog.close()

    assert page.training_manager.wait(timeout=15)
    assert page.progress_dialog is None


def test_a_second_run_is_refused_while_one_is_live(trainable, page, window, tmp_path) -> None:
    page.training_script = write_stub_trainer(
        tmp_path,
        """
        emit("start", epochs=99, classes=1, images=1)
        time.sleep(120)
        """,
    )

    page.start_training()
    dialog = page.progress_dialog
    assert dialog is not None
    assert drain_until(window, lambda: "Training" in dialog.status_label.text())

    assert not page.train_button.isEnabled()
    page.start_training()

    assert window.notify.titles == ["Training in progress"]

    page.training_manager.cancel()
    assert page.training_manager.wait(timeout=15)
    dialog.confirm_close = lambda: True
    dialog.close()


def test_spawn_failure_reports_and_closes_the_console(trainable, page, window, monkeypatch) -> None:
    """A process that never starts must not leave a console with nothing to say."""

    def no_process(*_args: Any, **_kwargs: Any):
        raise OSError("no interpreter")

    monkeypatch.setattr("sorter.training.manager.subprocess.Popen", no_process)

    page.start_training()

    assert window.notify.titles == ["Spawn failed"]
    assert page.progress_dialog is None
