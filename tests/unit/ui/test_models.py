"""The Models activity and its editor dialog, offscreen.

Everything runs against the real SQLite-backed ``Config`` from the shared UI
conftest, with ``CASESORTER_DATA_DIR`` pointed at ``tmp_path`` — the page reads
image counts off disk and deletes a model's directory, so an unset data root
would have the tests rummaging in the developer's own library.

The dialog hooks (``confirm``, ``ask_open_path``, ``ask_save_path``,
``ask_text``, ``ask_import_choice``) are replaced everywhere; nothing modal
opens. The export/import tests are end-to-end on purpose: a real ZIP written to
``tmp_path`` and read back through ``model_io``, no IO layer mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QHeaderView

from sorter import paths
from sorter.data.config import Config
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, HeadstampRepo, ModelRepo, SettingsRepo
from sorter.ui.dialog_model_editor import ModelEditorDialog
from sorter.ui.models_page import (
    ACTIVE_COLUMN,
    ACTIVE_MARK,
    AI_CONFIG_HINT,
    AI_CONFIG_NAME,
    AI_CONFIG_SENTINEL_ID,
    COLUMNS,
    FILTER_TYPE_COMMUNITY,
    FOREIGN_NOTICE,
    SELECT_HINT,
    ZIP_FILTER,
)
from sorter.ui.palettes import THEMES, theme_names

from .conftest import drain_until, seed_model


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Keep every path this page touches inside the test's tmp dir.

    Autouse so it lands before the ``config``/``window`` fixtures build
    anything; the assertion is the proof that it did — the page deletes model
    directories, and this suite must never reach the developer's own library.
    """
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


class _Recorder:
    """Stand-in for ``win.notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]


@pytest.fixture
def page(window):
    win_page = window.models_page
    window.notify = _Recorder()
    win_page.confirm = lambda _title, _text: True
    win_page.ask_text = lambda _title, _label: pytest.fail("unexpected text prompt")
    win_page.ask_open_path = lambda _title: pytest.fail("unexpected open dialog")
    win_page.ask_save_path = lambda _title, _name: pytest.fail("unexpected save dialog")
    win_page.ask_import_choice = lambda _name: pytest.fail("unexpected import prompt")
    return win_page


# ----- helpers ---------------------------------------------------------------


def cell(page: Any, row: int, column: str) -> str:
    return page.tree.topLevelItem(row).text(COLUMNS.index(column))


def names(page: Any) -> list[str]:
    return [cell(page, row, "Model") for row in range(page.tree.topLevelItemCount())]


def active_names(page: Any) -> list[str]:
    return [
        cell(page, row, "Model")
        for row in range(page.tree.topLevelItemCount())
        if cell(page, row, "Active") == ACTIVE_MARK
    ]


def active_colour(page: Any, model_name: str) -> QColor:
    """The Active cell's foreground on the row named ``model_name``."""
    item = page.tree.topLevelItem(names(page).index(model_name))
    return item.foreground(ACTIVE_COLUMN).color()


def select_row(page: Any, index: int) -> None:
    item = page.tree.topLevelItem(index)
    assert item is not None, f"no row {index}"
    page.tree.setCurrentItem(item)


def select(page: Any, model_id: int) -> None:
    select_row(page, next(i for i, (row_id, _model) in enumerate(page._rows) if row_id == model_id))


def select_name(page: Any, name: str) -> None:
    select_row(page, names(page).index(name))


def make_model(config: Any, name: str, **fields: Any) -> Model:
    fields.setdefault("cartridge_id", CartridgeRepo(config.db).list()[0].id)
    return ModelRepo(config.db).create(Model(name=name, **fields))


def get_model(config: Any, model_id: int | None) -> Model:
    """The row as it is on disk now — never the page's or the test's copy."""
    assert model_id is not None
    model = ModelRepo(config.db).get(model_id)
    assert model is not None
    return model


def fresh_active_id(config: Any) -> int | None:
    """The active model as a *second* reader sees it — never the page's copy."""
    return SettingsRepo(Config(config.db).load().db).get_active_model_id()


# ----- the library table -----------------------------------------------------


def test_list_shows_the_ai_row_and_the_seeded_model(page, config) -> None:
    # A fresh DB seeds one cartridge + one model and starts in AI Config mode.
    seeded = ModelRepo(config.db).list()[0]

    assert names(page) == [AI_CONFIG_NAME, seeded.name]
    assert active_names(page) == [AI_CONFIG_NAME]


def test_rows_carry_the_facts_the_tk_cards_showed(page, config) -> None:
    model = make_model(config, "Range brass", model_mode="convnext_small")
    page.refresh()

    cartridge = CartridgeRepo(config.db).get(model.cartridge_id)
    assert cartridge is not None
    row = names(page).index("Range brass")
    assert cell(page, row, "Cartridge") == cartridge.name
    assert cell(page, row, "Type") == "Standard"
    assert cell(page, row, "Mode") == "convnext_small"
    assert cell(page, row, "Images") == "0"
    assert cell(page, row, "Trained") == "no"


def test_last_trained_renders_in_the_os_regional_format(page, config, monkeypatch) -> None:
    from PySide6.QtCore import QLocale

    from sorter.ui import formatting

    monkeypatch.setattr(formatting, "_locale", lambda: QLocale("sv_SE"))
    make_model(config, "Range brass", last_training_date="2026-08-01 09:30")
    page.refresh()

    row = names(page).index("Range brass")
    # sv_SE's short format is exactly ISO/24h; a US-locale machine would
    # render "8/1/26 9:30 AM" for the same stored value — see
    # test_formatting.py for that side of the contract.
    assert cell(page, row, "Last trained") == "2026-08-01 09:30"


def test_image_count_comes_off_disk(page, config) -> None:
    model = make_model(config, "With images")
    images = paths.model_images_dir(model.id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "9mm FC__1.jpg").write_bytes(b"x")
    (images / "notes.txt").write_bytes(b"x")
    page.refresh()

    assert cell(page, names(page).index("With images"), "Images") == "1"


def test_the_active_model_is_marked(page, config) -> None:
    seed_model(config, {"9mm FC": 1}, name="Active one")
    page.refresh()

    assert active_names(page) == ["Active one"]


def test_search_filters_and_drops_the_ai_row(page, config) -> None:
    make_model(config, "Range brass")
    make_model(config, "Match prep")

    page.search_edit.setText("range")

    assert names(page) == ["Range brass"]


def test_search_for_ai_keeps_the_synthetic_row(page) -> None:
    page.search_edit.setText("ai")

    assert names(page) == [AI_CONFIG_NAME]


def test_type_filter_excludes_the_ai_row(page, config) -> None:
    make_model(config, "Shared one", community_model_uid="uid-1")

    page.type_combo.setCurrentText(FILTER_TYPE_COMMUNITY)

    assert names(page) == ["Shared one"]
    assert cell(page, 0, "Type") == "Community"


def test_cartridge_filter(page, config) -> None:
    other = CartridgeRepo(config.db).create(".223")
    ModelRepo(config.db).create(Model(name="Rifle", cartridge_id=other.id))
    page.refresh()

    page.cartridge_combo.setCurrentText(".223")

    # The AI row isn't a cartridge's model, so only the type filter hides it —
    # the ownership rule decides this, not the presence of a UID.
    assert names(page) == [AI_CONFIG_NAME, "Rifle"]


def test_zip_filter_label_survives_gnomes_paren_stripping() -> None:
    # GNOME's portal strips the "(*.zip)" Qt-pattern suffix from the label it
    # shows, so the human-readable half must carry its own, un-strippable
    # mention of the extension (JL live-testing).
    assert "*.zip" in ZIP_FILTER.split("(", 1)[0]
    assert ZIP_FILTER.endswith("(*.zip)")


# ----- column sorting ---------------------------------------------------------


def click_header(page: Any, column_name: str, window: Any = None) -> None:
    """A genuine mouse click on the header section, not ``.emit()`` on the
    signal — this is what proves click *delivery* (geometry, clickability)
    actually reaches the handler the way a real user's click does, not just
    that the handler is correct once invoked.

    Offscreen and unshown, a ``QHeaderView``'s section geometry
    (``sectionViewportPosition``) is occasionally stale on the first click
    a particular column receives — a real, shown app never has this
    problem, since painting the header at least once is unavoidable before
    a user can click it at all. Rather than a fragile "click every column
    in some order first" workaround, this verifies the click actually
    landed (against the page's own ``_sort_column``/``_sort_order`` —
    exactly what a click is supposed to change) and retries a bounded
    number of times against real state, so a genuine delivery failure still
    fails the test instead of being silently papered over.
    """
    if window is not None and window.pages.currentWidget() is not page and not window.isVisible():
        window.show()
        window.show_page("Models")
        QTest.qWait(0)
    header = page.tree.header()
    column = COLUMNS.index(column_name)

    def click_column(index: int) -> None:
        x = header.sectionViewportPosition(index) + header.sectionSize(index) // 2
        QTest.mouseClick(
            header.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(x, header.height() // 2),
        )

    prev_column, prev_order = page._sort_column, page._sort_order
    if column == prev_column:
        expected_order = (
            Qt.SortOrder.DescendingOrder if prev_order == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder
        )
    else:
        expected_order = Qt.SortOrder.AscendingOrder

    attempts = 6
    for _attempt in range(attempts):
        click_column(column)
        if page._sort_column == column and page._sort_order == expected_order:
            return
        # A neighbor click nudges the header into recomputing section
        # geometry before the retry.
        click_column(column - 1 if column else min(column + 1, len(COLUMNS) - 1))
    raise AssertionError(f"header click on {column_name!r} never registered after {attempts} attempts")


def synthetic_order(page: Any, wanted: tuple[str, ...]) -> list[str]:
    """The relative order of just the given model names among all rows."""
    shown = names(page)
    return [n for n in shown if n in wanted]


def test_images_column_sorts_numerically_not_lexically(page, window, config) -> None:
    # Lexical order would read "10" < "2" < "3"; numeric order must not.
    make_model(config, "Ten", trained_image_count=10)
    make_model(config, "Two", trained_image_count=2)
    make_model(config, "Three", trained_image_count=3)
    page.refresh()

    click_header(page, "Images", window)

    # Row 0 is the pinned AI row (blank Images cell); the rest sort numerically.
    values = [cell(page, row, "Images") for row in range(page.tree.topLevelItemCount())]
    assert values[0] == ""
    assert values[1:] == ["0", "2", "3", "10"]


def test_last_trained_column_sorts_chronologically(page, window, config) -> None:
    # The displayed text is locale-formatted (ui.formatting), which
    # doesn't sort chronologically in general — the typed sort key must be
    # the raw stored date instead.
    make_model(config, "Newer", last_training_date="2026-08-10 09:00")
    make_model(config, "Older", last_training_date="2025-01-05 09:00")
    page.refresh()

    click_header(page, "Last trained", window)

    order = names(page)
    assert order.index("Older") < order.index("Newer")


def test_the_ai_row_stays_pinned_first_under_every_sort(page, window, config) -> None:
    make_model(config, "Zed model")
    make_model(config, "Alpha model")
    page.refresh()

    for column_name in COLUMNS:
        click_header(page, column_name, window)
        assert names(page)[0] == AI_CONFIG_NAME, f"AI row wasn't first after sorting by {column_name!r}"
        # Click again to flip to descending, same assertion.
        click_header(page, column_name, window)
        assert names(page)[0] == AI_CONFIG_NAME, f"AI row wasn't first descending-sorted by {column_name!r}"


def test_every_column_sorts_and_toggles_on_a_real_header_click(page, window, config) -> None:
    """EVERY column, via genuine mouse clicks (not signal.emit()): a first
    click sorts ascending, a second click on the same header reverses it.

    Three rows, each column given genuinely distinct values, so a column
    that silently didn't sort (or only "Model" secretly worked, as JL's
    live-testing once suggested) can't hide behind ties or coincidental
    build order.
    """
    # Three distinct cartridges, not just Bravo's — CartridgeRepo.list() (and
    # so make_model's default) orders alphabetically, and "223" sorts before
    # the seeded "9mm", so leaving Alpha/Charlie on the "default" cartridge
    # here would tie them with Bravo instead of splitting from it.
    cart_a = CartridgeRepo(config.db).create("223")
    cart_b = CartridgeRepo(config.db).create("22-250")
    cart_c = CartridgeRepo(config.db).create("6.5 CM")
    make_model(
        config,
        "Bravo",
        cartridge_id=cart_a.id,
        model_mode="convnext_tiny",
        trained_image_count=5,
        last_training_date="2025-06-01 08:00",
    )
    alpha = make_model(
        config,
        "Alpha",
        cartridge_id=cart_b.id,
        community_model_uid="uid-x",
        model_mode="convnext_small",
        trained_image_count=20,
        last_training_date="2026-01-01 08:00",
        model_path="/fake/checkpoint.pth",
    )
    make_model(
        config,
        "Charlie",
        cartridge_id=cart_c.id,
        model_type="ReadOnly",
        model_mode="convnext_base",
        trained_image_count=1,
        last_training_date="2024-01-01 08:00",
    )
    SettingsRepo(config.db).set_active_model_id(alpha.id)
    page.refresh()

    wanted = ("Bravo", "Alpha", "Charlie")
    # Distinct across all three: Model/Cartridge/Type/Mode/Images/Last trained.
    for column_name in ("Model", "Cartridge", "Type", "Mode", "Images", "Last trained"):
        click_header(page, column_name, window)
        ascending = synthetic_order(page, wanted)
        assert len(ascending) == 3, f"{column_name!r}: a synthetic row went missing after sorting"

        click_header(page, column_name, window)
        descending = synthetic_order(page, wanted)
        assert descending == list(reversed(ascending)), (
            f"{column_name!r} didn't reverse on a second (real) click: {ascending} -> {descending}"
        )

    # Active/Trained are inherently binary (only Alpha is active/trained),
    # so a full reversal isn't meaningful — assert the click groups the
    # lone true row to one end, and flips ends on the second click.
    for column_name in ("Active", "Trained"):
        click_header(page, column_name, window)
        first_click = synthetic_order(page, wanted)
        click_header(page, column_name, window)
        second_click = synthetic_order(page, wanted)
        assert "Alpha" in (first_click[0], first_click[-1]), f"{column_name!r} didn't group Alpha to an end"
        assert "Alpha" in (second_click[0], second_click[-1]), f"{column_name!r} didn't group Alpha to an end"
        assert (first_click[0] == "Alpha") != (second_click[0] == "Alpha"), (
            f"{column_name!r} didn't flip ends on the second click"
        )


def test_default_order_is_unchanged_until_a_header_is_clicked(page, config) -> None:
    seeded = ModelRepo(config.db).list()[0].name
    make_model(config, "Zed model")
    make_model(config, "Alpha model")

    page.refresh()

    # Same order `_filtered()` builds (cartridge, then case-insensitive name)
    # — no sort has been requested yet.
    assert names(page) == [AI_CONFIG_NAME, "Alpha model", seeded, "Zed model"]


def test_selection_survives_a_header_click_sort(page, window, config) -> None:
    target = make_model(config, "Zed model")
    make_model(config, "Alpha model")
    page.refresh()
    select(page, target.id)

    click_header(page, "Model", window)

    assert page.selected_id() == target.id


def test_column_resizing_stays_interactive_after_sorting_is_wired(page, window) -> None:
    header = page.tree.header()
    click_header(page, "Model", window)

    for column in range(len(COLUMNS)):
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive


# ----- activation ------------------------------------------------------------


def test_activate_flips_the_setting_and_posts_mode_changed(page, window, config) -> None:
    model = make_model(config, "Range brass")
    page.refresh()
    posted: list[Any] = []
    window.bus.subscribe("mode/changed", posted.append)
    select(page, model.id)

    page.activate_selected()

    assert fresh_active_id(config) == model.id
    assert drain_until(window, lambda: posted == [{"active_model_id": model.id}])
    assert active_names(page) == ["Range brass"]
    # The window reacts to the activation event: Train is live for
    # a local model this user owns, and muted (never hidden) otherwise.
    assert not window.sidebar_buttons["Train"].property("unavailable")


def test_activating_the_ai_row_returns_to_ai_config_mode(page, window, config) -> None:
    seed_model(config, {"9mm FC": 1}, name="Local")
    page.refresh()
    select(page, AI_CONFIG_SENTINEL_ID)

    page.activate_selected()

    assert fresh_active_id(config) is None
    assert active_names(page) == [AI_CONFIG_NAME]
    assert drain_until(window, lambda: window.sidebar_buttons["Train"].property("unavailable"))


def test_activate_is_disabled_for_the_row_that_is_already_active(page, config) -> None:
    model = make_model(config, "Range brass")
    page.refresh()
    select(page, model.id)
    page.activate_selected()

    assert not page.buttons["Activate"].isEnabled()

    select(page, AI_CONFIG_SENTINEL_ID)
    assert page.buttons["Activate"].isEnabled()


def test_the_active_column_marks_exactly_one_row(page, config) -> None:
    """The column is a marker again (JL chose the bar over a per-row radio):
    "● ACTIVE" on the active row, nothing anywhere else."""
    model = make_model(config, "Range brass")
    page.refresh()
    select(page, model.id)
    page.activate_selected()

    marks = [cell(page, row, "Active") for row in range(page.tree.topLevelItemCount())]
    assert marks.count(ACTIVE_MARK) == 1
    assert active_names(page) == ["Range brass"]
    assert not any(mark for mark in marks if mark != ACTIVE_MARK)


def test_the_active_mark_is_inked_in_the_action_colour(page, window, config) -> None:
    """JL: activity should read by colour, not only by text."""
    model = make_model(config, "Range brass")
    page.refresh()
    select(page, model.id)
    page.activate_selected()

    action = QColor(window.palette_colors["action"])

    assert active_colour(page, "Range brass") == action
    # The AI row is the one that just lost the mark: it must not keep the ink.
    assert active_colour(page, AI_CONFIG_NAME) != action


def test_a_theme_switch_re_inks_the_active_mark(page, window, config) -> None:
    """An item brush is baked in, so the switch needs the explicit
    ``apply_palette`` re-render rather than the stylesheet."""
    model = make_model(config, "Range brass")
    page.refresh()
    select(page, model.id)
    page.activate_selected()
    other = next(name for name in theme_names() if THEMES[name]["action"] != window.palette_colors["action"])

    window.set_theme(other)

    assert active_colour(page, "Range brass") == QColor(THEMES[other]["action"])


def test_the_ai_row_has_no_model_actions(page) -> None:
    select(page, AI_CONFIG_SENTINEL_ID)

    assert not any(page.buttons[name].isEnabled() for name in ("Edit…", "Export…", "Delete"))


# ----- the bottom bar ---------------------------------------------------------


def test_the_bar_puts_delete_far_left_and_activate_far_right(page) -> None:
    # JL's rule: destructive alone on the left, the green primary on the far
    # right, so the two can never end up neighbours.
    assert list(page.buttons) == ["Delete", "Edit…", "Images…", "Headstamps…", "Evaluate…", "Export…", "Activate"]
    assert page.buttons["Delete"].objectName() == "danger"
    assert page.buttons["Activate"].objectName() == "action"
    assert [
        page.buttons[name].objectName() for name in ("Edit…", "Images…", "Headstamps…", "Evaluate…", "Export…")
    ] == [""] * 5


def test_the_bar_stands_down_while_an_archive_is_in_flight(page, config) -> None:
    model = make_model(config, "Range brass")
    page.refresh()
    select(page, model.id)
    scoped = ("Delete", "Edit…", "Export…", "Activate")

    page._set_busy(True)
    assert not any(page.buttons[name].isEnabled() for name in scoped)

    page._set_busy(False)
    assert all(page.buttons[name].isEnabled() for name in scoped)


def test_the_edit_button_opens_the_editor_for_the_selection(page, config, monkeypatch) -> None:
    from sorter.ui import models_page as module

    opened: list[Model | None] = []

    class StubDialog:
        def __init__(self, _db: Any, existing: Model | None = None, parent: Any = None) -> None:
            opened.append(existing)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(module, "ModelEditorDialog", StubDialog)
    target = make_model(config, "Range brass")
    make_model(config, "Match prep")
    page.refresh()
    select(page, target.id)

    page.buttons["Edit…"].click()

    assert [m.id for m in opened if m is not None] == [target.id]


def test_the_bar_still_targets_the_selected_model_after_a_sort(page, window, config) -> None:
    """``_pin_ai_row`` takes a row out of the tree and puts it back — the
    selection (and so what the bar acts on) must survive that."""
    zed = make_model(config, "Zed model")
    make_model(config, "Alpha model")
    page.refresh()

    click_header(page, "Model", window)  # ascending: Alpha, seeded, Zed
    click_header(page, "Model", window)  # descending: Zed, seeded, Alpha
    assert names(page)[0] == AI_CONFIG_NAME  # still pinned
    assert names(page)[1] == "Zed model"

    # By what's on screen, not by build order: `_rows` is pre-sort, and after
    # a header click the two disagree — which is the point of this test.
    select_name(page, "Zed model")
    page.buttons["Activate"].click()

    assert fresh_active_id(config) == zed.id
    assert active_names(page) == ["Zed model"]


# ----- ownership -------------------------------------------------------------


def test_a_community_download_is_read_only_and_not_trainable(page, config) -> None:
    make_model(config, "Someone else's", model_type="CommunityManaged", community_model_uid="uid-9")
    page.refresh()
    select_name(page, "Someone else's")

    assert cell(page, names(page).index("Someone else's"), "Type") == "Community (read-only)"
    assert page.hint_label.text() == FOREIGN_NOTICE
    assert not page.buttons["Images…"].isEnabled()
    # Everything that isn't about training the model stays available.
    assert all(page.buttons[name].isEnabled() for name in ("Edit…", "Export…", "Delete"))


def test_a_model_you_shared_yourself_stays_yours(page, config) -> None:
    # A UID means "exists in the community", not "isn't yours" (CLAUDE.md §5).
    make_model(config, "Mine", community_model_uid="uid-2")
    page.refresh()
    select_name(page, "Mine")
    page.set_images_hook(lambda _model: None)

    assert cell(page, names(page).index("Mine"), "Type") == "Community"
    assert page.buttons["Images…"].isEnabled()


def test_the_images_button_appears_only_once_a_browser_is_attached(window, config) -> None:
    # The window wires the browser at build, so exercise the module contract
    # on a fresh, unwired page.
    from sorter.ui.models_page import build_models_page

    page = build_models_page(window)
    opened: list[Any] = []
    make_model(config, "Range brass")
    page.refresh()
    select_name(page, "Range brass")

    assert page.buttons["Images…"].isHidden()

    page.set_images_hook(opened.append)
    page.images_selected()

    assert not page.buttons["Images…"].isHidden()
    assert [m.name for m in opened] == ["Range brass"]


# ----- create / edit ---------------------------------------------------------


def editor(page, existing: Model | None = None) -> tuple[ModelEditorDialog, _Recorder]:
    """The dialog plus the recorder standing in for its ``notify``."""
    dialog = ModelEditorDialog(page.db, existing=existing, parent=page)
    recorder = _Recorder()
    dialog.notify = recorder
    return dialog, recorder


def test_the_editor_creates_a_model(page, config) -> None:
    dialog, notified = editor(page)
    dialog.name_edit.setText("  Range brass  ")
    dialog.mode_combo.setCurrentText("convnext_small")
    dialog.primer_spin.setValue(120)
    dialog.hide_primer_check.setChecked(False)

    dialog.save()
    page.refresh()

    assert notified.calls == []
    saved = get_model(config, dialog.saved_id)
    assert (saved.name, saved.model_mode, saved.primer_mask_size, saved.hide_primer) == (
        "Range brass",
        "convnext_small",
        120,
        False,
    )
    assert saved.training_config.model_name == "convnext_small"
    assert "Range brass" in names(page)


def test_the_editor_edits_in_place(page, config) -> None:
    model = make_model(config, "Old name")
    other = CartridgeRepo(config.db).create(".223")

    dialog, notified = editor(page, model)
    dialog.name_edit.setText("New name")
    dialog.cartridge_combo.setCurrentText(".223")
    dialog.save()
    page.refresh()

    assert notified.calls == []
    saved = get_model(config, model.id)
    assert (saved.name, saved.cartridge_id) == ("New name", other.id)
    assert ModelRepo(config.db).list() and "Old name" not in names(page)


def test_the_editor_refuses_an_empty_name(page, config) -> None:
    before = len(ModelRepo(config.db).list())
    dialog, notified = editor(page)
    dialog.name_edit.setText("   ")

    dialog.save()

    assert dialog.saved_id is None
    assert notified.titles == ["Missing name"]
    assert len(ModelRepo(config.db).list()) == before


def test_the_feedback_box_is_only_built_for_community_models(page, config) -> None:
    plain = make_model(config, "Mine")
    linked = make_model(config, "Downloaded", model_type="CommunityManaged", community_model_uid="uid-3")

    assert not hasattr(editor(page, plain)[0], "fb_enabled_check")
    assert not hasattr(editor(page)[0], "fb_enabled_check")
    assert hasattr(editor(page, linked)[0], "fb_enabled_check")


def test_the_feedback_opt_in_saves_but_the_floor_is_the_publishers(page, config) -> None:
    model = make_model(
        config,
        "Downloaded",
        model_type="CommunityManaged",
        community_model_uid="uid-4",
        feedback_loop_confidence_floor=88,
    )
    dialog, notified = editor(page, model)
    dialog.fb_enabled_check.setChecked(True)
    dialog.fb_mode_combo.setCurrentText("On run complete")

    dialog.save()

    assert notified.calls == []
    saved = get_model(config, model.id)
    assert saved.feedback_loop_enabled
    assert saved.feedback_loop_upload_mode == "OnRunComplete"
    assert saved.feedback_loop_confidence_floor == 88


def test_new_cartridge_refuses_a_duplicate(page, config, window) -> None:
    existing = CartridgeRepo(config.db).list()[0].name
    page.ask_text = lambda _title, _label: existing

    page.new_cartridge()

    assert window.notify.titles == ["Duplicate"]
    assert len(CartridgeRepo(config.db).list()) == 1


def test_new_cartridge_adds_one(page, config) -> None:
    page.ask_text = lambda _title, _label: " .223 "

    page.new_cartridge()

    assert sorted(c.name for c in CartridgeRepo(config.db).list()) == [".223", "9mm"]
    assert ".223" in [page.cartridge_combo.itemText(i) for i in range(page.cartridge_combo.count())]


# ----- delete ----------------------------------------------------------------


def test_delete_refuses_the_last_model_in_a_cartridge(page, config, window) -> None:
    seeded = ModelRepo(config.db).list()[0]
    select(page, seeded.id)

    page.delete_selected()

    assert window.notify.titles == ["Cannot delete"]
    assert ModelRepo(config.db).get(seeded.id) is not None


def test_delete_refuses_the_active_model(page, config, window) -> None:
    model_id = seed_model(config, {"9mm FC": 1}, name="Active one")
    page.refresh()
    select(page, model_id)

    page.delete_selected()

    assert window.notify.titles == ["Cannot delete"]
    assert ModelRepo(config.db).get(model_id) is not None


def test_delete_needs_the_confirmation(page, config) -> None:
    model = make_model(config, "Doomed")
    page.refresh()
    select(page, model.id)
    page.confirm = lambda _title, _text: False

    page.delete_selected()

    assert ModelRepo(config.db).get(model.id) is not None


def test_delete_removes_the_row_and_its_directory(page, window, config) -> None:
    model = make_model(config, "Doomed")
    images = paths.model_images_dir(model.id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "9mm FC__1.jpg").write_bytes(b"x")
    page.refresh()
    select(page, model.id)

    page.delete_selected()

    assert ModelRepo(config.db).get(model.id) is None
    assert "Doomed" not in names(page)
    # The directory goes on a worker, so give it a moment to land.
    assert drain_until(window, lambda: not paths.model_dir(model.id).exists())


# ----- export / import (end to end, real archives) ---------------------------


def seed_exportable(config: Any, name: str, **fields: Any) -> Model:
    """A model with a headstamp, a training image and a checkpoint on disk."""
    model = make_model(config, name, **fields)
    HeadstampRepo(config.db).add(model.id, "9mm FC", 1)
    images = paths.model_images_dir(model.id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "9mm FC__1.jpg").write_bytes(b"jpeg-ish")
    trained = paths.model_trained_dir(model.id)
    trained.mkdir(parents=True, exist_ok=True)
    checkpoint = trained / f"{model.id}.pth"
    checkpoint.write_bytes(b"not-really-a-checkpoint")
    model.model_path = str(checkpoint)
    ModelRepo(config.db).update(model)
    return get_model(config, model.id)


def export_to(page, window, model: Model, dest: Path) -> Path:
    page.refresh()
    select(page, model.id)
    page.ask_save_path = lambda _title, _name: str(dest)
    page.export_selected()
    assert drain_until(window, dest.exists), "export worker never finished"
    return dest


def import_from(page, window, archive: Path, choice: str | None = None) -> None:
    page.ask_open_path = lambda _title: str(archive)
    page.ask_import_choice = lambda _name: choice if choice is not None else pytest.fail("unexpected prompt")
    before = len(window.notify.calls)
    page.import_archive()
    assert drain_until(window, lambda: len(window.notify.calls) > before), "import worker never finished"


def test_export_writes_a_real_archive(page, window, config, tmp_path) -> None:
    import zipfile

    model = seed_exportable(config, "Range brass")

    archive = export_to(page, window, model, tmp_path / "range.zip")

    with zipfile.ZipFile(archive) as zf:
        entries = set(zf.namelist())
    assert "manifest.json" in entries
    assert f"model/{model.id}.pth" in entries
    assert "images/9mm FC__1.jpg" in entries
    assert window.notify.titles == ["Export complete"]


def test_importing_an_installed_community_archive_updates_it_in_place(page, window, config, tmp_path) -> None:
    model = seed_exportable(config, "Shared", community_model_uid="uid-round-trip")
    archive = export_to(page, window, model, tmp_path / "shared.zip")
    # Diverge the installed row from the archive, and rename it the way a user
    # would: the update must refresh the model but keep the local name.
    model.model_version = 99
    model.name = "My copy"
    ModelRepo(config.db).update(model)
    page.refresh()

    import_from(page, window, archive, choice="update")

    installed = [m for m in ModelRepo(config.db).list() if m.community_model_uid == "uid-round-trip"]
    assert [m.id for m in installed] == [model.id], "the archive added a row instead of updating one"
    assert installed[0].model_version == 1, "the installed row wasn't refreshed from the archive"
    assert installed[0].name == "My copy", "the update overwrote the name the user gave it"
    assert names(page).count("My copy") == 1


def test_the_same_archive_can_be_imported_as_a_separate_copy(page, window, config, tmp_path) -> None:
    model = seed_exportable(config, "Shared", community_model_uid="uid-round-trip")
    archive = export_to(page, window, model, tmp_path / "shared.zip")

    import_from(page, window, archive, choice="copy")

    rows = sorted(
        (m for m in ModelRepo(config.db).list() if m.community_model_uid == "uid-round-trip"),
        key=lambda m: m.id,
    )
    assert [m.name for m in rows] == ["Shared", "Shared (2)"]
    # The copy is a model of its own: its own directory, its own checkpoint.
    copy = rows[1]
    assert copy.id != model.id
    assert copy.model_path is not None and Path(copy.model_path).exists()
    assert [h.name for h in HeadstampRepo(config.db).list_for_model(copy.id)] == ["9mm FC"]


def test_declining_the_update_prompt_imports_nothing(page, window, config, tmp_path) -> None:
    model = seed_exportable(config, "Shared", community_model_uid="uid-round-trip")
    archive = export_to(page, window, model, tmp_path / "shared.zip")
    page.ask_open_path = lambda _title: str(archive)
    page.ask_import_choice = lambda _name: "cancel"

    page.import_archive()

    assert [m.name for m in ModelRepo(config.db).list() if m.name.startswith("Shared")] == ["Shared"]


def test_the_security_notice_can_refuse_an_import(page, window, config, tmp_path) -> None:
    model = seed_exportable(config, "Shared")
    archive = export_to(page, window, model, tmp_path / "shared.zip")
    page.ask_open_path = lambda _title: str(archive)
    page.confirm = lambda _title, _text: False

    page.import_archive()

    assert [m.name for m in ModelRepo(config.db).list() if m.name.startswith("Shared")] == ["Shared"]


def test_a_plain_archive_imports_as_a_new_trainable_model(page, window, config, tmp_path) -> None:
    # No community UID: nothing to update, so no prompt — and the import is
    # the user's own model, which must stay trainable.
    model = seed_exportable(config, "Range brass")
    archive = export_to(page, window, model, tmp_path / "range.zip")

    import_from(page, window, archive)

    imported = next(m for m in ModelRepo(config.db).list() if m.name == "Range brass (2)")
    assert imported.id != model.id
    assert imported.model_type == "Standard"


def test_a_broken_archive_reports_instead_of_raising(page, window, tmp_path) -> None:
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip at all")
    page.ask_open_path = lambda _title: str(broken)

    page.import_archive()

    assert drain_until(window, lambda: window.notify.titles == ["Import failed"])


def test_restoring_saved_column_widths_keeps_headers_sortable(page, window, config) -> None:
    """Regression (JL live-testing): ``QHeaderView.restoreState`` also restores
    clickable/indicator-shown — and a blob saved by a pre-sorting build
    restores them *off*, which killed header-click sorting in the real app
    while every test (none of which restored saved state) stayed green.
    """
    from PySide6.QtWidgets import QTreeWidget

    legacy = QTreeWidget()
    legacy.setColumnCount(len(COLUMNS))
    legacy.setHeaderLabels(list(COLUMNS))
    assert not legacy.header().sectionsClickable()  # what the old build saved
    blob = bytes(legacy.header().saveState().data())

    assert page.restore_header_state(blob)

    header = page.tree.header()
    assert header.sectionsClickable()
    assert header.isSortIndicatorShown()
    seed_model(config, {"9mm FC": 1}, name="Bravo")
    seed_model(config, {"9mm FC": 1}, name="Alpha")
    page.refresh()
    click_header(page, "Model", window)
    assert names(page)[0] == "Use AI Config"  # pinned row survives the sort
    assert names(page)[1] == "Alpha"


# ----- column sizing ---------------------------------------------------------


def test_typical_values_are_not_elided_at_the_default_widths(page, config) -> None:
    # The two that clipped (JL live-testing): "8/8/26 7:06 P…" and "convnext_…".
    make_model(config, "Range brass", model_mode="convnext_small", last_training_date="2026-12-28 23:59")
    # The window's construction-time refresh already autosized (and latched
    # _columns_sized); reset so this refresh sizes for the new, longer row —
    # the same first-sight sizing a fresh launch gives it.
    page._columns_sized = False
    page.refresh()

    header = page.tree.header()
    for column in ("Mode", "Last trained"):
        index = COLUMNS.index(column)
        # Compare against the view's own computed content width, not raw font
        # advances: the delegate-vs-fontMetrics offset is platform-dependent
        # (a Linux-tuned pixel allowance failed on Windows CI). Section wider
        # than the delegate's need = nothing to elide, and strictly wider
        # proves COLUMN_PADDING was actually applied.
        assert header.sectionSize(index) > page.tree.sizeHintForColumn(index)


def test_the_user_can_still_drag_the_columns(page) -> None:
    assert page.tree.header().sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive


# ----- the AI row explains itself --------------------------------------------


def test_the_ai_row_carries_its_explanation_as_a_tooltip(page) -> None:
    row = page.tree.topLevelItem(names(page).index(AI_CONFIG_NAME))

    assert [row.toolTip(i) for i in range(len(COLUMNS))] == [AI_CONFIG_HINT] * len(COLUMNS)


def test_the_hint_line_is_left_to_what_a_row_cannot_carry(page, config) -> None:
    select(page, AI_CONFIG_SENTINEL_ID)

    assert page.hint_label.text() == SELECT_HINT  # not the AI text, twice over

    make_model(config, "Someone else's", model_type="CommunityManaged")
    page.refresh()
    select_name(page, "Someone else's")

    assert page.hint_label.text() == FOREIGN_NOTICE


def test_editor_offers_openai_and_persists_it(page) -> None:
    """ "openai" is a first-class mode in Create/Edit Model (PR #125 review) —
    the Windows app's "OpenAI API" Training Mode, imported or created here."""
    from sorter.data.models import is_trainable

    dialog, _recorder = editor(page)
    offered = [dialog.mode_combo.itemText(i) for i in range(dialog.mode_combo.count())]
    assert "openai" in offered

    dialog.name_edit.setText("HTTP model")
    dialog.mode_combo.setCurrentText("openai")
    dialog.save()

    assert dialog.saved_id is not None
    saved = ModelRepo(page.db).get(dialog.saved_id)
    assert saved is not None
    assert saved.model_mode == "openai"
    assert not is_trainable(saved)
