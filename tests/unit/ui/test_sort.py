"""The Sort dashboard: slot cards, live counts, the current case, and the run.

Everything here runs offscreen against a real SQLite-backed ``Config`` and,
where a board is needed, the in-process serial emulator — no display, no
hardware, no network.
"""

from __future__ import annotations

import types

import numpy as np
import pytest

pytest.importorskip("PySide6")

from sorter.control.run_controller import RunController
from sorter.hardware.serial_emulator import EMULATED_PORT, EmulatorBroker
from sorter.ml import classifier, local_inference
from sorter.ui.app import (
    CROP_EMPTY_TEXT,
    RESULT_EMPTY_CONFIDENCE,
    RESULT_EMPTY_TEXT,
    SETTING_SHOW_CAMERA,
)
from sorter.ui.slot_grid import CATCH_ALL_HINT, EMPTY_HINT

from .conftest import drain_until, seed_model


class FakeBroker:
    port = "COM-fake"
    baud = 9600
    firmware_version = "Fake-1.0"
    is_connected = True

    def stop(self) -> None:
        pass


class FakeController:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.cycles = 0
        self.package_resets: list[object] = []

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def cycle_once(self) -> dict:
        self.cycles += 1
        return {"ok": True, "slot": 0}

    def reset_package_counts(self) -> None:
        self.package_resets.append("all")

    def reset_package_slot(self, slot: int) -> None:
        self.package_resets.append(slot)


@pytest.fixture
def connected(window):
    """A window holding a stand-in board and controller (no run loop)."""
    controller = FakeController()
    window.broker = FakeBroker()
    window.run_controller = controller
    window._update_run_buttons()
    return controller


def history(label: str, confidence: float, slot: int = 1) -> dict:
    """A ``run/history`` payload as ``RunController._post_history`` builds it."""
    return {
        "image": np.zeros((480, 480, 3), np.uint8),
        "label": label,
        "parent": None,
        "confidence": confidence,
        "slot": slot,
    }


# ----- slot cards ------------------------------------------------------------


def test_slot_cards_cover_every_slot_including_the_catch_all(window) -> None:
    cards = window.slot_grid.cards

    assert sorted(cards) == list(range(8))  # serial.slot_quantity default
    assert cards[0].title_label.text() == "Catch-All"
    assert cards[0].names_label.text() == CATCH_ALL_HINT
    assert cards[3].title_label.text() == "Slot #3"


def test_slot_count_follows_the_serial_setting(config, window_factory) -> None:
    config.serial["slot_quantity"] = 4

    cards = window_factory(config).slot_grid.cards

    assert sorted(cards) == [0, 1, 2, 3]


def test_slot_cards_show_the_active_models_assignments(config, window_factory) -> None:
    seed_model(config, {"9mm FC": 1, ".223 LC": 2, "45 ACP": 1})

    cards = window_factory(config).slot_grid.cards

    assert cards[1].names_label.text() == "45 ACP, 9mm FC"  # case-folded sort
    assert cards[2].names_label.text() == ".223 LC"
    assert cards[3].names_label.text() == EMPTY_HINT


def test_a_card_never_shortens_its_headstamp_list(config, window_factory) -> None:
    # The card is how an operator knows what is in the bin, so the list is
    # always complete: it wraps and the card grows, never elides.
    names = [f"Headstamp {i:02d} with a long name" for i in range(12)]
    seed_model(config, dict.fromkeys(names, 1))

    grid = window_factory(config).slot_grid

    assert grid.cards[1].names_label.text() == ", ".join(names)
    assert grid.cards[1].names_label.wordWrap()
    assert grid.cards[1].sizeHint().height() > grid.cards[2].sizeHint().height()


def test_cards_advertise_that_they_are_editable(window) -> None:
    assert window.slot_grid.cards[1].edit_hint.text() == "✎ Click to edit"
    assert not window.slot_grid.cards[1].edit_hint.isHidden()
    assert window.slot_grid.cards[0].edit_hint.isHidden()  # the catch-all isn't
    assert "QFrame#slotCard:hover" in window.styleSheet()


def test_slot_cards_follow_ai_config_headstamps(config, window) -> None:
    # No active model: headstamps live in a settings key, and the grid must go
    # through Config for them exactly like the model-scoped case.
    config.add_headstamp("9mm RP", 3)

    window.slot_grid.refresh_assignments()

    assert window.slot_grid.cards[3].names_label.text() == "9mm RP"


def test_returning_to_sort_re_reads_the_assignments(config, window) -> None:
    window.sidebar_buttons["Settings"].click()
    config.add_headstamp("9mm RP", 3)

    window.sidebar_buttons["Sort"].click()

    assert window.slot_grid.cards[3].names_label.text() == "9mm RP"


def test_refresh_picks_up_a_reassignment(config, window) -> None:
    config.add_headstamp("9mm RP", 3)
    window.slot_grid.refresh_assignments()

    config.set_headstamp_slot("9mm RP", 5)
    window.slot_grid.refresh_assignments()

    assert window.slot_grid.cards[3].names_label.text() == EMPTY_HINT
    assert window.slot_grid.cards[5].names_label.text() == "9mm RP"


# ----- live counts -----------------------------------------------------------


def test_sorted_case_increments_its_card(window) -> None:
    for _ in range(2):
        window.bus.post("run/result", {"ok": True, "slot": 2, "label": "9mm"})
    window.bus.drain()

    assert window.slot_grid.cards[2].count_label.text() == "2"
    assert window.slot_grid.cards[1].count_label.text() == "0"


def test_below_floor_case_counts_against_the_catch_all(window) -> None:
    window.bus.post("run/result", {"ok": True, "slot": 0, "label": "9mm"})
    window.bus.drain()

    assert window.slot_grid.cards[0].count_label.text() == "1"


def test_failed_cycle_is_not_counted(window) -> None:
    window.bus.post("run/result", {"ok": False, "slot": 2, "error": "Sort timeout"})
    window.bus.drain()

    assert window.slot_grid.cards[2].count_label.text() == "0"


def test_master_counter_tallies_only_sorted_cases(window) -> None:
    for result in (
        {"ok": True, "slot": 2},
        {"ok": True, "slot": 0},
        {"ok": False, "slot": 3, "error": "Sort timeout"},
    ):
        window.bus.post("run/result", result)
    window.bus.drain()

    assert window.master_count_label.text() == "2"


def test_counts_survive_a_stop_start(window) -> None:
    # JL: operators stop to clear a jam and restart mid-tray;
    # only the explicit resets clear the counters.
    window.bus.post("run/result", {"ok": True, "slot": 2})
    window.bus.drain()

    window.bus.post("run/stopped", None)
    window.bus.post("run/started", None)
    window.bus.drain()

    assert window.slot_grid.cards[2].count_label.text() == "1"
    assert window.master_count_label.text() == "1"


def test_reset_counts_clears_the_cards_and_the_batches(window, connected) -> None:
    window.bus.post("run/result", {"ok": True, "slot": 2})
    window.bus.drain()

    window.reset_counts()

    assert window.slot_grid.cards[2].count_label.text() == "0"
    assert window.master_count_label.text() == "0"
    assert connected.package_resets == ["all"]


# ----- package mode ----------------------------------------------------------


def test_package_cards_show_the_batch_target(window, config) -> None:
    config.set_run_package_size(25)

    window.package_check.setChecked(True)

    assert window.slot_grid.cards[1].package_label.text() == "/ 25"
    # The catch-all is never batched.
    assert window.slot_grid.cards[0].package_label.isHidden()


def test_a_full_batch_rings_and_reports(window, monkeypatch) -> None:
    beeps = []
    monkeypatch.setattr(window, "beep", lambda: beeps.append(True))

    window.bus.post("run/package_full", {"slot": 3, "label": "9mm FC", "count": 50})
    window.bus.drain()

    assert beeps == [True]
    assert window.statusBar().currentMessage() == "Slot 3 batch full (50). Reset it to refill."


def test_a_halted_run_says_how_to_resume(qapp, window, monkeypatch) -> None:
    monkeypatch.setattr(window, "beep", lambda: None)
    notices = []
    window.notify = lambda title, text: notices.append((title, text))

    window.bus.post("run/package_halt", {"label": "9mm FC"})
    window.bus.drain()
    qapp.processEvents()  # the dialog is queued out of the drain

    message = window.statusBar().currentMessage()
    assert "9mm FC" in message
    assert "reset their counters" in message
    assert notices and notices[0][0] == "Package complete"


def test_a_cards_reset_button_empties_just_that_bin(window, connected, config) -> None:
    window.package_check.setChecked(True)
    for slot in (1, 2):
        window.bus.post("run/result", {"ok": True, "slot": slot})
    window.bus.drain()

    window.slot_grid.cards[1].reset_button.click()

    assert window.slot_grid.cards[1].count_label.text() == "0"
    assert window.slot_grid.cards[2].count_label.text() == "1"
    assert connected.package_resets == [1]


# ----- the cropped headstamp, and the call made on it ------------------------


def test_the_crop_panel_is_empty_until_a_case_is_classified(window) -> None:
    assert window.crop_label.pixmap().isNull()
    assert window.crop_label.text() == CROP_EMPTY_TEXT
    assert window.result_label.text() == RESULT_EMPTY_TEXT
    assert window.result_confidence_label.text() == RESULT_EMPTY_CONFIDENCE


def test_a_classification_fills_the_crop_panel_and_the_result_line(window) -> None:
    window.bus.post("run/history", history("9mm FC", 99.0))
    window.bus.drain()

    pixmap = window.crop_label.pixmap()
    assert not pixmap.isNull()
    # Scaled into the panel, never past it — the panel is what resizes now.
    assert pixmap.width() <= window.crop_label.width()
    assert pixmap.height() <= window.crop_label.height()
    assert window.result_label.text() == "9mm FC"
    assert window.result_confidence_label.text() == "99%"


def test_the_result_line_shows_the_current_case_only(window) -> None:
    window.bus.post("run/history", history("9mm", 99.0))
    window.bus.post("run/history", history(".223", 71.0))
    window.bus.drain()

    assert window.result_label.text() == ".223"
    assert window.result_confidence_label.text() == "71%"
    assert "9mm" not in window.result_label.text()  # nothing accumulates here


def test_the_result_line_carries_the_parent_classification(window) -> None:
    payload = history("LC 21", 99.0)
    payload["parent"] = "Lake City"
    window.bus.post("run/history", payload)
    window.bus.drain()

    assert window.result_label.text() == "Lake City · LC 21"


def test_confidence_is_colored_against_the_floor(window, config) -> None:
    config.set_run_confidence_floor(80)

    window.bus.post("run/history", history("9mm", 92.0))
    window.bus.drain()
    assert window.palette_colors["success"] in window.result_confidence_label.styleSheet()

    window.bus.post("run/history", history("unknown", 41.0))
    window.bus.drain()
    assert window.palette_colors["warning"] in window.result_confidence_label.styleSheet()


def test_the_result_line_recolors_on_a_theme_switch(window) -> None:
    window.bus.post("run/history", history("9mm", 99.0))
    window.bus.drain()
    before = window.result_confidence_label.styleSheet()

    window.set_theme("Comic Book")

    assert window.result_confidence_label.styleSheet() != before
    assert window.palette_colors["success"] in window.result_confidence_label.styleSheet()


# ----- the live camera toggle ------------------------------------------------


class RecordingCamera:
    """Counts the frame reads the preview timer would make."""

    def __init__(self) -> None:
        self.reads = 0

    def latest_frame(self):
        self.reads += 1
        return np.zeros((480, 640, 3), np.uint8)

    def stop(self) -> None:
        pass


def test_the_live_camera_is_off_by_default_and_reads_no_frames(window) -> None:
    camera = RecordingCamera()
    window.camera = camera

    assert window.show_camera_check.isChecked() is False
    assert window.preview_label.isVisibleTo(window.preview_label.parentWidget()) is False

    window._refresh_preview()

    assert camera.reads == 0


def test_turning_the_camera_on_shows_the_preview_and_reads_frames(window) -> None:
    camera = RecordingCamera()
    window.camera = camera

    window.show_camera_check.setChecked(True)

    assert window.preview_label.isVisibleTo(window.preview_label.parentWidget()) is True
    window._refresh_preview()
    assert camera.reads == 1
    assert not window.preview_label.pixmap().isNull()


def test_the_camera_toggle_survives_a_restart(window, window_factory, config) -> None:
    window.show_camera_check.setChecked(True)

    reopened = window_factory(config)

    assert reopened.show_camera_check.isChecked() is True
    assert reopened._load_setting(SETTING_SHOW_CAMERA) is True


def test_the_crop_stays_the_bigger_panel_when_the_camera_is_on(window) -> None:
    window.show_camera_check.setChecked(True)
    column = window.crop_label.parentWidget().layout()

    stretches = {
        column.itemAt(index).widget(): column.stretch(index)
        for index in range(column.count())
        if column.itemAt(index).widget() is not None
    }

    assert stretches[window.crop_label] > stretches[window.preview_label]


# ----- start preflight -------------------------------------------------------


def test_start_without_a_board_says_so_and_starts_nothing(window, monkeypatch) -> None:
    notices = []
    monkeypatch.setattr(window, "notify", lambda title, text: notices.append(title))

    window.start_run()

    assert window.run_controller is None
    assert "Settings → Serial" in window.statusBar().currentMessage()
    assert notices == []


def test_start_refuses_a_model_without_a_checkpoint(window, connected, monkeypatch) -> None:
    notices = []
    monkeypatch.setattr(window, "notify", lambda title, text: notices.append((title, text)))
    monkeypatch.setattr(classifier, "checkpoint_problem", lambda _db: "no trained model file")

    window.start_run()

    assert notices == [("Model not ready", "no trained model file")]
    assert connected.started == 0


def test_start_routes_a_local_model_without_torch_through_the_gate(window, connected, monkeypatch) -> None:
    # A local model with no torch has to reach the install gate, not a run (#7).
    gate_calls = []
    monkeypatch.setattr(window, "ensure_torch", lambda proceed, **kw: gate_calls.append(kw.get("reason")) and False)
    monkeypatch.setattr(classifier, "checkpoint_problem", lambda _db: None)
    monkeypatch.setattr(classifier, "uses_local_inference", lambda _db: True)

    window.start_run()

    assert gate_calls == ["Sorting needs PyTorch"]
    assert connected.started == 0

    # Gate satisfied (torch present) → the run starts.
    monkeypatch.setattr(window, "ensure_torch", lambda proceed, **kw: True)
    window.start_run()
    assert connected.started == 1


def test_start_refuses_ai_config_mode_without_credentials(window, connected, config, monkeypatch) -> None:
    notices = []
    monkeypatch.setattr(window, "notify", lambda title, text: notices.append(title))
    config.api["api_key"] = ""

    window.start_run()

    assert notices == ["AI not configured"]
    assert connected.started == 0


def test_a_local_model_does_not_need_ai_credentials(window, connected, config, monkeypatch) -> None:
    # The HTTP client is never reached, so an unset key is no reason to refuse.
    monkeypatch.setattr(window, "notify", lambda title, text: pytest.fail(title))
    monkeypatch.setattr(classifier, "uses_local_inference", lambda _db: True)
    monkeypatch.setattr(classifier, "checkpoint_problem", lambda _db: None)
    monkeypatch.setattr(local_inference, "is_installed", lambda: True)
    config.api["model"] = ""

    window.start_run()

    assert connected.started == 1


def test_start_runs_the_controller_in_ai_config_mode(window, connected) -> None:
    # Fresh DB = AI Config mode: nothing local to check, so no notice.
    window.start_run()

    assert connected.started == 1


def test_stop_stops_the_controller(window, connected) -> None:
    window.stop_run()

    assert connected.stopped == 1


def test_manual_feed_runs_one_cycle_off_the_main_thread(window, connected) -> None:
    window.manual_feed()

    assert drain_until(window, lambda: connected.cycles == 1)


# ----- button state ----------------------------------------------------------


def test_actions_enable_once_a_board_is_present(window, connected) -> None:
    assert window.run_button.isEnabled()
    assert window.run_button.text() == "Start"
    assert window.action_buttons["Manual feed"].isEnabled()


def test_run_events_own_the_button_state(window, connected) -> None:
    window.bus.post("run/started", None)
    window.bus.drain()

    # One button, both faces: the run's own events flip the label and the
    # palette role, never the click handler.
    assert window.run_button.text() == "Stop"
    assert window.run_button.objectName() == "danger"
    assert window.run_button.isEnabled()
    assert not window.action_buttons["Manual feed"].isEnabled()

    window.bus.post("run/stopped", None)
    window.bus.drain()

    assert window.run_button.text() == "Start"
    assert window.run_button.objectName() == "action"
    assert window.run_button.isEnabled()
    assert window.action_buttons["Manual feed"].isEnabled()


def test_a_stopped_run_replaces_the_in_flight_status(window) -> None:
    """Seth: the corner label sat on "Stopping…"/"Classifying…" after the run
    had long ended — nothing terminal ever replaced the transient."""
    window.bus.post("run/status", "Classifying…")
    window.bus.post("run/stopped", None)
    window.bus.drain()

    assert window.statusBar().currentMessage() == "Run stopped."


def test_manual_feed_progress_reaches_the_status_bar(window) -> None:
    window.bus.post("test/status", "Classifying…")
    window.bus.drain()

    assert window.statusBar().currentMessage() == "Classifying…"


def test_the_action_strip_sits_at_the_foot_of_the_sort_page(qapp, window) -> None:
    # JL: Start / Manual feed / Run options at the bottom, like the Train
    # page's Training strip — working surface first, launchers under it.
    window.show_page("Sort")
    window.resize(1200, 700)
    window.show()
    qapp.processEvents()

    assert window.run_button.geometry().top() >= window.sort_stack.geometry().bottom()


def test_the_toggle_starts_and_then_stops_the_run(window, connected) -> None:
    window.run_button.click()

    assert connected.started == 1

    # The controller's event is what puts the Stop face up; clicking it then
    # stops rather than starting a second run.
    window.bus.post("run/started", None)
    window.bus.drain()
    window.run_button.click()

    assert connected.stopped == 1
    assert connected.started == 1


def test_the_toggle_is_the_same_button_under_both_keys(window) -> None:
    assert window.action_buttons["Start/Stop"] is window.run_button


def test_run_error_reaches_the_status_bar(window) -> None:
    window.bus.post("run/error", "Sort timeout")
    window.bus.drain()

    assert window.statusBar().currentMessage() == "Run error: Sort timeout"


# ----- the active model owns the sidebar -------------------------------------


def mode_changed(window) -> None:
    window.bus.post("mode/changed", None)
    window.bus.drain()


def test_train_is_muted_but_present_in_ai_config_mode(window) -> None:
    """JL never discovered Train existed while it was hidden. It stays in the
    sidebar at all times; unavailability is ink plus an explainer, and the
    click keeps working — so `setEnabled(False)` is exactly what this isn't."""
    button = window.sidebar_buttons["Train"]

    assert not button.isHidden()
    assert button.isEnabled()
    assert button.property("unavailable") is True


def test_ai_config_sits_beside_a_muted_train_in_ai_config_mode(window) -> None:
    """Seth: "it takes the place of the training screen; it is analogous to
    training for an LLM". The two are mode-mirrors and coexist — neither is
    ever hidden, and exactly one of them is live (JL)."""
    assert not window.sidebar_buttons["AI Config"].isHidden()
    assert not window.sidebar_buttons["Train"].isHidden()
    names = list(window.sidebar_buttons)
    assert names.index("AI Config") == names.index("Train") + 1
    assert window.sidebar_buttons["AI Config"].property("unavailable") is False
    assert window.sidebar_buttons["Train"].property("unavailable") is True


def test_ai_config_goes_muted_beside_a_live_train_for_a_local_model(window, config) -> None:
    """The mirror image, and the pair still both in the sidebar."""
    seed_model(config, {"9mm FC": 1})

    mode_changed(window)

    assert not window.sidebar_buttons["AI Config"].isHidden()
    assert window.sidebar_buttons["AI Config"].property("unavailable") is True
    assert not window.sidebar_buttons["Train"].property("unavailable")


def test_a_community_model_leaves_neither_of_the_pair_live(window, config) -> None:
    """Its publisher trained it and it isn't AI Config either — so both are
    muted, and each explains its own half of that."""
    from sorter.data.repository import ModelRepo

    repo = ModelRepo(config.db)
    model = repo.get(seed_model(config, {"9mm FC": 1}))
    assert model is not None
    model.model_type = "CommunityManaged"
    repo.update(model)

    mode_changed(window)

    assert window.sidebar_buttons["Train"].property("unavailable") is True
    assert window.sidebar_buttons["AI Config"].property("unavailable") is True
    assert not window.sidebar_buttons["Train"].isHidden()
    assert not window.sidebar_buttons["AI Config"].isHidden()


def test_the_pair_tooltips_state_which_one_is_live(window, config) -> None:
    from sorter.ui.app import ACTIVITY_TOOLTIP_LIVE, AI_CONFIG_TOOLTIP_MUTED, TRAIN_TOOLTIP_MUTED

    assert window.sidebar_buttons["AI Config"].toolTip() == ACTIVITY_TOOLTIP_LIVE
    assert window.sidebar_buttons["Train"].toolTip() == TRAIN_TOOLTIP_MUTED

    seed_model(config, {"9mm FC": 1})
    mode_changed(window)

    assert window.sidebar_buttons["Train"].toolTip() == ACTIVITY_TOOLTIP_LIVE
    assert window.sidebar_buttons["AI Config"].toolTip() == AI_CONFIG_TOOLTIP_MUTED


def test_ai_config_is_a_page_of_its_own(window) -> None:
    """It left Settings (JL): the sidebar entry is a plain page navigation,
    and the button that was clicked is the one that ends up checked."""
    window.sidebar_buttons["AI Config"].click()

    assert window.pages.currentWidget() is window._pages_by_name["AI Config"]
    assert window.sidebar_buttons["AI Config"].isChecked()
    assert "AI Config" not in [window.settings_list.item(i).text() for i in range(window.settings_list.count())]


def test_a_muted_ai_config_click_lands_on_the_explainer_naming_the_active_model(window, config) -> None:
    """The mirror of Train's explainer page: the form is replaced by a panel
    saying what is classifying instead."""
    seed_model(config, {"9mm FC": 1}, name="Range brass")
    mode_changed(window)

    window.sidebar_buttons["AI Config"].click()

    page = window.ai_page
    assert window.pages.currentWidget() is page
    assert not page.is_available()
    assert "Range brass" in page.notice_label.text()
    assert "Use AI Config" in page.notice_label.text()


def test_the_explainer_jump_button_goes_to_models(window, config) -> None:
    seed_model(config, {"9mm FC": 1})
    mode_changed(window)
    window.sidebar_buttons["AI Config"].click()

    window.ai_page.models_button.click()

    assert window.pages.currentWidget() is window._pages_by_name["Models"]
    assert window.sidebar_buttons["Models"].isChecked()


def test_the_form_is_what_ai_config_mode_shows(window) -> None:
    window.sidebar_buttons["AI Config"].click()

    assert window.ai_page.is_available()
    assert window.ai_page.stack.currentWidget() is window.ai_page.section


def test_a_mode_change_leaves_the_ai_config_page_alone(window, config) -> None:
    """The explainer replaces the form in place — a forced jump to Sort would
    be a jump the user didn't ask for."""
    window.sidebar_buttons["AI Config"].click()
    seed_model(config, {"9mm FC": 1})

    mode_changed(window)

    assert not window.sidebar_buttons["AI Config"].isHidden()
    assert window.pages.currentWidget() is window._pages_by_name["AI Config"]
    assert not window.ai_page.is_available()


def test_community_follows_auth_state(window) -> None:
    # Signed out (the fixture never constructs an AuthManager): hidden, and
    # the status-bar button offers sign-in.
    assert window.sidebar_buttons["Community"].isHidden()
    assert window.signin_button.text() == "Sign in"

    window.community_page.is_signed_in = lambda: True
    window._apply_auth_visibility()

    assert not window.sidebar_buttons["Community"].isHidden()
    assert window.signin_button.text() == "Sign out"


def test_identity_label_shows_the_display_name_next_to_sign_out(window) -> None:
    # The Community page used to carry its own "Signed in as ... [Sign out]"
    # row; identity now lives only in the status bar (JL live-testing).
    window.auth = types.SimpleNamespace(identity=lambda: ("Ada Lovelace", "ada@example.com"))
    window.community_page.is_signed_in = lambda: True

    window._apply_auth_visibility()

    assert not window.identity_label.isHidden()
    assert window.identity_label.text() == "Ada Lovelace"
    assert window.identity_label.toolTip() == "ada@example.com"


def test_identity_label_falls_back_to_email_with_no_name_claim(window) -> None:
    window.auth = types.SimpleNamespace(identity=lambda: (None, "ada@example.com"))
    window.community_page.is_signed_in = lambda: True

    window._apply_auth_visibility()

    assert window.identity_label.text() == "ada@example.com"


def test_identity_label_is_hidden_when_signed_out(window) -> None:
    window.auth = types.SimpleNamespace(identity=lambda: ("Ada Lovelace", "ada@example.com"))
    window.community_page.is_signed_in = lambda: False

    window._apply_auth_visibility()

    assert window.identity_label.isHidden()
    assert window.identity_label.text() == ""


def test_train_goes_live_for_a_model_this_user_owns(window, config) -> None:
    seed_model(config, {"9mm FC": 1})

    mode_changed(window)

    assert not window.sidebar_buttons["Train"].property("unavailable")
    assert window.train_page.is_available()


def test_train_stays_muted_for_a_community_model(window, config) -> None:
    from sorter.data.repository import ModelRepo

    repo = ModelRepo(config.db)
    model = repo.get(seed_model(config, {"9mm FC": 1}))
    assert model is not None
    model.model_type = "CommunityManaged"
    repo.update(model)

    mode_changed(window)

    assert not window.sidebar_buttons["Train"].isHidden()
    assert window.sidebar_buttons["Train"].property("unavailable") is True


def test_a_muted_train_keeps_its_page_when_the_mode_changes(window, config) -> None:
    """Nothing bounces the user off Train anymore — the page is the explainer."""
    seed_model(config, {"9mm FC": 1})
    mode_changed(window)
    window.sidebar_buttons["Train"].click()

    from sorter.data.repository import SettingsRepo

    SettingsRepo(config.db).clear_active_model()
    mode_changed(window)

    assert window.pages.currentWidget() is window._pages_by_name["Train"]
    assert not window.train_page.is_available()


def test_hiding_the_current_activity_falls_back_to_sort(window) -> None:
    """Community is the only activity that still comes and goes (auth)."""
    window.community_page.is_signed_in = lambda: True
    window._apply_auth_visibility()
    window.sidebar_buttons["Community"].click()
    assert window.pages.currentWidget() is window._pages_by_name["Community"]

    window.community_page.is_signed_in = lambda: False
    window._apply_auth_visibility()

    assert window.pages.currentWidget() is window._pages_by_name["Sort"]
    assert window.sidebar_buttons["Sort"].isChecked()


def test_a_model_switch_re_reads_the_slots_and_templates(window, config) -> None:
    window.bus.post("run/result", {"ok": True, "slot": 1})
    window.bus.drain()
    seed_model(config, {"9mm FC": 1})

    mode_changed(window)

    assert window.slot_grid.cards[1].names_label.text() == "9mm FC"
    assert window.slot_grid.cards[1].count_label.text() == "0"
    assert window.template_combo.currentText() == "Default"


# ----- Settings → Serial -----------------------------------------------------


# Port-combo ordering (Emulated first, USB before /dev/ttyS*) is pinned in
# test_settings_serial.py, where the combo lives now.


def test_connecting_the_emulator_wires_the_run_controller(window, config) -> None:
    from sorter.data.config import Config

    window.connect_serial(EMULATED_PORT)

    assert isinstance(window.broker, EmulatorBroker)
    assert isinstance(window.run_controller, RunController)
    assert window.run_controller.broker is window.broker
    assert "connected" in window.serial_label.text()
    assert window.run_button.isEnabled()
    # Persisted, not just remembered in the in-memory section.
    assert Config(config.db).load().serial["port"] == EMULATED_PORT


def test_manual_feed_drives_the_emulator_end_to_end(window, config, monkeypatch) -> None:
    config.add_headstamp("9mm FC", 2)
    monkeypatch.setattr(classifier, "classify_active", lambda *a, **k: ("9mm FC", 99.0))
    # The controller captures the camera it is built with, so stub before connecting.
    window.camera = types.SimpleNamespace(
        capture_frame=lambda: np.zeros((480, 640, 3), np.uint8),
        latest_frame=lambda: None,
        stop=lambda: None,
    )
    window.connect_serial(EMULATED_PORT)

    window.action_buttons["Manual feed"].click()

    assert drain_until(window, lambda: window.slot_grid.cards[2].count_label.text() == "1")
    assert window.result_label.text() == "9mm FC"
    # The dock saw the exchange: the wheel command out, the board's ack back.
    log = window.serial_monitor.output.toPlainText()
    assert "-> xf:0" in log
    assert "<- done" in log


# ----- run options ------------------------------------------------------------


def test_run_options_reflect_the_config_defaults(window, config) -> None:
    assert window.floor_spin.value() == config.run_confidence_floor
    assert window.store_images_combo.currentText() == "None"
    assert window.auto_select_check.isChecked() == config.run_auto_select_trays


def test_confidence_floor_control_persists_and_colors_the_result(window, config) -> None:
    window.floor_spin.setValue(80)

    assert config.run_confidence_floor == 80

    window.bus.post("run/history", history("unknown", 41.0))
    window.bus.drain()

    assert window.result_confidence_label.text() == "41%"
    assert window.palette_colors["warning"] in window.result_confidence_label.styleSheet()


def test_store_images_control_persists_and_warns_once_per_session(window, config) -> None:
    notices = []
    window.notify = lambda title, text: notices.append(title)

    window.store_images_combo.setCurrentText("Above confidence floor")
    assert config.run_store_images == "above"
    assert notices == ["Store images enabled"]

    window.store_images_combo.setCurrentText("All images")
    assert config.run_store_images == "all"
    assert notices == ["Store images enabled"]  # not a second time

    window.store_images_combo.setCurrentText("None")
    assert config.run_store_images == "none"


def test_auto_select_control_persists(window, config) -> None:
    window.auto_select_check.setChecked(True)
    assert config.run_auto_select_trays is True

    window.auto_select_check.setChecked(False)
    assert config.run_auto_select_trays is False


# ----- the action row --------------------------------------------------------


def action_row(window):
    """The launcher strip at the foot of the Sort page."""
    column = window._pages_by_name["Sort"].layout()
    for index in range(column.count()):
        row = column.itemAt(index).layout()
        if row is not None and any(row.itemAt(i).widget() is window.run_button for i in range(row.count())):
            return row
    raise AssertionError("the run button is not on any row of the Sort page")


def grid_header_row(window):
    """The row above the slot cards: Slots · counters · template picker."""
    holder = window.slot_grid.parentWidget()
    header = holder.layout().itemAt(0).layout()
    assert header is not None, "the slot column's header row is gone"
    return header


def row_widgets(row) -> list:
    return [row.itemAt(i).widget() for i in range(row.count())]


def test_the_run_controls_are_right_aligned_on_the_strip(window) -> None:
    """JL: the launchers cluster on the right, like the Train page's Training
    strip — stretch first, buttons after, nothing floating mid-row."""
    row = action_row(window)

    assert row.itemAt(0).spacerItem() is not None, "the strip does not start with the stretch"
    widgets = row_widgets(row)
    assert widgets[0] is None  # the spacer's slot
    for widget in (
        window.run_button,
        window.action_buttons["Manual feed"],
        window.run_options_button,
        window.notes_button,
    ):
        assert widget in widgets
    # No second stretch: one after the buttons would re-centre the cluster.
    assert sum(1 for i in range(row.count()) if row.itemAt(i).spacerItem() is not None) == 1
    # Nothing of the template group is left down here.
    assert window.template_combo not in widgets


def test_the_template_picker_rides_the_slot_grid_header(window) -> None:
    """JL: the template names the layout the cards *are*, so it belongs over
    them — right-aligned after the run counters, not on the launcher strip."""
    header = row_widgets(grid_header_row(window))

    for widget in (
        window.template_hint,
        window.template_combo,
        window.template_new_button,
        window.template_edit_button,
    ):
        assert widget in header
    # After the counters it came to join, and after the single stretch that
    # pins the whole cluster right.
    assert header.index(window.master_count_label) < header.index(window.template_combo)
    stretches = [i for i in range(grid_header_row(window).count()) if grid_header_row(window).itemAt(i).spacerItem()]
    assert len(stretches) == 1
    assert stretches[0] < header.index(window.template_combo)

    # And the Sort page still has exactly one row of its own under the splitter.
    column = window._pages_by_name["Sort"].layout()
    assert sum(1 for i in range(column.count()) if column.itemAt(i).layout() is not None) == 1


def test_the_template_edits_are_compact_and_explain_themselves(window) -> None:
    assert window.template_new_button.toolTip() == "New template…"
    assert window.template_edit_button.toolTip() == "Edit template…"
    assert window.template_combo.maximumWidth() <= 240


# ----- a camera that isn't there ---------------------------------------------


def test_the_preview_says_where_to_fix_a_dead_camera(window) -> None:
    window._set_camera_indicator("Camera: failed to start", connected=False)

    text = window.preview_label.text()
    assert "No camera feed" in text
    assert "open Camera settings" in text

    # The whole label is the click target (plain text on purpose — a rich-text
    # link here crashed Windows CI offscreen; see _PreviewLabel).
    window.preview_label.clicked.emit()

    assert window.pages.currentWidget() is window._pages_by_name["Settings"]
    assert window.settings_list.currentItem().text() == "Camera"


def test_a_failed_camera_start_reaches_the_status_bar(window) -> None:
    window.camera = types.SimpleNamespace(
        start_preview=lambda: False,
        latest_frame=lambda: None,
        stop=lambda: None,
        width=640,
        height=480,
    )

    window.start_camera()
    window.bus.drain()

    assert "Settings" in window.statusBar().currentMessage()
    assert "Camera" in window.statusBar().currentMessage()
    assert window._camera_state[1] is False


def test_an_openai_model_keeps_ai_config_live_and_train_muted(window, config) -> None:
    """The mode pair's third state (PR #125 review): an active openai model
    classifies over HTTP, so AI Config stays the live surface — editing that
    model's own settings — while Train gets the openai explainer."""
    from sorter.data.models import AIModelConfig, Model
    from sorter.data.repository import CartridgeRepo, ModelRepo, SettingsRepo

    cart = CartridgeRepo(config.db).list()[0]
    model = ModelRepo(config.db).create(
        Model(name="HTTP model", cartridge_id=cart.id, model_mode="openai", ai_model_config=AIModelConfig())
    )
    SettingsRepo(config.db).set_active_model_id(model.id)
    mode_changed(window)

    assert not window.sidebar_buttons["AI Config"].property("unavailable")
    assert window.sidebar_buttons["Train"].property("unavailable")
    assert window.ai_page.is_available()
    assert "HTTP model" in window.ai_page.section.target_label.text()

    window.sidebar_buttons["Train"].click()
    assert not window.train_page.is_available()
    assert "HTTP" in window.train_page.unavailable_title.text()
