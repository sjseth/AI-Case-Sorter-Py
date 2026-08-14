"""Community model settings on the Sort page: version prompt + moderator notes.

Offscreen and networkless: the seams are ``community_page.api_factory`` (a fake
``CommunityApi``), the auth object the page reads off the window, and the two
modal hooks (``window.open_notes_dialog`` / ``window.open_model_update_dialog``).
Dialogs are built through the window's own wiring and driven by their button
handlers — never ``exec()``.

The store the ack flow writes to is the real SQLite-backed ``SettingsRepo``, so
"acknowledged survives a restart" is asserted against a second window built on
the same config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from sorter import paths
from sorter.community import notes as notes_store
from sorter.community.community_api import ModelInfo, ModelSettings, ModeratorNote
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, ModelRepo, SettingsRepo
from sorter.ml import classifier
from sorter.ui.app import FEEDBACK_BLOCKED_STATUS, MODEL_UPDATE_BUTTON, NOTES_BUTTON, NOTES_GATE_TITLE
from sorter.ui.dialog_community_notes import BUTTON_ACKNOWLEDGE, NEW_BADGE, linkify
from sorter.ui.dialog_model_update import BUTTON_UPDATE, CARRY_OVER, build_model_update_dialog, format_meta

from .conftest import drain_until, seed_model

UID = "uid-1"


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


# ----- fakes -------------------------------------------------------------------


class FakeAuth:
    def is_authenticated(self) -> bool:
        return True

    def identity(self) -> tuple[str, str]:
        return "Ada", "ada@example.invalid"


class FakeApi:
    """Records what the Sort page asks for; answers with canned settings."""

    def __init__(self, settings: ModelSettings | None = None, models: list[ModelInfo] | None = None) -> None:
        self.settings = settings
        self.models = list(models or [])
        self.settings_calls: list[str] = []
        self.model_queries = 0

    def fetch_model_settings(self, uid: str) -> ModelSettings | None:
        self.settings_calls.append(uid)
        return self.settings

    def get_models(self, search: str = "", model_type: str = "", cartridge: str = "") -> list[ModelInfo]:
        self.model_queries += 1
        return list(self.models)


def settings(**overrides: Any) -> ModelSettings:
    payload: dict[str, Any] = {"version": 0, "confidence_floor": 0}
    payload.update(overrides)
    return ModelSettings(**payload)


def info(version: int = 5, **overrides: Any) -> ModelInfo:
    payload: dict[str, Any] = {
        "model_uid": UID,
        "model_name": "Range brass",
        "cartridge_name": "9mm",
        "cartridge_id": 1,
        "model_version": version,
        "model_description": "",
        "author": "publisher",
        "publish_date": "2026-03-04T10:00:00",
        "image_count": 12,
        "headstamp_count": 3,
        "download_size": 2048,
        "export_mode": "ModelAndImages",
    }
    payload.update(overrides)
    return ModelInfo(**payload)


def note(note_id: int = 1, text: str = "Center the case.", created: str = "2026-08-01T09:00:00") -> ModeratorNote:
    return ModeratorNote(id=note_id, note=text, created=created)


# ----- fixtures ----------------------------------------------------------------


def seed_community_model(
    config: Any,
    *,
    uid: str | None = UID,
    version: int = 3,
    model_type: str = "CommunityManaged",
    feedback: bool = True,
) -> Model:
    cartridge = CartridgeRepo(config.db).list()[0]
    model = ModelRepo(config.db).create(
        Model(
            name="Range brass",
            cartridge_id=cartridge.id,
            community_model_uid=uid,
            model_version=version,
            model_type=model_type,
            feedback_loop_enabled=feedback,
            feedback_loop_confidence_floor=95,
        )
    )
    SettingsRepo(config.db).set_active_model_id(model.id)
    return model


def wire(window: Any, api: FakeApi, *, signed_in: bool = True) -> FakeApi:
    """Every seam replaced: no MSAL, no network, no modal."""
    window.auth = FakeAuth() if signed_in else None
    window.notify = lambda title, text: window._notices.append((title, text))
    window._notices = []
    window.community_page.api_factory = lambda: api
    window.open_notes_dialog = lambda: window._notes_opened.append(True)
    window._notes_opened = []
    return api


def enter_sort(window: Any, api: FakeApi) -> None:
    """Navigate to Sort and let the fetch land."""
    window.show_page("Sort")
    drain_until(window, lambda: not window._settings_fetch_busy)


# ----- the fetch ---------------------------------------------------------------


def test_entering_sort_fetches_the_settings_once(config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings()))

    enter_sort(window, api)

    assert api.settings_calls == [UID]


def test_a_local_model_never_fetches(config, window) -> None:
    seed_model(config, {"FC": 1})
    api = wire(window, FakeApi(settings()))

    enter_sort(window, api)

    assert api.settings_calls == []


def test_a_signed_out_user_never_fetches(config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings()), signed_in=False)

    enter_sort(window, api)

    assert api.settings_calls == []


def test_a_shared_model_of_our_own_fetches_like_the_wish_list_does(config, window) -> None:
    """The two fetches must agree — this one is Standard but feedback-enabled."""
    seed_community_model(config, model_type="Standard", feedback=True)
    api = wire(window, FakeApi(settings()))

    enter_sort(window, api)

    assert api.settings_calls == [UID]


def test_the_server_floor_applies_to_the_run_and_never_to_the_model_row(config, window, monkeypatch) -> None:
    model = seed_community_model(config)
    api = wire(window, FakeApi(settings(confidence_floor=70)))
    window.broker = object()
    window._rebuild_run_controller()

    enter_sort(window, api)

    feedback = window.run_controller._feedback
    assert feedback.effective_floor(model) == 70
    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None and stored.feedback_loop_confidence_floor == 95


def test_a_reconnect_reinstalls_the_server_settings(config, window) -> None:
    model = seed_community_model(config)
    api = wire(window, FakeApi(settings(confidence_floor=70)))
    window.broker = object()
    window._rebuild_run_controller()
    enter_sort(window, api)

    window._rebuild_run_controller()  # a reconnect builds a fresh FeedbackService

    assert window.run_controller._feedback.effective_floor(model) == 70


def test_a_blocked_model_says_so_once(config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(blocked=True)))

    enter_sort(window, api)

    assert window.statusBar().currentMessage() == FEEDBACK_BLOCKED_STATUS


def test_a_failed_fetch_leaves_local_behaviour_alone(config, window) -> None:
    model = seed_community_model(config)
    api = wire(window, FakeApi(settings(confidence_floor=70)))
    window.broker = object()
    window._rebuild_run_controller()
    enter_sort(window, api)

    api.settings = None  # offline
    enter_sort(window, api)

    assert window.run_controller._feedback.effective_floor(model) == 95
    assert window.model_update_button.isHidden()


def test_the_wish_list_is_pre_seeded_but_an_empty_one_keeps_the_start_time_list(config, window) -> None:
    model = seed_community_model(config)
    api = wire(window, FakeApi(settings(wish_list=["FC"])))
    window.broker = object()
    window._rebuild_run_controller()

    enter_sort(window, api)
    assert window.run_controller._feedback.wish_list() == ["fc"]

    api.settings = settings()  # no wish list this time
    enter_sort(window, api)
    assert window.run_controller._feedback.wish_list() == ["fc"]
    assert model.id  # (the list is bound to it)


# ----- the version prompt -------------------------------------------------------


def test_a_newer_version_raises_the_status_bar_button(config, window) -> None:
    seed_community_model(config, version=3)
    api = wire(window, FakeApi(settings(version=5), models=[info(version=5)]))

    enter_sort(window, api)

    assert not window.model_update_button.isHidden()
    assert window.model_update_button.text() == MODEL_UPDATE_BUTTON.format(version=5)


@pytest.mark.parametrize("published", [3, 2])
def test_an_older_or_equal_version_raises_nothing(config, window, published: int) -> None:
    seed_community_model(config, version=3)
    api = wire(window, FakeApi(settings(version=published), models=[info(version=published)]))

    enter_sort(window, api)

    assert window.model_update_button.isHidden()
    assert api.model_queries == 0  # nothing to show, nothing to look up


def test_an_unknown_local_version_never_nags(config, window) -> None:
    seed_community_model(config, version=0)
    api = wire(window, FakeApi(settings(version=5), models=[info(version=5)]))

    enter_sort(window, api)

    assert window.model_update_button.isHidden()


def test_a_model_missing_from_the_catalogue_raises_nothing(config, window) -> None:
    """No catalogue row means no way to drive the download — so no prompt."""
    seed_community_model(config, version=3)
    api = wire(window, FakeApi(settings(version=5), models=[]))

    enter_sort(window, api)

    assert api.model_queries == 1
    assert window.model_update_button.isHidden()


def test_update_now_drives_the_community_pages_own_download(config, window) -> None:
    seed_community_model(config, version=3)
    entry = info(version=5)
    api = wire(window, FakeApi(settings(version=5), models=[entry]))
    updates: list[Any] = []
    window.community_page.start_update = lambda info: updates.append(info)
    enter_sort(window, api)

    dialog = window.model_update_dialog()
    dialog.update_button.click()

    assert updates == [entry]
    assert window.model_update_button.isHidden()  # the update is under way


def test_not_now_hides_the_button_until_the_next_entry(config, window) -> None:
    seed_community_model(config, version=3)
    api = wire(window, FakeApi(settings(version=5), models=[info(version=5)]))
    enter_sort(window, api)

    window.model_update_dialog().reject()  # "Not now", and closing, are the same
    assert window.model_update_button.isHidden()

    enter_sort(window, api)

    assert not window.model_update_button.isHidden()


def test_the_dialog_carries_upstreams_information(qapp, window) -> None:
    calls: list[bool] = []
    dialog = build_model_update_dialog(
        window,
        model_name="Range brass",
        installed_version=3,
        available_version=5,
        info=info(version=5),
        on_update=lambda: calls.append(True),
    )

    assert dialog.installed_label.text() == "v3"
    assert dialog.available_label.text() == "v5"
    assert dialog.meta_label.text().startswith("Published ")
    assert "2.00 KB" in dialog.meta_label.text() and "by publisher" in dialog.meta_label.text()
    assert "sorting templates" in CARRY_OVER
    assert dialog.update_button.text() == BUTTON_UPDATE

    dialog.update_button.click()
    assert calls == [True]


def test_the_dialog_reads_without_catalogue_metadata(qapp, window) -> None:
    dialog = build_model_update_dialog(
        window, model_name="Range brass", installed_version=3, available_version=5, info=None
    )
    assert format_meta(None) == ""
    assert dialog.meta_label.text() == ""


# ----- moderator notes ----------------------------------------------------------


def test_an_unacknowledged_note_raises_the_dialog_on_entering_sort(qapp, config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note()])))

    enter_sort(window, api)
    qapp.processEvents()  # the modal is queued out of the drain

    assert window._notes_opened == [True]


def test_acknowledged_notes_never_re_raise_and_survive_a_restart(qapp, config, window, window_factory) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note()])))
    enter_sort(window, api)
    qapp.processEvents()

    dialog = window.notes_dialog()
    assert dialog.list.item(0).text().endswith(NEW_BADGE)
    dialog.acknowledge()

    rebuilt = window_factory(config)
    api2 = wire(rebuilt, FakeApi(settings(notes=[note()])))
    enter_sort(rebuilt, api2)
    qapp.processEvents()

    assert rebuilt._notes_opened == []
    assert [n.acknowledged for n in notes_store.load(config.db, UID)] == [True]


def test_remind_me_later_re_raises_on_the_next_entry(qapp, config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note()])))
    enter_sort(window, api)
    qapp.processEvents()

    dialog = window.notes_dialog()
    dialog.reject()  # "Remind me later" / closing changes nothing

    enter_sort(window, api)
    qapp.processEvents()

    assert window._notes_opened == [True, True]


def test_start_is_refused_while_a_note_is_unacknowledged(qapp, config, window, monkeypatch) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note()])))
    # Everything else about this model is ready to run; only the note isn't.
    monkeypatch.setattr(classifier, "checkpoint_problem", lambda _db: None)
    monkeypatch.setattr(window, "ensure_torch", lambda proceed, **kw: True)
    window.broker = object()
    window._rebuild_run_controller()
    starts: list[int] = []
    window.run_controller.start = lambda: starts.append(1)
    enter_sort(window, api)
    qapp.processEvents()

    window.start_run()
    assert [title for title, _text in window._notices] == [NOTES_GATE_TITLE]
    assert starts == []

    window.notes_dialog().acknowledge()
    window.start_run()
    assert starts == [1]


def test_the_history_button_shows_for_any_notes_acknowledged_or_not(qapp, config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note(), note(2, "Second")])))
    assert window.notes_button.isHidden()

    enter_sort(window, api)
    qapp.processEvents()

    assert not window.notes_button.isHidden()
    assert window.notes_button.text() == NOTES_BUTTON.format(count=2)

    window.notes_dialog().acknowledge()
    assert not window.notes_button.isHidden()  # it is the history view too


def test_a_note_the_server_stopped_sending_stays_in_the_history(qapp, config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note(), note(2, "Second")])))
    enter_sort(window, api)
    qapp.processEvents()
    window.notes_dialog().acknowledge()

    api.settings = settings(notes=[note()])
    enter_sort(window, api)

    assert window.notes_button.text() == NOTES_BUTTON.format(count=2)


def test_a_url_in_a_note_renders_as_a_clickable_anchor(qapp, config, window) -> None:
    seed_community_model(config)
    api = wire(window, FakeApi(settings(notes=[note(text="See https://example.invalid/guide for the crop.")])))
    enter_sort(window, api)
    qapp.processEvents()

    dialog = window.notes_dialog()

    assert dialog.body.openExternalLinks() is True
    assert '<a href="https://example.invalid/guide">' in dialog.body.toHtml()
    assert dialog.acknowledge_button.text() == BUTTON_ACKNOWLEDGE


def test_note_bodies_are_escaped_apart_from_their_links() -> None:
    rendered = linkify("<b>bold</b> https://example.invalid/a?x=1&y=2.")
    assert "&lt;b&gt;bold&lt;/b&gt;" in rendered
    # The trailing sentence period stays out of the href.
    assert '<a href="https://example.invalid/a?x=1&amp;y=2">' in rendered
    assert rendered.endswith(".")


def test_switching_models_drops_the_previous_ones_prompts(qapp, config, window) -> None:
    seed_community_model(config, version=3)
    api = wire(window, FakeApi(settings(version=5, notes=[note()]), models=[info(version=5)]))
    enter_sort(window, api)
    qapp.processEvents()
    assert not window.model_update_button.isHidden()

    seed_model(config, {"FC": 1}, name="Local")
    window.bus.post("mode/changed", None)
    window.bus.drain()

    assert window.model_update_button.isHidden()
    assert window.notes_button.isHidden()
