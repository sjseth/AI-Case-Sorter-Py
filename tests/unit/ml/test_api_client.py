"""Unit tests for api_client — request shape, prompt templating, response parsing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from sorter.ml import api_client


def _frame() -> np.ndarray:
    return np.full((100, 100, 3), 128, dtype=np.uint8)


def _cfg() -> dict:
    return {
        "endpoint_url": "https://example.com",
        "api_key": "secret-xyz",
        "model": "gpt-4.1",
        "prompt": "Classify. List:\n{{headstamps}}",
        "image_quality": 70,
        "image_scale": 50,
    }


def test_classify_uses_server_confidence_when_present() -> None:
    """SJS server returns a top-level 'confidence' float (0-1). Convert to %."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "WIN"}}],
        "confidence": 0.999429832,
    }
    with patch("sorter.ml.api_client._session.post", return_value=fake_response):
        label, confidence = api_client.classify(_frame(), [], _cfg())
    assert label == "WIN"
    assert confidence == pytest.approx(99.9429832, rel=1e-6)


def test_classify_returns_minus_one_when_server_omits_confidence() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "WIN"}}],
    }
    with patch("sorter.ml.api_client._session.post", return_value=fake_response):
        label, confidence = api_client.classify(_frame(), ["WIN"], _cfg())
    assert label == "WIN"
    assert confidence == -1.0


def test_classify_returns_minus_one_when_server_confidence_is_unparseable() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "WIN"}}],
        "confidence": "nan-not-a-float",
    }
    with patch("sorter.ml.api_client._session.post", return_value=fake_response):
        label, confidence = api_client.classify(_frame(), [], _cfg())
    assert label == "WIN"
    assert confidence == -1.0


def test_classify_request_shape_and_label() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "WIN"}}],
        "confidence": 1.0,
    }

    with patch("sorter.ml.api_client._session.post", return_value=fake_response) as post:
        label, confidence = api_client.classify(_frame(), ["WIN", "FC"], _cfg())

    assert label == "WIN"
    assert confidence == 100.0

    args, kwargs = post.call_args
    assert args[0] == "https://example.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-xyz"
    body = json.loads(kwargs["data"])
    assert body["model"] == "gpt-4.1"
    assert body["temperature"] == 0
    user_content = body["messages"][0]["content"]
    assert user_content[0]["text"].startswith("Classify. List:")
    assert "WIN" in user_content[0]["text"] and "FC" in user_content[0]["text"]
    assert user_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_classify_uses_server_confidence_even_for_off_list_label() -> None:
    """Confidence comes from the server payload regardless of headstamp list membership."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "Unknown"}}],
        "confidence": 0.25,
    }
    with patch("sorter.ml.api_client._session.post", return_value=fake_response):
        label, confidence = api_client.classify(_frame(), ["WIN", "FC"], _cfg())
    assert label == "Unknown"
    assert confidence == pytest.approx(25.0)


def test_classify_strips_quoted_label() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "choices": [{"message": {"content": '"WIN"'}}],
        "confidence": 1.0,
    }
    with patch("sorter.ml.api_client._session.post", return_value=fake_response):
        label, confidence = api_client.classify(_frame(), ["WIN"], _cfg())
    assert label == "WIN"
    assert confidence == 100.0


def test_classify_omits_headstamp_placeholder_when_absent() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"choices": [{"message": {"content": "WIN"}}]}
    cfg = _cfg()
    cfg["prompt"] = "No placeholder here"
    with patch("sorter.ml.api_client._session.post", return_value=fake_response) as post:
        api_client.classify(_frame(), ["WIN"], cfg)
    body = json.loads(post.call_args.kwargs["data"])
    assert body["messages"][0]["content"][0]["text"] == "No placeholder here"


def test_classify_raises_on_http_error() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = "boom"
    with patch("sorter.ml.api_client._session.post", return_value=fake_response):
        with pytest.raises(api_client.ApiError):
            api_client.classify(_frame(), [], _cfg())


def test_get_headstamps_parses_array() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = ["WIN", "FC", "Federal"]
    with patch("sorter.ml.api_client._session.get", return_value=fake_response) as get:
        names = api_client.get_headstamps("https://example.com/", "9mm")
    assert names == ["WIN", "FC", "Federal"]
    args, kwargs = get.call_args
    assert args[0] == "https://example.com/getheadstamps"
    assert kwargs["params"] == {"model": "9mm"}


def test_get_headstamps_rejects_non_array() -> None:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"oops": "not an array"}
    with patch("sorter.ml.api_client._session.get", return_value=fake_response):
        with pytest.raises(api_client.ApiError):
            api_client.get_headstamps("https://example.com", "9mm")
