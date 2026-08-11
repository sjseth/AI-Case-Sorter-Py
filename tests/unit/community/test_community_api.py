"""Tests for the Community API client.

Uses a stubbed `requests.Session` so no network calls are made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import requests

from sorter.community.auth import AuthManager, AuthResult
from sorter.community.community_api import (
    API_BASE,
    CartridgeInfo,
    CommunityApi,
    CommunityApiError,
    FeedbackUploadTicket,
    UserMetaData,
)


class _FakeResp:
    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._chunks = chunks or []
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return list(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, str]]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self.put_calls: list[tuple[str, dict[str, str]]] = []
        # `verify` as passed per request — see the note in CommunityApi.__init__
        # on why it must not live on the session.
        self.verify_args: list = []
        self.next_responses: dict[str, _FakeResp] = {}

    def get(self, url: str, headers=None, timeout=None, **kw) -> _FakeResp:
        self.get_calls.append((url, headers or {}))
        self.verify_args.append(kw.get("verify"))
        return self.next_responses.get(url, _FakeResp(404))

    def post(self, url: str, headers=None, json=None, timeout=None, **kw) -> _FakeResp:
        self.post_calls.append((url, json))
        self.verify_args.append(kw.get("verify"))
        return self.next_responses.get(url, _FakeResp(404))

    def put(self, url: str, data=None, headers=None, timeout=None, **kw) -> _FakeResp:
        self.put_calls.append((url, headers or {}))
        self.verify_args.append(kw.get("verify"))
        # Drain the body the way requests would: read() a file-like (drives the
        # progress callback) or iterate a generator.
        if hasattr(data, "read"):
            while data.read(65536):
                pass
        elif data is not None and not isinstance(data, (bytes, bytearray, str)):
            try:
                for _ in data:
                    pass
            except TypeError:
                pass
        # Azure returns 201 Created for a successful Put Blob.
        return self.next_responses.get(url, _FakeResp(201))


class _FakeAuth(AuthManager):
    def __init__(self, token: str = "TOKEN") -> None:
        # Skip MSAL setup
        self._token = token

    def acquire_token_silent(self, scopes=None) -> AuthResult | None:
        return AuthResult(
            access_token=self._token,
            name="x",
            email="x@x.com",
            expires_in=3600,
            raw={},
        )


def _api(session: _FakeSession) -> CommunityApi:
    # base_url pinned so these URL assertions don't depend on the developer's
    # CASESORTER_API_BASE; the override itself is covered below.
    # `_FakeSession` duck-types `requests.Session` (only the methods
    # CommunityApi actually calls) rather than subclassing it — subclassing
    # trips ty's method-override check since the fake's `get`/`post`/`put`
    # signatures are narrower than `Session`'s.
    return CommunityApi(auth=_FakeAuth(), session=cast(requests.Session, session), base_url=API_BASE)


def test_authorization_header_added(tmp_path: Path) -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/GetAvailableCartridges"
    s.next_responses[url] = _FakeResp(json_data=[{"Id": 1, "Name": "9mm"}])
    api = _api(s)
    cartridges = api.get_available_cartridges()
    assert cartridges == [CartridgeInfo(id=1, name="9mm")]
    assert s.get_calls[0][1].get("Authorization") == "Bearer TOKEN"


def test_get_models_query_string_built() -> None:
    s = _FakeSession()
    expected_url = f"{API_BASE}/Models/GetModels?searchQuery=foo+bar&ModelType=ModelAndImages&Cartridge=9mm"
    s.next_responses[expected_url] = _FakeResp(json_data=[])
    _api(s).get_models("foo bar", "ModelAndImages", "9mm")
    assert s.get_calls[0][0] == expected_url


def test_get_models_parses_results() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/GetModels?searchQuery=&ModelType=&Cartridge="
    s.next_responses[url] = _FakeResp(
        json_data=[
            {
                "ModelUID": "uid-1",
                "ModelName": "Foo",
                "CartridgeName": "9mm",
                "CartridgeId": 1,
                "ModelVersion": 3,
                "ModelDescription": "desc",
                "Author": "Anon",
                "PublishDate": "2026-01-01",
                "ImageCount": 100,
                "HeadstampCount": 5,
                "DownloadSize": 12345,
                "ModelExportMode": "ModelAndImages",
            }
        ]
    )
    results = _api(s).get_models()
    assert len(results) == 1
    m = results[0]
    assert m.model_uid == "uid-1"
    assert m.model_version == 3


def test_download_streams_and_writes_atomically(tmp_path: Path) -> None:
    s = _FakeSession()
    download_url = "https://blob.example/sas?token=abc"
    s.next_responses[download_url] = _FakeResp(
        chunks=[b"hello ", b"world"],
        headers={"Content-Length": "11"},
    )
    progress_calls: list[tuple[int, int | None]] = []
    api = _api(s)
    dest = tmp_path / "out.zip"
    api.download_to(download_url, dest, progress=lambda done, total: progress_calls.append((done, total)))
    assert dest.read_bytes() == b"hello world"
    assert not dest.with_suffix(".zip.tmp").exists()
    assert progress_calls[-1] == (11, 11)


def test_request_download_returns_sas_payload() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/RequestDownloadModel/uid-1"
    s.next_responses[url] = _FakeResp(json_data={"FullUrl": "https://sas/foo", "BlobPath": "p"})
    payload = _api(s).request_download("uid-1")
    assert payload["FullUrl"] == "https://sas/foo"


def test_non_200_raises() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/GetAvailableCartridges"
    s.next_responses[url] = _FakeResp(status_code=500)
    with pytest.raises(CommunityApiError):
        _api(s).get_available_cartridges()


def test_user_metadata_can_contribute_role_flag() -> None:
    """Role flags are a bitfield: 1=Read, 2=Contribute, 4=Admin.
    can_contribute() must return True iff the Contribute bit is set,
    regardless of other bits."""
    assert UserMetaData(roles=0).can_contribute() is False
    assert UserMetaData(roles=1).can_contribute() is False  # Read only
    assert UserMetaData(roles=2).can_contribute() is True  # Contribute
    assert UserMetaData(roles=3).can_contribute() is True  # Read+Contribute
    assert UserMetaData(roles=4).can_contribute() is False  # Admin only
    assert UserMetaData(roles=6).can_contribute() is True  # Contribute+Admin
    assert UserMetaData(roles=7).can_contribute() is True  # FullControl


def test_user_metadata_from_json_picks_up_roles() -> None:
    """Server payload uses PascalCase keys; tolerate camelCase too."""
    m1 = UserMetaData.from_json({"ProfileName": "x", "Country": "US", "Roles": 2})
    assert m1.profile_name == "x" and m1.country == "US" and m1.can_contribute()
    m2 = UserMetaData.from_json({"profileName": "y", "country": "CA", "roles": 1})
    assert m2.profile_name == "y" and m2.country == "CA" and not m2.can_contribute()


# ----- feedback-loop upload --------------------------------------------------


def test_request_feedback_upload_parses_ticket_and_posts_dto() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/FeedbackImageUploadRequest"
    s.next_responses[url] = _FakeResp(
        json_data={
            "SasToken": "st",
            "BlobPath": "bp",
            "ContainerName": "cn",
            "ContainerURI": "https://acct.blob/cn",
            "AccountURI": "https://acct.blob",
            "FullUrl": "https://acct.blob/cn/bp?st",
            "ModelInfo": {"feedbackAccepted": True, "feedbackMessage": ""},
        }
    )
    ticket = _api(s).request_feedback_upload(
        filename="WIN__1.jpg",
        community_model_uid="uid-1",
        classification="WIN",
        confidence=42,
    )
    assert ticket is not None
    assert ticket.feedback_accepted is True
    assert ticket.blob_put_url() == "https://acct.blob/cn/bp?st"
    sent_url, body = s.post_calls[0]
    assert sent_url == url
    assert body == {
        "filename": "WIN__1.jpg",
        "CommunityModelUID": "uid-1",
        "classification": "WIN",
        "confidence": 42,
    }


def test_request_feedback_upload_declined_flag() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/FeedbackImageUploadRequest"
    s.next_responses[url] = _FakeResp(
        json_data={
            "SasToken": "x",
            "ModelInfo": {"feedbackAccepted": False, "feedbackMessage": "limit"},
        }
    )
    ticket = _api(s).request_feedback_upload(
        filename="f",
        community_model_uid="u",
        classification="c",
        confidence=1,
    )
    assert ticket is not None and ticket.feedback_accepted is False
    assert ticket.feedback_message == "limit"


def test_request_feedback_upload_html_returns_none() -> None:
    """Server answers with an HTML error page when the method is unavailable."""
    s = _FakeSession()
    url = f"{API_BASE}/Models/FeedbackImageUploadRequest"
    s.next_responses[url] = _FakeResp(text="<!DOCTYPE html><html>nope</html>")
    ticket = _api(s).request_feedback_upload(
        filename="f",
        community_model_uid="u",
        classification="c",
        confidence=1,
    )
    assert ticket is None


def test_upload_feedback_blob_puts_to_sas_url_with_headers(tmp_path: Path) -> None:
    s = _FakeSession()
    ticket = FeedbackUploadTicket(
        sas_token="st",
        blob_path="bp",
        container_uri="https://acct.blob/cn",
    )
    f = tmp_path / "img.jpg"
    f.write_bytes(b"jpegbytes")
    _api(s).upload_feedback_blob(f, ticket, version=3)
    put_url, headers = s.put_calls[0]
    assert put_url == "https://acct.blob/cn/bp?st"
    assert headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["x-ms-meta-version"] == "3"


def test_complete_feedback_upload_posts_raw_payload() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/CompleteFeedbackUpload"
    s.next_responses[url] = _FakeResp(status_code=200, text="true")
    raw = {"SasToken": "st", "BlobPath": "bp"}
    ok = _api(s).complete_feedback_upload(FeedbackUploadTicket(raw=raw))
    assert ok is True
    sent_url, body = s.post_calls[0]
    assert sent_url == url
    assert body == raw


def test_feedback_ticket_from_json_camelcase_and_defaults() -> None:
    t = FeedbackUploadTicket.from_json(
        {
            "sasToken": "s",
            "blobPath": "b",
            "containerURI": "c",
        }
    )
    assert t.sas_token == "s" and t.blob_path == "b" and t.container_uri == "c"
    # No ModelInfo block → default to accepted so a sparse reply still uploads.
    assert t.feedback_accepted is True


# ----- model share (export + upload) -----------------------------------------


def _file_url() -> str:
    return f"{API_BASE}/Models/FileUploadRequest"


def _complete_url() -> str:
    return f"{API_BASE}/Models/CompleteUpload"


def _manifest_req_url() -> str:
    return f"{API_BASE}/Models/ManifestUploadRequest"


def test_request_file_upload_posts_model_info() -> None:
    s = _FakeSession()
    s.next_responses[_file_url()] = _FakeResp(
        json_data={
            "SasToken": "zt",
            "BlobPath": "models/uid.zip",
            "ContainerURI": "https://acct.blob/cn",
            "ModelInfo": {"ModelUID": "sv"},
        }
    )
    ticket = _api(s).request_file_upload(
        filename="uid.zip",
        model_info={"ModelName": "M", "CommunityModelUID": "uid"},
    )
    assert ticket is not None
    assert ticket.blob_path == "models/uid.zip"
    assert ticket.model_info["ModelUID"] == "sv"
    sent_url, body = s.post_calls[0]
    assert sent_url == _file_url()
    assert body == {"filename": "uid.zip", "ModelInfo": {"ModelName": "M", "CommunityModelUID": "uid"}}


def test_upload_blob_puts_with_blob_headers(tmp_path: Path) -> None:
    from sorter.community.community_api import SasResponse

    s = _FakeSession()
    ticket = SasResponse(sas_token="zt", blob_path="models/uid.zip", container_uri="https://acct.blob/cn")
    f = tmp_path / "uid.zip"
    f.write_bytes(b"zipdata")
    seen: list[tuple[int, int]] = []
    _api(s).upload_blob(f, ticket, content_type="application/zip", progress=lambda a, b: seen.append((a, b)))
    put_url, headers = s.put_calls[0]
    assert put_url == "https://acct.blob/cn/models/uid.zip?zt"
    assert headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["Content-Type"] == "application/zip"
    assert seen and seen[-1] == (7, 7)  # 7 bytes streamed


def test_upload_blob_sends_sized_body_not_chunked(tmp_path: Path) -> None:
    """Body must expose a length so requests sets Content-Length and does NOT
    chunk — Azure Put-Blob rejects chunked encoding (the SSL-EOF on share)."""
    from sorter.community.community_api import SasResponse

    captured: dict[str, Any] = {}

    class _S(_FakeSession):
        def put(self, url, data=None, headers=None, timeout=None, **kw):
            captured["has_len"] = hasattr(data, "__len__")
            captured["len"] = len(data) if hasattr(data, "__len__") else None
            captured["headers"] = dict(headers or {})
            return super().put(url, data=data, headers=headers, timeout=timeout, **kw)

    s = _S()
    ticket = SasResponse(sas_token="t", blob_path="b", container_uri="https://a/c")
    f = tmp_path / "x.zip"
    f.write_bytes(b"0123456789")
    _api(s).upload_blob(f, ticket, content_type="application/zip")
    assert captured["has_len"] is True
    assert captured["len"] == 10
    assert "Transfer-Encoding" not in captured["headers"]
    assert captured["headers"]["x-ms-blob-type"] == "BlockBlob"


def test_upload_blob_retries_on_ssl_error(tmp_path: Path) -> None:
    from unittest.mock import patch

    from sorter.community.community_api import SasResponse

    attempts = {"n": 0}

    class _S(_FakeSession):
        def put(self, url, data=None, headers=None, timeout=None, **kw):
            attempts["n"] += 1
            if hasattr(data, "read"):
                while data.read(65536):
                    pass
            if attempts["n"] == 1:
                raise requests.exceptions.SSLError("EOF in violation of protocol")
            return _FakeResp(201)

    s = _S()
    ticket = SasResponse(sas_token="t", blob_path="b", container_uri="https://a/c")
    f = tmp_path / "x.zip"
    f.write_bytes(b"payload")
    with patch("sorter.community.community_api.time.sleep"):
        _api(s).upload_blob(f, ticket, retries=3)
    assert attempts["n"] == 2  # failed once, succeeded on retry


def test_share_model_runs_full_sequence_in_order(tmp_path: Path) -> None:
    s = _FakeSession()
    s.next_responses[_file_url()] = _FakeResp(
        json_data={
            "SasToken": "zt",
            "BlobPath": "models/uid.zip",
            "ContainerName": "cn",
            "ContainerURI": "https://acct.blob/cn",
            "ModelInfo": {"ModelUID": "server-uid"},
        }
    )
    s.next_responses[_complete_url()] = _FakeResp(status_code=200, text="true")
    s.next_responses[_manifest_req_url()] = _FakeResp(
        json_data={
            "SasToken": "mt",
            "BlobPath": "models/uid.manifest.json",
            "ContainerName": "cn",
            "ContainerURI": "https://acct.blob/cn",
        }
    )
    zip_path = tmp_path / "uid.zip"
    zip_path.write_bytes(b"zipdata")
    manifest_path = tmp_path / "uid.manifest.json"
    manifest_path.write_text("{}")

    prog: list[tuple[int, int]] = []
    uid = _api(s).share_model(
        zip_path=zip_path,
        manifest_path=manifest_path,
        model_info={"CommunityModelUID": "uid"},
        progress=lambda a, b: prog.append((a, b)),
    )
    # Server-assigned UID wins over the locally-minted one.
    assert uid == "server-uid"
    # Endpoints hit in the documented order.
    assert [u for u, _ in s.post_calls] == [_file_url(), _complete_url(), _manifest_req_url()]
    # CompleteUpload echoes the file-request payload verbatim.
    assert s.post_calls[1][1]["BlobPath"] == "models/uid.zip"
    # ManifestUploadRequest is keyed by the zip's blob path.
    assert s.post_calls[2][1] == {"BlobPath": "models/uid.zip"}
    # Two PUTs: the ZIP first, then the standalone manifest.
    assert [u for u, _ in s.put_calls] == [
        "https://acct.blob/cn/models/uid.zip?zt",
        "https://acct.blob/cn/models/uid.manifest.json?mt",
    ]
    assert prog  # zip upload reported progress


def test_share_model_skips_manifest_when_no_sas(tmp_path: Path) -> None:
    s = _FakeSession()
    s.next_responses[_file_url()] = _FakeResp(
        json_data={
            "SasToken": "zt",
            "BlobPath": "models/uid.zip",
            "ContainerURI": "https://acct.blob/cn",
            "ModelInfo": {},
        }
    )
    s.next_responses[_complete_url()] = _FakeResp(status_code=200, text="true")
    s.next_responses[_manifest_req_url()] = _FakeResp(status_code=500)
    zip_path = tmp_path / "uid.zip"
    zip_path.write_bytes(b"z")
    manifest_path = tmp_path / "uid.manifest.json"
    manifest_path.write_text("{}")
    uid = _api(s).share_model(
        zip_path=zip_path,
        manifest_path=manifest_path,
        model_info={"CommunityModelUID": "fallback-uid"},
    )
    assert uid == "fallback-uid"  # no ModelUID from server → local UID
    assert len(s.put_calls) == 1  # only the ZIP was PUT


def test_share_model_raises_when_file_request_refused(tmp_path: Path) -> None:
    s = _FakeSession()
    s.next_responses[_file_url()] = _FakeResp(status_code=403, text="nope")
    zip_path = tmp_path / "uid.zip"
    zip_path.write_bytes(b"z")
    with pytest.raises(CommunityApiError):
        _api(s).share_model(
            zip_path=zip_path,
            manifest_path=tmp_path / "m.json",
            model_info={},
        )


# ----- wish list (model-balancing feedback) ----------------------------------


def _wish_url(uid: str = "uid-1") -> str:
    return f"{API_BASE}/Models/FetchWishList?communityModelId={uid}"


def test_fetch_wish_list_returns_names() -> None:
    s = _FakeSession()
    s.next_responses[_wish_url()] = _FakeResp(json_data=["FC", "R-P", "WIN 9MM"])
    assert _api(s).fetch_wish_list("uid-1") == ["FC", "R-P", "WIN 9MM"]
    assert s.get_calls[0][1].get("Authorization") == "Bearer TOKEN"


def test_fetch_wish_list_url_encodes_uid() -> None:
    s = _FakeSession()
    url = f"{API_BASE}/Models/FetchWishList?communityModelId=a+b"
    s.next_responses[url] = _FakeResp(json_data=[])
    _api(s).fetch_wish_list("a b")
    assert s.get_calls[0][0] == url


def test_fetch_wish_list_drops_blank_and_non_string_entries() -> None:
    s = _FakeSession()
    s.next_responses[_wish_url()] = _FakeResp(json_data=["  FC  ", "", None, 7, "R-P"])
    assert _api(s).fetch_wish_list("uid-1") == ["FC", "R-P"]


def test_fetch_wish_list_fails_open() -> None:
    """Every failure mode answers [] — a run is never blocked on this call."""

    class _RaisingResp(_FakeResp):
        def json(self) -> Any:
            raise ValueError("not json")

    class _BoomSession(_FakeSession):
        def get(self, url, headers=None, timeout=None, **kw):
            raise requests.exceptions.ConnectionError("offline")

    assert _api(_FakeSession()).fetch_wish_list("") == []  # no UID
    assert _api(_BoomSession()).fetch_wish_list("uid-1") == []  # network error

    s = _FakeSession()
    s.next_responses[_wish_url()] = _FakeResp(status_code=500, text="boom")
    assert _api(s).fetch_wish_list("uid-1") == []  # non-200

    s = _FakeSession()
    s.next_responses[_wish_url()] = _RaisingResp(text="<html>")
    assert _api(s).fetch_wish_list("uid-1") == []  # non-JSON body

    s = _FakeSession()
    s.next_responses[_wish_url()] = _FakeResp(json_data={"nope": 1})
    assert _api(s).fetch_wish_list("uid-1") == []  # wrong shape


def test_fetch_wish_list_signed_out_returns_empty() -> None:
    class _NoAuth(_FakeAuth):
        def acquire_token_silent(self, scopes=None):
            return None

    s = _FakeSession()
    api = CommunityApi(auth=_NoAuth(), session=cast(requests.Session, s))
    assert api.fetch_wish_list("uid-1") == []
    assert s.get_calls == []
