"""The AI Config activity page.

Offscreen, against the real SQLite-backed ``Config`` from the shared UI
conftest — every headstamp assertion round-trips through a *second* ``Config``
on the same DB, because the page reads them fresh on every access.

Nothing here touches the network or a camera: the test-shot tests stub
``window.camera`` and swap ``api_client``'s module-level session, so the real
request is built (rendered prompt, JPEG encoding, bearer key) and inspected
instead of sent.
"""

from __future__ import annotations

import base64
import json
import types
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

import cv2
from PySide6.QtWidgets import QLineEdit

from sorter.data.config import Config
from sorter.data.repository import SettingsRepo
from sorter.ml import api_client
from sorter.ui import ai_page

from .conftest import drain_until, seed_model


@pytest.fixture
def page(window):
    """The page, with modals and the camera stubbed out."""
    window.notify = _Notifier()
    window.camera = types.SimpleNamespace(capture_frame=lambda: None, latest_frame=lambda: None, stop=lambda: None)
    return ai_page.build_ai_page(window)


@pytest.fixture
def section(page):
    """The form half of the page — what most of these tests drive."""
    return page.section


class _Notifier:
    """Stand-in for ``win.notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))


class _Response:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self) -> Any:
        return self._payload


class _Session:
    """Records what ``api_client`` posts instead of putting it on the wire."""

    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], data: str, timeout: float) -> _Response:
        self.requests.append({"url": url, "headers": headers, "body": json.loads(data), "timeout": timeout})
        return self.response


def _frame() -> np.ndarray:
    """A synthetic BGR frame with a bright disc — enough for the Hough crop."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(frame, (320, 240), 180, (200, 200, 200), -1)
    return frame


def _fresh(config: Config) -> Config:
    """A second Config on the same DB — proves a mutation actually persisted."""
    return Config(config.db).load()


def _names(section: ai_page.AiSection) -> list[str]:
    return [str(e["name"]) for e in section._entries()]


# ----- server settings ----------------------------------------------------------


def test_fields_reflect_the_stored_config(section, config) -> None:
    api = config.api
    assert section.endpoint_edit.text() == api["endpoint_url"]
    assert section.key_edit.text() == api["api_key"]
    assert section.model_edit.text() == api["model"]
    assert section.prompt_edit.toPlainText() == api["prompt"]
    assert section.quality_spin.value() == int(api["image_quality"])
    assert section.scale_spin.value() == int(api["image_scale"])


def test_edits_persist_only_on_save(section, config) -> None:
    section.endpoint_edit.setText("  http://box:9000  ")
    section.key_edit.setText("sk-secret")
    section.model_edit.setText(" 45acp ")
    section.prompt_edit.setPlainText("Pick one:\n{{headstamps}}\n")
    section.quality_spin.setValue(70)
    section.scale_spin.setValue(50)

    # Nothing is written until the Save button is pressed.
    assert _fresh(config).api["endpoint_url"] == config.api["endpoint_url"]

    section.save_button.click()

    saved = _fresh(config).api
    assert saved["endpoint_url"] == "http://box:9000"  # whitespace trimmed
    assert saved["api_key"] == "sk-secret"
    assert saved["model"] == "45acp"
    assert saved["prompt"] == "Pick one:\n{{headstamps}}"  # trailing newline stripped
    assert saved["image_quality"] == 70
    assert saved["image_scale"] == 50


def test_api_key_is_masked_until_toggled(section) -> None:
    assert section.key_edit.echoMode() == QLineEdit.EchoMode.Password

    section.key_toggle.click()
    assert section.key_edit.echoMode() == QLineEdit.EchoMode.Normal
    assert section.key_toggle.text() == "Hide"

    section.key_toggle.click()
    assert section.key_edit.echoMode() == QLineEdit.EchoMode.Password
    assert section.key_toggle.text() == "Show"


# ----- headstamp manager --------------------------------------------------------


def test_add_persists_and_lists_fresh(section, config) -> None:
    section.name_edit.setText("9mm")
    section.add_button.click()

    assert [e["name"] for e in _fresh(config).headstamps] == ["9mm"]
    assert section.list_widget.count() == 1
    assert section.name_edit.text() == ""
    assert "1 headstamp" in section.count_label.text()


def test_duplicate_add_is_refused(section, config) -> None:
    for _ in range(2):
        section.name_edit.setText("9mm")
        section.add_button.click()

    assert [e["name"] for e in _fresh(config).headstamps] == ["9mm"]


def test_slot_assignment_round_trips(section, config, window) -> None:
    section.name_edit.setText("9mm")
    section.add_button.click()
    section.list_widget.setCurrentRow(0)

    section.slot_spin.setValue(3)

    assert _fresh(config).slot_for_headstamp("9mm") == 3
    # The list shows the assignment, and the spin follows the selection back.
    assert "slot 3" in section.list_widget.item(0).text()
    section.refresh_list()
    assert section.slot_spin.value() == 3


def test_slot_change_tells_the_sort_grid(section, window) -> None:
    posted: list[Any] = []
    window.bus.subscribe("run/assignment_changed", posted.append)
    section.name_edit.setText("9mm")
    section.add_button.click()
    section.list_widget.setCurrentRow(0)
    section.slot_spin.setValue(2)

    window.bus.drain()

    assert len(posted) == 2  # the add, then the slot change


def test_rename_keeps_the_slot(section, config) -> None:
    section.name_edit.setText("9mm")
    section.add_button.click()
    section.list_widget.setCurrentRow(0)
    section.slot_spin.setValue(4)

    section.name_edit.setText("9x19")
    section.rename_button.click()

    reread = _fresh(config)
    assert [e["name"] for e in reread.headstamps] == ["9x19"]
    assert reread.slot_for_headstamp("9x19") == 4
    assert section.name_edit.text() == ""


def test_rename_onto_an_existing_name_is_refused(section, config) -> None:
    for name in ("9mm", "45acp"):
        section.name_edit.setText(name)
        section.add_button.click()
    section.list_widget.setCurrentRow(0)  # "45acp" — the list is name-sorted

    section.name_edit.setText("9mm")
    section.rename_button.click()

    assert sorted(e["name"] for e in _fresh(config).headstamps) == ["45acp", "9mm"]


def test_remove_and_clear(section, config) -> None:
    for name in ("9mm", "45acp"):
        section.name_edit.setText(name)
        section.add_button.click()

    section.list_widget.setCurrentRow(0)
    section.remove_button.click()
    assert [e["name"] for e in _fresh(config).headstamps] == ["9mm"]

    # Clear all confirms first — it deletes everything at once.
    asked = []
    section.confirm = lambda title, text: asked.append(title) or False
    section.clear_button.click()
    assert asked == ["Clear all headstamps"]
    assert [e["name"] for e in _fresh(config).headstamps] == ["9mm"]

    section.confirm = lambda title, text: True
    section.clear_button.click()
    assert _fresh(config).headstamps == []
    assert section.list_widget.count() == 0


def test_remove_and_rename_need_a_selection(section) -> None:
    assert not section.remove_button.isEnabled()
    assert not section.rename_button.isEnabled()

    section.name_edit.setText("9mm")
    section.add_button.click()
    section.list_widget.setCurrentRow(0)

    assert section.remove_button.isEnabled()
    assert section.rename_button.isEnabled()


def test_load_from_server_adds_new_names(section, config, window, monkeypatch) -> None:
    monkeypatch.setattr(api_client, "get_headstamps", lambda endpoint, model: ["9mm", "45acp"])

    section.load_button.click()

    assert drain_until(window, lambda: section.list_widget.count() == 2)
    assert sorted(e["name"] for e in _fresh(config).headstamps) == ["45acp", "9mm"]


def test_load_from_server_needs_endpoint_and_model(section, window) -> None:
    section.model_edit.setText("")

    section.load_button.click()

    assert window.notify.calls and window.notify.calls[0][0] == "Missing values"


# ----- mode awareness -----------------------------------------------------------


def test_a_local_model_replaces_the_form_with_the_explainer(page, section, config) -> None:
    """Train's pattern, mirrored: the form is swapped out, not greyed out."""
    assert page.is_available()

    seed_model(config, {"9mm": 1, "45acp": 2}, name="Range brass")
    page.refresh_mode()

    assert not page.is_available()
    assert page.stack.currentWidget() is not section
    assert "Range brass" in page.notice_label.text()
    assert "Use AI Config" in page.notice_label.text()
    # Headstamps are model-scoped, so the list now shows the model's.
    assert _names(section) == ["45acp", "9mm"]


def test_the_explainer_falls_back_when_the_model_cannot_be_named(page, config, monkeypatch) -> None:
    seed_model(config, {"9mm": 1})
    monkeypatch.setattr(ai_page, "active_model_name", lambda _win: None)

    page.refresh_mode()

    assert page.notice_label.text() == ai_page.LOCAL_MODEL_NOTICE_UNNAMED


def test_the_explainer_jumps_to_the_models_page(page, window, config) -> None:
    seed_model(config, {"9mm": 1})
    page.refresh_mode()

    page.models_button.click()

    assert window.pages.currentWidget() is window._pages_by_name["Models"]
    assert window.sidebar_buttons["Models"].isChecked()


def test_the_form_comes_back_in_ai_config_mode(page, section, config) -> None:
    seed_model(config, {"9mm": 1})
    page.refresh_mode()

    SettingsRepo(config.db).clear_active_model()
    page.refresh_mode()

    assert page.is_available()
    assert page.stack.currentWidget() is section


def test_refresh_mode_keeps_unsaved_edits(page, section) -> None:
    section.endpoint_edit.setText("http://box:9000")

    page.refresh_mode()

    assert section.endpoint_edit.text() == "http://box:9000"


# ----- single-shot test (end to end through api_client) -------------------------


def _configure_server(section) -> None:
    section.endpoint_edit.setText("http://box:9000")
    section.key_edit.setText("sk-secret")
    section.model_edit.setText("9mm")
    section.prompt_edit.setPlainText("Pick one of:\n{{headstamps}}")
    section.quality_spin.setValue(80)
    section.scale_spin.setValue(50)


def test_test_shot_posts_the_crop_and_shows_the_result(section, window, monkeypatch) -> None:
    session = _Session(_Response(200, {"choices": [{"message": {"content": '"9mm"'}}], "confidence": 0.985}))
    monkeypatch.setattr(api_client, "_session", session)
    window.camera = types.SimpleNamespace(capture_frame=_frame, latest_frame=lambda: None, stop=lambda: None)
    for name in ("9mm", "45acp"):
        section.name_edit.setText(name)
        section.add_button.click()
    _configure_server(section)

    section.test_button.click()

    assert drain_until(window, lambda: section.result_label.text() == "9mm")
    assert section.confidence_label.text() == "98.50%"
    assert section.crop_label.pixmap() is not None and not section.crop_label.pixmap().isNull()
    assert section.test_button.isEnabled()

    assert len(session.requests) == 1
    request = session.requests[0]
    assert request["url"] == "http://box:9000/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-secret"
    body = request["body"]
    assert body["model"] == "9mm"
    content = body["messages"][0]["content"]
    # The {{headstamps}} placeholder is rendered from the page's own list.
    assert content[0]["text"] == "Pick one of:\n45acp\n9mm"
    prefix, _, b64 = content[1]["image_url"]["url"].partition(",")
    assert prefix == "data:image/jpeg;base64"
    sent = cv2.imdecode(np.frombuffer(base64.b64decode(b64), dtype=np.uint8), cv2.IMREAD_COLOR)
    # 480x480 crop at the page's 50% scale — the encoding fields are honored.
    assert sent is not None and sent.shape[:2] == (240, 240)


def test_test_shot_without_a_frame_reports_it(section, window) -> None:
    _configure_server(section)

    section.test_button.click()

    assert drain_until(window, lambda: "No camera frame" in window.statusBar().currentMessage())
    assert window.notify.calls == []
    assert section.test_button.isEnabled()


def test_test_shot_surfaces_a_request_failure(section, window, monkeypatch) -> None:
    monkeypatch.setattr(api_client, "_session", _Session(_Response(500, {"error": "boom"})))
    window.camera = types.SimpleNamespace(capture_frame=_frame, latest_frame=lambda: None, stop=lambda: None)
    _configure_server(section)

    section.test_button.click()

    assert drain_until(window, lambda: bool(window.notify.calls))
    assert window.notify.calls[0][0] == "Test failed"
    assert "HTTP 500" in window.notify.calls[0][1]
    assert "Test failed" in window.statusBar().currentMessage()
    assert section.test_button.isEnabled()


def test_test_shot_needs_credentials(section, window) -> None:
    section.key_edit.setText("")

    section.test_button.click()

    assert window.notify.calls and window.notify.calls[0][0] == "AI not configured"
