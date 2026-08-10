"""HTTP client for the OpenAI-compatible Case Sorter server."""

from __future__ import annotations

import base64
import json
from typing import Any

import cv2
import numpy as np
import requests

# Module-level session — pools TCP/TLS connections per host so repeated
# classify calls don't pay handshake cost (typically 100-500 ms each on HTTPS).
_session = requests.Session()


class ApiError(Exception):
    pass


def _encode_jpeg(image_bgr: np.ndarray, scale_pct: int, quality: int) -> bytes:
    """JPEG-encode a BGR frame."""
    if scale_pct != 100:
        scale = max(1, scale_pct) / 100.0
        w = max(1, int(image_bgr.shape[1] * scale))
        h = max(1, int(image_bgr.shape[0] * scale))
        image_bgr = cv2.resize(image_bgr, (w, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise ApiError("Failed to JPEG-encode image.")
    return buf.tobytes()


def _build_url(endpoint: str) -> str:
    if not endpoint:
        raise ApiError("API endpoint URL is not configured.")
    return endpoint.rstrip("/") + "/v1/chat/completions"


def _render_prompt(prompt: str, headstamps: list[str]) -> str:
    """Replace the {{headstamps}} placeholder with a newline-joined list.

    If the placeholder is absent, the prompt is sent unchanged.
    """
    if not prompt:
        prompt = "Classify this image. Respond with only the label."
    if "{{headstamps}}" not in prompt:
        return prompt
    return prompt.replace("{{headstamps}}", "\n".join(headstamps))


def classify(
    image_bgr: np.ndarray,
    headstamps: list[str],
    cfg: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> tuple[str, float]:
    """POST image to the classification endpoint. Returns (label, confidence%).

    Confidence is taken from a top-level ``confidence`` field in the server
    response when present (the server returns a float 0-1, e.g.
    ``0.999429832``). We convert that to a 0-100 percentage. If the field
    is absent or unparseable, confidence is -1 to signal 'unknown'.
    """
    url = _build_url(cfg.get("endpoint_url", ""))
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "")
    if not model:
        raise ApiError("API model is not configured.")

    jpeg = _encode_jpeg(
        image_bgr,
        int(cfg.get("image_scale", 80)),
        int(cfg.get("image_quality", 80)),
    )
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    prompt = _render_prompt(cfg.get("prompt", ""), headstamps)

    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    resp = _session.post(url, headers=headers, data=json.dumps(body), timeout=timeout)
    if resp.status_code >= 400:
        raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    payload = resp.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "", 0.0

    label = (text or "").strip()
    # Strip surrounding quotes the model sometimes adds.
    if len(label) >= 2 and label[0] == label[-1] and label[0] in ('"', "'"):
        label = label[1:-1].strip()

    raw_confidence = payload.get("confidence") if isinstance(payload, dict) else None
    if raw_confidence is None:
        confidence = -1.0
    else:
        try:
            confidence = float(raw_confidence) * 100.0
        except (TypeError, ValueError):
            confidence = -1.0
    return label, confidence


def get_headstamps(endpoint: str, model: str, *, timeout: float = 15.0) -> list[str]:
    """GET /getheadstamps?model={model}."""
    if not endpoint or not model:
        raise ApiError("Endpoint and model are required.")
    url = endpoint.rstrip("/") + "/getheadstamps"
    resp = _session.get(url, params={"model": model}, timeout=timeout)
    if resp.status_code >= 400:
        raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, list):
        raise ApiError("Server did not return a JSON array of strings.")
    return [str(x) for x in data if str(x).strip()]
