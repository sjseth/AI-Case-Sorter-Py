"""Cross-cutting demo paths: the journeys a live demo actually walks.

Every other module in this directory pins one page, dialog or widget. This one
wires whole journeys through the real layers — real SQLite ``Config``, real
``EventBus``, real ``RunController``, the in-process serial emulator — and
asserts what a demo *shows*: cases landing in the right bins, the counter
climbing, the history and serial docks filling, settings surviving a restart, a
model built from nothing and activated, F1 landing on the right topic.

Rules this file keeps (PLAN, increment 16):

* **Drive the widgets.** Connecting means picking the port in Settings → Serial
  and clicking Connect, not calling ``connect_serial``; a journey that skips
  the button proves only the button-less path.
* **No sleeps.** ``drain_until`` pumps the bus until the assertion holds.
* **Nothing modal.** ``notify``/``confirm``/``ask_*`` are replaced, and the
  dialogs the shell opens with ``exec()`` are scripted (see ``script_dialog``):
  the real dialog is built and its real widgets are driven — only the modal
  event loop, the one thing an offscreen run can't have, is replaced.
* ``CASESORTER_DATA_DIR`` is redirected autouse — these journeys write model
  directories, checkpoints and archives.

The only stand-ins are the two things a test box doesn't have: the camera
(a synthetic frame) and the classifier (``classify_active``, patched at its
seam in ``sorter.ml.classifier``, plus ``local_inference.is_installed`` so the
PyTorch gate answers the same on every machine). Everything between them —
crop, routing, the confidence floor, the board protocol, persistence — is the
app itself.
"""

from __future__ import annotations

import itertools
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from sorter import paths
from sorter.control.run_controller import RunController
from sorter.data.config import Config
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, HeadstampRepo, ModelRepo, SettingsRepo
from sorter.hardware.serial_emulator import EMULATED_PORT, EmulatorBroker
from sorter.ml import classifier, local_inference
from sorter.ui import settings_camera
from sorter.ui.dialog_headstamps import HeadstampManagerDialog
from sorter.ui.dialog_model_editor import ModelEditorDialog
from sorter.ui.dialog_slot_assign import SlotAssignDialog
from sorter.ui.dialog_template import NewTemplateDialog
from sorter.ui.models_page import ACTIVE_MARK, COLUMNS
from sorter.ui.slot_grid import EMPTY_HINT

from .conftest import drain_until, seed_model

CAMERA_METADATA = [
    {"index": 0, "name": "USB Webcam", "resolutions": [(640, 480), (1280, 720)]},
    {"index": 1, "name": "Bench cam", "resolutions": [(320, 240), (800, 600)]},
]


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    """Every path these journeys touch stays inside the test's tmp dir."""
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


class Recorder:
    """Stand-in for ``win.notify`` — a real modal would hang an offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]


class FakeCamera:
    """Stand-in for ``hardware.camera.Camera`` in the Settings → Camera apply path."""

    def __init__(self, device_index: int, width: int, height: int) -> None:
        self.device_index = device_index
        self.width = width
        self.height = height

    def start_preview(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def stub_camera(window: Any) -> None:
    """A camera that always hands back one synthetic frame, never a device."""
    frame = np.zeros((480, 640, 3), np.uint8)
    window.camera = types.SimpleNamespace(
        capture_frame=lambda: frame,
        latest_frame=lambda: None,
        stop=lambda: None,
    )


def script_dialog(monkeypatch, dialog_class: Any, script: Callable[[Any], Any]) -> None:
    """Replace one dialog class's modal loop with a scripted drive of its widgets."""

    def _exec(dialog: Any) -> int:
        script(dialog)
        return int(dialog.result())

    monkeypatch.setattr(dialog_class, "exec", _exec)


def open_settings(window: Any, name: str) -> Any:
    """Select a Settings section by name and hand back the page it revealed."""
    items = window.settings_list.findItems(name, Qt.MatchFlag.MatchExactly)
    assert items, f"no Settings section named {name!r}"
    window.settings_list.setCurrentItem(items[0])
    return window.settings_pages.currentWidget()


def card_counts(window: Any) -> dict[int, int]:
    return {slot: int(card.count_label.text()) for slot, card in window.slot_grid.cards.items()}


def give_checkpoint(config: Any, model_id: int) -> None:
    """A trained-model file on disk, so ``checkpoint_problem`` clears for real."""
    trained = paths.model_trained_dir(model_id)
    trained.mkdir(parents=True, exist_ok=True)
    checkpoint = trained / f"{model_id}.pth"
    checkpoint.write_bytes(b"not-really-a-checkpoint")
    repo = ModelRepo(config.db)
    model = repo.get(model_id)
    assert model is not None
    model.model_path = str(checkpoint)
    repo.update(model)


def help_block(window: Any) -> str:
    """The heading the guide dock scrolled to (what ``show_topic`` moves)."""
    return window.help_view.browser.textCursor().block().text()


# ----- (a) a full sorting session ---------------------------------------------


def test_demo_a_full_sorting_session(config, window_factory, monkeypatch) -> None:
    """Connect the board, start, watch cases land, stop — the core demo.

    Nothing between the camera and the board is faked: the emulator answers the
    real protocol, ``RunController`` runs its real loop on its own thread, and
    every number asserted here is one an operator reads off the screen.
    """
    model_id = seed_model(config, {"9mm FC": 2, ".223 LC": 3})
    give_checkpoint(config, model_id)
    monkeypatch.setattr(local_inference, "is_installed", lambda: True)
    labels = itertools.cycle(("9mm FC", ".223 LC"))
    monkeypatch.setattr(classifier, "classify_active", lambda *_a, **_k: (next(labels), 96.0))

    window = window_factory(config)
    window.notify = Recorder()
    stub_camera(window)  # the controller captures the camera it is built with

    # 1. Connect the emulator the way a user does: Settings → Serial, pick the
    #    port, press Connect.
    window.sidebar_buttons["Settings"].click()
    serial_page = open_settings(window, "Serial")
    serial_page.port_combo.setCurrentText(EMULATED_PORT)
    serial_page.connect_button.click()

    assert isinstance(window.broker, EmulatorBroker)
    assert isinstance(window.run_controller, RunController)
    assert "connected" in window.serial_label.text()

    # 2. Back to the dashboard: the cards already show what routes where.
    window.sidebar_buttons["Sort"].click()
    assert window.slot_grid.cards[2].names_label.text() == "9mm FC"
    assert window.slot_grid.cards[3].names_label.text() == ".223 LC"
    assert window.run_button.isEnabled()
    assert window.run_button.text() == "Start"

    # 3. Run several cases. The one button turns into Stop as the run starts.
    window.run_button.click()
    assert drain_until(window, lambda: window._is_running)
    assert window.run_button.text() == "Stop"
    assert window.run_button.objectName() == "danger"
    assert not window.action_buttons["Manual feed"].isEnabled()
    assert drain_until(window, lambda: window._master_count >= 4, timeout_s=30), "the run never sorted four cases"

    # 4. Stop cleanly — same button, its other face.
    window.run_button.click()
    assert drain_until(window, lambda: not window._is_running), "the run never stopped"
    assert window.run_button.text() == "Start"
    assert window.run_button.objectName() == "action"

    # What the demo shows, in the order a viewer reads it.
    sorted_cases = window._master_count
    counts = card_counts(window)
    assert window.master_count_label.text() == str(sorted_cases)
    assert sum(counts.values()) == sorted_cases, "the cards and the master counter disagree"
    assert counts[2] > 0 and counts[3] > 0, f"both configured slots should have filled: {counts}"
    assert counts[0] == 0, "nothing was below the floor, so the catch-all stays empty"

    # The current case, front and centre: its crop, its label, its confidence.
    assert window.result_label.text() in ("9mm FC", ".223 LC")
    assert window.result_confidence_label.text() == "96%"
    assert not window.crop_label.pixmap().isNull(), "the last cropped headstamp is never shown"

    # The history dock saw every classification (it is posted before the sort
    # completes, so it can only ever lead the counter).
    entries = window.history_view._entries
    assert len(entries) >= sorted_cases
    assert entries[0].label_label.text() in ("9mm FC", ".223 LC")
    assert entries[0].confidence_label.text() == "96%"
    assert entries[0].slot_label.text() in ("Slot 2", "Slot 3")

    # The serial dock saw the exchange: the prime, the per-case sort commands,
    # the board's acks, and the stop that ended the run.
    log = window.serial_monitor.output.toPlainText()
    assert "-> xf:0" in log, "the run never primed the wheel"
    assert "-> 2" in log or "-> 3" in log
    assert "<- done" in log
    assert "-> stop" in log, "Stop never reached the board"

    assert window.notify.calls == [], f"a clean run should warn about nothing: {window.notify.calls}"

    # Counts survive Stop (jam-clearing): only an explicit reset
    # clears them, and that one also zeroes the run's package batches.
    window.reset_counts()
    assert card_counts(window) == dict.fromkeys(counts, 0)
    assert window.master_count_label.text() == "0"


# ----- (b) settings round-trip across a restart -------------------------------


def test_demo_b_settings_survive_a_restart(config, window_factory, monkeypatch) -> None:
    """Change one thing on every settings surface, relaunch, find them all.

    The second window is a *different* ``Config`` over the same database, so
    what is asserted is what reached SQLite — not what an in-memory section
    happens to still hold.
    """
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})
    monkeypatch.setattr(settings_camera, "list_cameras_with_metadata", lambda: CAMERA_METADATA)
    monkeypatch.setattr(settings_camera, "Camera", FakeCamera)

    window = window_factory(config)
    window.notify = Recorder()

    # Camera: detect, pick the second device and its smallest resolution, apply.
    window.sidebar_buttons["Settings"].click()
    camera_page = open_settings(window, "Camera")
    camera_page.detect_button.click()
    assert drain_until(window, lambda: camera_page.device_combo.count() == len(CAMERA_METADATA))
    camera_page.device_combo.setCurrentIndex(1)
    camera_page.resolution_combo.setCurrentIndex(0)  # 320 x 240
    camera_page.apply_button.click()

    # Serial: a board init value, the startup push, and the slot count (which
    # is also how many cards the dashboard draws).
    serial_page = open_settings(window, "Serial")
    serial_page.slot_count_spin.setValue(5)
    serial_page._init_widgets["feedspeed"].setValue(123)
    serial_page.init_on_startup_check.setChecked(True)

    # Image processing: a Hough bound and the primer mask.
    imageproc_page = open_settings(window, "Image Processing")
    imageproc_page.min_radius_spin.setValue(111)
    imageproc_page.primer_mode_combo.setCurrentIndex(0)  # "None"

    # Run options: the confidence floor, package mode and its batch size.
    window.sidebar_buttons["Sort"].click()
    window.floor_spin.setValue(77)
    window.package_check.setChecked(True)
    window.batch_spin.setValue(25)
    window.close()

    # Relaunch on the same database.
    restarted = window_factory(Config(config.db).load())

    assert restarted.camera.device_index == 1
    assert (restarted.camera.width, restarted.camera.height) == (320, 240)
    restarted.sidebar_buttons["Settings"].click()
    assert open_settings(restarted, "Camera").resolution_combo.currentText() == "320 x 240"

    serial_again = open_settings(restarted, "Serial")
    assert serial_again.slot_count_spin.value() == 5
    assert serial_again._init_widgets["feedspeed"].value() == 123
    assert serial_again.init_on_startup_check.isChecked()
    assert sorted(restarted.slot_grid.cards) == [0, 1, 2, 3, 4], "the slot count didn't reach the grid"

    imageproc_again = open_settings(restarted, "Image Processing")
    assert imageproc_again.min_radius_spin.value() == 111
    assert imageproc_again.primer_mode_combo.currentData() == "none"

    assert restarted.floor_spin.value() == 77
    assert restarted.package_check.isChecked()
    assert restarted.batch_spin.value() == 25

    # And the same answers read straight out of a third Config.
    stored = Config(config.db).load()
    assert stored.camera["width"] == 320 and stored.camera["device_index"] == 1
    assert stored.serial["init_settings"]["feedspeed"] == 123
    assert stored.serial["slot_quantity"] == 5
    assert stored.image_proc["hough"]["min_radius"] == 111
    assert stored.image_proc["primer_mode"] == "none"
    assert stored.run_confidence_floor == 77
    assert stored.run_package_mode is True
    assert stored.run_package_size == 25


# ----- (c) a model's whole life -----------------------------------------------


def test_demo_c_model_lifecycle(config, window, monkeypatch, tmp_path) -> None:
    """Cartridge → model → headstamps → slots → export → import → activate.

    Every step goes through the page or dialog that owns it; the archive
    written here is a real ZIP read back through ``model_io``.
    """
    page = window.models_page
    window.notify = Recorder()
    page.confirm = lambda _title, _text: True
    page.ask_import_choice = lambda _name: pytest.fail("a fresh archive must not prompt to update")

    # 1. A cartridge and a model, both through the page's own prompts.
    page.ask_text = lambda _title, _label: "9x19"
    page.new_cartridge()
    assert "9x19" in [c.name for c in CartridgeRepo(config.db).list()]

    def fill_editor(dialog: Any) -> None:
        dialog.notify = lambda title, text: pytest.fail(f"{title}: {text}")
        dialog.name_edit.setText("Demo model")
        dialog.cartridge_combo.setCurrentText("9x19")
        dialog.mode_combo.setCurrentText("ConvNeXt-Small")
        dialog.save()

    script_dialog(monkeypatch, ModelEditorDialog, fill_editor)
    page.new_model()

    created = next(m for m in ModelRepo(config.db).list() if m.name == "Demo model")
    assert created.model_mode == "convnext_small"

    # 2. Activate it: the Train activity un-mutes, the library marks the row.
    activate_row(page, created.id)
    assert drain_until(window, lambda: not window.sidebar_buttons["Train"].property("unavailable"))
    assert page.tree.currentItem().text(COLUMNS.index("Active")) == ACTIVE_MARK
    assert SettingsRepo(config.db).get_active_model_id() == created.id

    # 3. Headstamps, through the manager the Models page opens.
    def add_headstamps(dialog: Any) -> None:
        dialog.notify = lambda title, text: pytest.fail(f"{title}: {text}")
        for name in ("9mm FC", ".223 LC"):
            dialog.name_edit.setText(name)
            dialog.add_button.click()

    script_dialog(monkeypatch, HeadstampManagerDialog, add_headstamps)
    page.buttons["Headstamps…"].click()
    assert sorted(h.name for h in HeadstampRepo(config.db).list_for_model(created.id)) == [".223 LC", "9mm FC"]

    # 4. Route one of them, through the slot card's own editor.
    script_dialog(monkeypatch, SlotAssignDialog, lambda dialog: dialog.checkboxes["9mm FC"].click())
    window.sidebar_buttons["Sort"].click()
    window.open_slot_editor(1)
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"

    # 5. Export the model, then import the archive back as a second copy.
    window.sidebar_buttons["Models"].click()
    archive = tmp_path / "demo-model.zip"
    select_model(page, created.id)
    page.ask_save_path = lambda _title, _name: str(archive)
    page.buttons["Export…"].click()
    assert drain_until(window, archive.exists), "the export worker never finished"

    # Import is a filter-bar button with no handle on the page, so it is called
    # by name; everything it goes on to ask is a replaced hook.
    page.ask_open_path = lambda _title: str(archive)
    page.import_archive()
    assert drain_until(window, lambda: "Import complete" in window.notify.titles)

    copies = [m for m in ModelRepo(config.db).list() if m.name.startswith("Demo model")]
    assert len(copies) == 2, f"the import should have added a second model: {[m.name for m in copies]}"
    imported = next(m for m in copies if m.id != created.id)
    # The WinForms-compatible archive carries headstamp *names*, not their
    # slots (model_io: `headstamp_repo.add(saved.id, hs_name)`), so a fresh
    # import always lands unrouted — worth knowing before demoing one.
    assert {h.name: h.slot for h in HeadstampRepo(config.db).list_for_model(imported.id)} == {
        "9mm FC": 0,
        ".223 LC": 0,
    }

    # 6. Activate the copy: the dashboard follows the active model, so the
    #    slot the original had routed is empty again.
    activate_row(page, imported.id)
    assert drain_until(window, lambda: SettingsRepo(config.db).get_active_model_id() == imported.id)
    assert window.slot_grid.cards[1].names_label.text() == EMPTY_HINT
    assert not window.sidebar_buttons["Train"].property("unavailable")
    # ...and AI Config, its mirror, is the muted half of the pair.
    assert window.sidebar_buttons["AI Config"].property("unavailable") is True

    # 7. Back to AI Config mode: the pair swaps which one is live. Neither is
    #    ever hidden (JL: hidden activities are undiscoverable) — the muted one
    #    explains itself when clicked.
    activate_row(page, -1)  # the synthetic "Use AI Config" row
    assert drain_until(window, lambda: window.sidebar_buttons["Train"].property("unavailable"))
    assert not window.sidebar_buttons["Train"].isHidden()
    assert not window.sidebar_buttons["AI Config"].property("unavailable")
    assert SettingsRepo(config.db).get_active_model_id() is None

    window.sidebar_buttons["Train"].click()
    assert window.pages.currentWidget() is window._pages_by_name["Train"]
    assert not window.train_page.is_available()

    # And the muted AI Config, back on the imported model, lands on its own
    # page's explainer, which names the model classifying instead.
    activate_row(page, imported.id)
    assert drain_until(window, lambda: window.sidebar_buttons["AI Config"].property("unavailable") is True)
    window.sidebar_buttons["AI Config"].click()
    assert window.pages.currentWidget() is window.ai_page
    assert not window.ai_page.is_available()
    assert imported.name in window.ai_page.notice_label.text()


def select_model(page: Any, model_id: int) -> None:
    """Select a library row by model id (``-1`` is the AI Config row)."""
    index = next(i for i, (row_id, _model) in enumerate(page._rows) if row_id == model_id)
    item = page.tree.topLevelItem(index)
    assert item is not None
    page.tree.setCurrentItem(item)


def activate_row(page: Any, model_id: int) -> None:
    """Activate a model the way the library does it: select it, then Activate."""
    select_model(page, model_id)
    assert page.buttons["Activate"].isEnabled(), f"Activate is dead for row {model_id}"
    page.buttons["Activate"].click()


# ----- (c2) sorting templates carry a whole layout ----------------------------


def template_names(window: Any) -> list[str]:
    return [window.template_combo.itemText(i) for i in range(window.template_combo.count())]


def pick_template(window: Any, name: str) -> None:
    """Choose a template the way a user does: index in, ``activated`` out.

    ``setCurrentIndex`` alone doesn't emit ``activated`` (that signal is
    user-only by design, so repopulating the combo can't look like a switch),
    hence the explicit handler call.
    """
    window.template_combo.setCurrentIndex(template_names(window).index(name))
    window._on_template_selected(window.template_combo.currentIndex())


def test_demo_c2_sorting_templates_swap_the_whole_layout(config, window, monkeypatch) -> None:
    """Two bin layouts for one model, switched from the Run bar."""
    seed_model(config, {"9mm FC": 1})
    window.bus.post("mode/changed", None)
    window.bus.drain()
    assert template_names(window) == ["Default"]

    # A second layout, copied from the current one.
    def name_it(dialog: Any) -> None:
        dialog.notify = lambda title, text: pytest.fail(f"{title}: {text}")
        dialog.name_edit.setText("Match prep")
        dialog.copy_radio.setChecked(True)
        dialog.create_template()

    script_dialog(monkeypatch, NewTemplateDialog, name_it)
    window.new_template()

    assert template_names(window) == ["Default", "Match prep"]
    assert window.template_combo.currentText() == "Match prep"
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"

    # Re-route inside the new layout only.
    script_dialog(monkeypatch, SlotAssignDialog, lambda dialog: dialog.checkboxes["9mm FC"].click())
    window.open_slot_editor(4)
    assert window.slot_grid.cards[4].names_label.text() == "9mm FC"
    assert window.slot_grid.cards[1].names_label.text() == EMPTY_HINT

    # Switching back restores the layout Default was left holding — the live
    # assignments stay authoritative, so this is a real reassignment.
    pick_template(window, "Default")
    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"
    assert window.slot_grid.cards[4].names_label.text() == EMPTY_HINT
    assert config.slot_for_headstamp("9mm FC") == 1

    pick_template(window, "Match prep")
    assert window.slot_grid.cards[4].names_label.text() == "9mm FC"
    assert Config(config.db).load().slot_for_headstamp("9mm FC") == 4


# ----- (d) F1 follows the user ------------------------------------------------


def test_demo_d_help_follows_the_page_you_are_on(window) -> None:
    """The guide dock opens at the topic for wherever the user currently is."""
    guide_action = window.menus["Help"].actions()[0]
    assert guide_action.text() == "User Guide"
    assert not guide_action.shortcut().isEmpty(), "the guide has no F1 shortcut"

    window.sidebar_buttons["Sort"].click()
    guide_action.trigger()

    assert not window.help_dock.isClosed(), "F1 didn't open the guide dock"
    assert help_block(window) == "Sort dashboard"

    window.sidebar_buttons["Settings"].click()
    open_settings(window, "Serial")
    window.open_help()
    assert help_block(window) == "Serial"

    open_settings(window, "Camera")
    window.open_help()
    assert help_block(window) == "Camera"

    open_settings(window, "Image Processing")
    window.open_help()
    assert help_block(window) == "Image Processing"

    # Whole pages carry their own topic too, not just Settings sections.
    window.sidebar_buttons["Models"].click()
    window.open_help()
    assert help_block(window) == "Models"

    # An anchor the guide doesn't have still opens *something* useful — the
    # top of the guide, never an error.
    window.help_view.show_topic("a-topic-the-guide-does-not-have")
    assert "AI Case Sorter — User Guide" in help_block(window)


def test_demo_d_the_guide_dock_toggles_from_the_view_menu(window) -> None:
    toggles = {action.text(): action for action in window.menus["View"].actions() if action.text()}
    assert set(toggles) == {
        "Serial Monitor",
        "Classification History",
        "User Guide panel",
        "Themes",
        "Re-dock panels",
    }

    toggles["User Guide panel"].trigger()
    assert not window.help_dock.isClosed()

    toggles["User Guide panel"].trigger()
    assert window.help_dock.isClosed()


# ----- one journey the others can't reach: a model with no checkpoint ---------


def test_demo_e_start_refuses_a_model_that_cannot_classify(config, window_factory, monkeypatch) -> None:
    """The preflight a demo hits when the model was never trained.

    Deliberately the *whole* path: a real active model with no checkpoint on
    disk, a real emulator connection, and the real Start button — the machine
    must not feed a case.
    """
    seed_model(config, {"9mm FC": 1})
    monkeypatch.setattr(local_inference, "is_installed", lambda: True)
    window = window_factory(config)
    window.notify = Recorder()
    stub_camera(window)

    window.sidebar_buttons["Settings"].click()
    serial_page = open_settings(window, "Serial")
    serial_page.port_combo.setCurrentText(EMULATED_PORT)
    serial_page.connect_button.click()

    window.sidebar_buttons["Sort"].click()
    window.run_button.click()

    assert window.notify.titles == ["Model not ready"]
    assert not window.run_controller.is_running
    assert window.serial_monitor.output.toPlainText().count("-> xf:") == 0, "a case was fed anyway"


def test_demo_e_a_model_that_cannot_classify_still_lists_and_exports(config, window, tmp_path) -> None:
    """The library never becomes a dead end because a checkpoint is missing."""
    model = ModelRepo(config.db).create(Model(name="Untrained", cartridge_id=CartridgeRepo(config.db).list()[0].id))
    page = window.models_page
    window.notify = Recorder()
    page.refresh()
    select_model(page, model.id)

    archive = tmp_path / "untrained.zip"
    page.ask_save_path = lambda _title, _name: str(archive)
    page.buttons["Export…"].click()

    assert drain_until(window, archive.exists)
    assert window.notify.titles == ["Export complete"]
