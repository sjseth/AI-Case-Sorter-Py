"""Community API client for https://www.reloadingrecipes.com/api.

All calls go through a module-level `requests.Session` for connection pooling
(matches the pattern in `api_client._session`). Bearer tokens are pulled from
the `AuthManager` on each call so we always send the freshest token.

Model downloads use an Azure-blob SAS URL returned by the API. We just stream
the URL with `requests`; no `azure-storage-blob` dependency needed.

The base URL and TLS trust are developer-overridable (`CASESORTER_API_BASE`,
`CASESORTER_API_CA_BUNDLE`, `CASESORTER_API_INSECURE` — see `appenv`) so the
app can be pointed at a local copy of the backend. Both are resolved when a
`CommunityApi` is constructed, not at import, so a `.env` loaded during startup
still applies.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests

from . import appenv
from .auth import API_SCOPES, AuthManager

log = logging.getLogger(__name__)

# The production default. The URL actually used is `appenv.api_base()`, which
# applies a CASESORTER_API_BASE override; this constant is what that falls back
# to.
API_BASE = appenv.DEFAULT_API_BASE
_session = requests.Session()


class _ProgressReader:
    """File wrapper that reports read progress and exposes a fixed length.

    Azure Put-Blob rejects chunked transfer encoding, so the request body must
    advertise a Content-Length. Passing a generator made ``requests`` chunk the
    upload and Azure dropped the TLS connection (an SSL-EOF on share). A
    file-like object with ``__len__`` lets ``requests`` set Content-Length and
    stream without chunking, while ``read`` drives the progress callback.
    """

    def __init__(self, path: Path | str, total: int, callback: Callable[[int, int], None] | None):
        self._f = open(path, "rb")
        self._total = total
        self._cb = callback
        self._sent = 0

    def __len__(self) -> int:
        return self._total

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        if chunk:
            self._sent += len(chunk)
            if self._cb is not None:
                self._cb(self._sent, self._total)
        return chunk

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


@dataclass
class CartridgeInfo:
    id: int
    name: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CartridgeInfo:
        return cls(id=int(d.get("Id", d.get("id", 0))), name=d.get("Name", d.get("name", "")))


_EXPORT_MODE_NAMES = {0: "ModelOnly", 1: "ModelAndImages", 2: "ImagesOnly"}


def _export_mode_name(raw: Any) -> str:
    """The catalogue's export mode as its WinForms enum *name*.

    The server serializes the enum as its integer value (ModelOnly=0,
    ModelAndImages=1, ImagesOnly=2) — and 0 is falsy, which is exactly how
    every ModelOnly row rendered as "—" in the table (JL live-testing).
    Older payloads and our own exports carry the name; both are accepted.
    """
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return _EXPORT_MODE_NAMES[int(text)]
        except (KeyError, ValueError):
            return text
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _EXPORT_MODE_NAMES.get(int(raw), "ModelAndImages")
    return "ModelAndImages"


@dataclass
class ModelInfo:
    model_uid: str
    model_name: str
    cartridge_name: str
    cartridge_id: int
    model_version: int
    model_description: str
    author: str
    publish_date: str
    image_count: int
    headstamp_count: int
    download_size: int
    export_mode: str
    feedback_loop_enabled: bool = False
    feedback_loop_confidence_floor: int = 0

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ModelInfo:
        def get(key: str, default: Any = "") -> Any:
            return d.get(key, d.get(key[:1].lower() + key[1:], default))

        return cls(
            model_uid=get("ModelUID", ""),
            model_name=get("ModelName", ""),
            cartridge_name=get("CartridgeName", ""),
            cartridge_id=int(get("CartridgeId", 0) or 0),
            model_version=int(get("ModelVersion", 1) or 1),
            model_description=get("ModelDescription", ""),
            author=get("Author", ""),
            publish_date=get("PublishDate", ""),
            image_count=int(get("ImageCount", 0) or 0),
            headstamp_count=int(get("HeadstampCount", 0) or 0),
            download_size=int(get("DownloadSize", 0) or 0),
            export_mode=_export_mode_name(get("ModelExportMode", None)),
            feedback_loop_enabled=bool(get("FeedbackLoopEnabled", False)),
            feedback_loop_confidence_floor=int(get("FeedbackLoopConfidenceFloor", 0) or 0),
        )


@dataclass
class FeedbackUploadTicket:
    """SAS ticket for a feedback-image upload.

    Uses the server's PascalCase ``SasResponse`` shape plus the nested
    ``ModelInfo.feedbackAccepted``/``feedbackMessage`` (camelCase) rate-limit
    signal. The original payload is retained in ``raw`` so it can be POSTed
    back verbatim to ``CompleteFeedbackUpload``.
    """

    sas_token: str = ""
    blob_path: str = ""
    container_name: str = ""
    container_uri: str = ""
    account_uri: str = ""
    full_url: str = ""
    feedback_accepted: bool = True
    feedback_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> FeedbackUploadTicket:
        def get(key: str, default: Any = "") -> Any:
            # Accept PascalCase (the server's default) and camelCase spellings.
            return d.get(key, d.get(key[:1].lower() + key[1:], default))

        model_info = get("ModelInfo", {}) or {}

        def mi(key: str, default: Any) -> Any:
            return model_info.get(key, model_info.get(key[:1].lower() + key[1:], default))

        return cls(
            sas_token=get("SasToken", ""),
            blob_path=get("BlobPath", ""),
            container_name=get("ContainerName", ""),
            container_uri=get("ContainerURI", ""),
            account_uri=get("AccountURI", ""),
            full_url=get("FullUrl", ""),
            feedback_accepted=bool(mi("feedbackAccepted", True)),
            feedback_message=str(mi("feedbackMessage", "") or ""),
            raw=d,
        )

    def blob_put_url(self) -> str:
        """Full SAS URL to PUT the blob to: ``{ContainerURI}/{BlobPath}?{SasToken}``."""
        return f"{self.container_uri}/{self.blob_path}?{self.sas_token}"


@dataclass
class SasResponse:
    """SAS ticket for a model / manifest upload.

    Uses the server's PascalCase ``SasResponse`` shape. ``model_info`` carries
    the server-assigned ``ModelUID`` back from FileUploadRequest, and ``raw``
    is kept so the whole payload can be POSTed verbatim to ``CompleteUpload``.
    """

    sas_token: str = ""
    blob_path: str = ""
    container_name: str = ""
    container_uri: str = ""
    account_uri: str = ""
    full_url: str = ""
    model_info: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SasResponse:
        def get(key: str, default: Any = "") -> Any:
            return d.get(key, d.get(key[:1].lower() + key[1:], default))

        return cls(
            sas_token=get("SasToken", ""),
            blob_path=get("BlobPath", ""),
            container_name=get("ContainerName", ""),
            container_uri=get("ContainerURI", ""),
            account_uri=get("AccountURI", ""),
            full_url=get("FullUrl", ""),
            model_info=get("ModelInfo", {}) or {},
            raw=d,
        )

    def blob_put_url(self) -> str:
        """``{ContainerURI}/{BlobPath}?{SasToken}`` — the blob to PUT to."""
        return f"{self.container_uri}/{self.blob_path}?{self.sas_token}"


@dataclass
class ModeratorNote:
    """One note a moderator left the calling user about this model.

    ``id`` is stable server-side and is what an acknowledgement is recorded
    against (client-side only — the server keeps no ack state). ``note`` may
    contain hyperlinks.
    """

    id: int
    note: str
    created: str = ""


@dataclass
class ModelSettings:
    """Server-side settings for one community model (``FetchModelSettings``).

    The server's ``uploadmode`` is deliberately not carried: the client ignores
    it and the local upload-mode preference wins.

    Defaults are the "server said nothing" values, i.e. what leaves the client
    behaving as it does offline: no floor override, feedback enabled, not
    blocked, no version to compare against, no notes.
    """

    wish_list: list[str] = field(default_factory=list)
    confidence_floor: int = 0
    feedback_enabled: bool = True
    blocked: bool = False
    version: int = 0
    notes: list[ModeratorNote] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ModelSettings:
        # The contract spells every property lowercase; folding the keys keeps
        # a PascalCase serializer working too, as the rest of this file does.
        low = {str(k).lower(): v for k, v in d.items()}

        def as_int(key: str, default: int) -> int:
            try:
                value = low.get(key, default)
                return default if isinstance(value, bool) else int(value)
            except (TypeError, ValueError):
                return default

        raw_wish = low.get("wishlist") or []
        wish = [n.strip() for n in raw_wish if isinstance(n, str) and n.strip()] if isinstance(raw_wish, list) else []
        raw_notes = low.get("notes") or []
        notes: list[ModeratorNote] = []
        if isinstance(raw_notes, list):
            for entry in raw_notes:
                note = _note_from_json(entry)
                if note is not None:
                    notes.append(note)
        return cls(
            wish_list=wish,
            confidence_floor=as_int("confidencefloor", 0),
            feedback_enabled=bool(low.get("feedbackenabled", True)),
            blocked=bool(low.get("blocked", False)),
            version=as_int("version", 0),
            notes=notes,
        )


def _note_from_json(entry: Any) -> ModeratorNote | None:
    """One note, or ``None`` for anything without an id and a body."""
    if not isinstance(entry, dict):
        return None
    low = {str(k).lower(): v for k, v in entry.items()}
    text = low.get("note")
    if not isinstance(text, str) or not text.strip():
        return None
    raw_id = low.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return None
    try:
        note_id = int(raw_id)
    except ValueError:
        return None
    created = low.get("created")
    return ModeratorNote(id=note_id, note=text.strip(), created=str(created or ""))


@dataclass
class UserMetaData:
    profile_name: str = ""
    country: str = ""
    roles: int = 0  # bitfield: 1=Read, 2=Contribute, 4=Admin

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> UserMetaData:
        return cls(
            profile_name=d.get("ProfileName", d.get("profileName", "")),
            country=d.get("Country", d.get("country", "")),
            roles=int(d.get("Roles", d.get("roles", 0)) or 0),
        )

    def can_contribute(self) -> bool:
        return bool(self.roles & 2)


class CommunityApiError(Exception):
    pass


class CommunityApi:
    def __init__(
        self,
        auth: AuthManager,
        *,
        base_url: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        verify: bool | str | None = None,
    ) -> None:
        self.auth = auth
        self.base_url = (base_url or appenv.api_base()).rstrip("/")
        self.session = session or _session
        self.timeout = timeout
        # Passed to every call — including the Azure-blob URLs the API hands
        # back, since a local backend pairs with a local blob emulator whose
        # cert needs the same trust.
        #
        # It has to be per request, NOT `session.verify`: requests lets
        # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in the environment outrank the
        # session attribute, so behind a corporate proxy (or any box that sets
        # those) a session-level setting is silently ignored. A per-request
        # value wins. Leaving it True keeps the env bundle working as before.
        self.verify = appenv.tls_verify() if verify is None else verify

    # ----- helpers ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        result = self.auth.acquire_token_silent(API_SCOPES)
        if result is None:
            raise CommunityApiError("Not authenticated. Sign in first.")
        return {"Authorization": f"Bearer {result.access_token}"}

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.session.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=self.timeout,
            verify=self.verify,
            **kwargs,
        )

    def _post(self, path: str, json: Any = None) -> requests.Response:
        return self.session.post(
            f"{self.base_url}{path}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=json,
            timeout=self.timeout,
            verify=self.verify,
        )

    # ----- user metadata ------------------------------------------------------

    def get_user_metadata(self) -> UserMetaData | None:
        resp = self._get("/Users/GetUserMetaData")
        if resp.status_code != 200:
            return None
        return UserMetaData.from_json(resp.json())

    def update_user_metadata(self, meta: UserMetaData) -> bool:
        resp = self._post(
            "/Users/UpdateUserMetaData",
            json={
                "ProfileName": meta.profile_name,
                "Country": meta.country,
            },
        )
        return resp.status_code == 200

    def is_username_available(self, name: str) -> bool:
        resp = self._get(f"/Users/EnsureUserCommunityNameAvailable?username={quote_plus(name)}")
        return resp.status_code == 200 and resp.text.strip().lower() == "true"

    # ----- cartridges / models ------------------------------------------------

    def get_available_cartridges(self) -> list[CartridgeInfo]:
        resp = self._get("/Models/GetAvailableCartridges")
        if resp.status_code != 200:
            raise CommunityApiError(f"GetAvailableCartridges → {resp.status_code}")
        return [CartridgeInfo.from_json(d) for d in resp.json() or []]

    def get_models(
        self,
        search: str = "",
        model_type: str = "",
        cartridge: str = "",
    ) -> list[ModelInfo]:
        qs = f"?searchQuery={quote_plus(search)}&ModelType={quote_plus(model_type)}&Cartridge={quote_plus(cartridge)}"
        resp = self._get(f"/Models/GetModels{qs}")
        if resp.status_code != 200:
            raise CommunityApiError(f"GetModels → {resp.status_code}")
        return [ModelInfo.from_json(d) for d in resp.json() or []]

    def request_download(self, model_uid: str) -> dict[str, Any]:
        resp = self._get(f"/Models/RequestDownloadModel/{quote_plus(model_uid)}")
        if resp.status_code != 200:
            raise CommunityApiError(f"RequestDownloadModel → {resp.status_code}")
        return resp.json()

    # ----- file download ------------------------------------------------------

    def download_to(
        self,
        url: str,
        dest: Path | str,
        *,
        progress: Callable[[int, int | None], None] | None = None,
        chunk_size: int = 64 * 1024,
        expected_total: int | None = None,
    ) -> Path:
        """Stream a URL (typically an Azure-blob SAS URL) to disk with progress.

        Uses an atomic-write `.tmp` → `os.replace()` so a failure can't leave
        a partial file at the destination.
        """
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")

        with self.session.get(
            url,
            stream=True,
            timeout=self.timeout,
            verify=self.verify,
        ) as resp:
            resp.raise_for_status()
            total = expected_total
            if total is None:
                cl = resp.headers.get("Content-Length")
                if cl is not None:
                    try:
                        total = int(cl)
                    except ValueError:
                        total = None
            bytes_done = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    bytes_done += len(chunk)
                    if progress is not None:
                        progress(bytes_done, total)
        import os

        os.replace(tmp, dest_path)
        return dest_path

    # ----- feedback-loop upload ----------------------------------------------

    def request_feedback_upload(
        self,
        *,
        filename: str,
        community_model_uid: str,
        classification: str,
        confidence: int,
    ) -> FeedbackUploadTicket | None:
        """Ask the server for a SAS ticket to upload one feedback image.

        Returns ``None`` when the endpoint is unavailable for this user (the
        server answers with an HTML error page rather than JSON).
        """
        resp = self._post(
            "/Models/FeedbackImageUploadRequest",
            json={
                "filename": filename,
                "CommunityModelUID": community_model_uid,
                "classification": classification,
                "confidence": confidence,
            },
        )
        log.debug("POST /Models/FeedbackImageUploadRequest -> HTTP %s", resp.status_code)
        if resp.status_code != 200:
            log.debug("  non-200 body: %r", (resp.text or "")[:200])
            return None
        text = (resp.text or "").lstrip()
        if text.startswith("<!DOCTYPE html>") or text.startswith("<html"):
            log.debug("  HTML error page — endpoint not available for this user")
            return None
        try:
            data = resp.json()
        except ValueError:
            log.debug("  non-JSON body: %r", text[:200])
            return None
        if not isinstance(data, dict):
            log.debug("  unexpected JSON shape: %s", type(data).__name__)
            return None
        ticket = FeedbackUploadTicket.from_json(data)
        log.debug(
            "  ticket: accepted=%s container=%r blob=%r",
            ticket.feedback_accepted,
            ticket.container_uri,
            ticket.blob_path,
        )
        return ticket

    def fetch_wish_list(self, community_model_uid: str) -> list[str]:
        """Classifications this community model wants more training images for.

        ``GET /Models/FetchWishList?communityModelId={uid}`` answers with a JSON
        array of classification names (the same strings the classifier returns).

        The server has no error path — an unknown or malformed UID, a disabled
        feedback loop, no configured threshold, or an unpopulated image folder
        all answer ``200 []``, meaning "don't collect anything extra". We extend
        that to our side too: not signed in, a non-200, or an unparseable body
        all return ``[]`` so the feature fails open to confidence-only feedback
        and a run is never blocked on this call.
        """
        if not community_model_uid:
            return []
        try:
            resp = self._get(f"/Models/FetchWishList?communityModelId={quote_plus(community_model_uid)}")
        except Exception as exc:
            log.debug("GET /Models/FetchWishList failed (%s: %s)", exc.__class__.__name__, exc)
            return []
        log.debug("GET /Models/FetchWishList -> HTTP %s", resp.status_code)
        if resp.status_code != 200:
            log.debug("  non-200 body: %r", (resp.text or "")[:200])
            return []
        try:
            data = resp.json()
        except ValueError:
            log.debug("  non-JSON body: %r", (resp.text or "")[:200])
            return []
        if not isinstance(data, list):
            log.debug("  unexpected JSON shape: %s", type(data).__name__)
            return []
        names = [item.strip() for item in data if isinstance(item, str) and item.strip()]
        log.debug("  wish list (%d): %s", len(names), names)
        return names

    def fetch_model_settings(self, community_model_uid: str) -> ModelSettings | None:
        """Server-side settings for one community model, or ``None``.

        ``GET /Models/FetchModelSettings?communityModelId={uid}`` answers with
        the wish list, the publisher's confidence floor, the feedback
        shutoff/block flags, the currently published version and any moderator
        notes for the calling user.

        Fails open exactly like ``fetch_wish_list``, but with ``None`` rather
        than a neutral value so a caller can tell "the server said nothing"
        from "the server said no": an empty UID, not being signed in, a network
        error, a non-200 or an unparseable body all answer ``None``, and the
        caller then behaves as it does offline — local floor, local opt-in, no
        version prompt, no notes.
        """
        if not community_model_uid:
            return None
        try:
            resp = self._get(f"/Models/FetchModelSettings?communityModelId={quote_plus(community_model_uid)}")
        except Exception as exc:
            log.debug("GET /Models/FetchModelSettings failed (%s: %s)", exc.__class__.__name__, exc)
            return None
        log.debug("GET /Models/FetchModelSettings -> HTTP %s", resp.status_code)
        if resp.status_code != 200:
            log.debug("  non-200 body: %r", (resp.text or "")[:200])
            return None
        try:
            data = resp.json()
        except ValueError:
            log.debug("  non-JSON body: %r", (resp.text or "")[:200])
            return None
        if not isinstance(data, dict):
            log.debug("  unexpected JSON shape: %s", type(data).__name__)
            return None
        settings = ModelSettings.from_json(data)
        log.debug(
            "  settings: version=%s floor=%s enabled=%s blocked=%s wish=%d notes=%d",
            settings.version,
            settings.confidence_floor,
            settings.feedback_enabled,
            settings.blocked,
            len(settings.wish_list),
            len(settings.notes),
        )
        return settings

    def upload_feedback_blob(
        self,
        file_path: Path | str,
        ticket: FeedbackUploadTicket,
        *,
        version: int,
    ) -> None:
        """PUT the image bytes to the SAS blob URL (no azure-storage-blob dep).

        Single Put-Blob REST call: sets the block-blob type and stamps the
        model version as blob metadata.
        """
        url = ticket.blob_put_url()
        headers = {
            "x-ms-blob-type": "BlockBlob",
            "x-ms-meta-version": str(version),
            "Content-Type": "image/jpeg",
        }
        with open(file_path, "rb") as f:
            resp = self.session.put(
                url,
                data=f,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify,
            )
        log.debug("PUT blob -> HTTP %s", resp.status_code)
        resp.raise_for_status()

    def complete_feedback_upload(self, ticket: FeedbackUploadTicket) -> bool:
        """Notify the server the upload finished."""
        resp = self._post("/Models/CompleteFeedbackUpload", json=ticket.raw)
        log.debug("POST /Models/CompleteFeedbackUpload -> HTTP %s", resp.status_code)
        return resp.status_code < 400

    # ----- model share (export + upload) -------------------------------------

    def request_file_upload(
        self,
        *,
        filename: str,
        model_info: dict[str, Any],
    ) -> SasResponse:
        """Ask the server for a SAS ticket to upload the model ZIP.

        POSTs a ``FileUploadReq { filename, ModelInfo }``. Raises
        ``CommunityApiError`` with the server's status + body on failure so the
        UI can show why the upload was refused, rather than a generic "no SAS".
        """
        resp = self._post(
            "/Models/FileUploadRequest",
            json={"filename": filename, "ModelInfo": model_info},
        )
        log.debug("POST /Models/FileUploadRequest -> HTTP %s", resp.status_code)
        text = (resp.text or "").strip()
        if resp.status_code != 200:
            log.debug("  non-200 body: %r", text[:300])
            raise CommunityApiError(f"FileUploadRequest → HTTP {resp.status_code}: {text[:300] or '(empty body)'}")
        if text.startswith("<!DOCTYPE html>") or text.lower().startswith("<html"):
            raise CommunityApiError(
                "The upload endpoint is not available for this account (it requires the Contribute community role)."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise CommunityApiError(f"FileUploadRequest returned non-JSON: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise CommunityApiError("FileUploadRequest returned an unexpected payload.")
        ticket = SasResponse.from_json(data)
        if not ticket.sas_token and not ticket.full_url:
            raise CommunityApiError("FileUploadRequest returned no SAS token.")
        return ticket

    def upload_blob(
        self,
        file_path: Path | str,
        ticket: SasResponse,
        *,
        content_type: str = "application/octet-stream",
        progress: Callable[[int, int], None] | None = None,
        retries: int = 3,
    ) -> None:
        """PUT a file to its SAS blob URL with optional progress.

        No ``azure-storage-blob`` dependency — a single Put-Blob REST call,
        same approach as the feedback-image upload. The body is a file-like
        object (not a generator) so ``requests`` sets a Content-Length and does
        NOT use chunked encoding, which Azure rejects. ``progress(sent, total)``
        fires as the body is read. Transient SSL/connection drops are retried
        with exponential backoff (a fresh reader each attempt).
        """
        total = os.path.getsize(file_path)
        url = ticket.blob_put_url()
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type}

        last_exc: Exception | None = None
        for attempt in range(max(1, retries)):
            reader = _ProgressReader(file_path, total, progress)
            try:
                resp = self.session.put(
                    url,
                    data=reader,
                    headers=headers,
                    timeout=None,
                    verify=self.verify,
                )
                log.debug("PUT blob (%s bytes) -> HTTP %s", total, resp.status_code)
                resp.raise_for_status()
                return
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                log.debug("  blob PUT attempt %d failed: %s", attempt + 1, exc)
                if attempt < retries - 1:
                    time.sleep(2**attempt)
            finally:
                reader.close()
        if last_exc is not None:
            raise last_exc

    def complete_upload(self, ticket: SasResponse) -> bool:
        """Notify the server the model ZIP finished."""
        resp = self._post("/Models/CompleteUpload", json=ticket.raw)
        log.debug("POST /Models/CompleteUpload -> HTTP %s", resp.status_code)
        return resp.status_code < 400

    def request_manifest_upload(self, blob_path: str) -> SasResponse | None:
        """Ask for a SAS ticket to upload the standalone manifest.

        POSTs ``{ BlobPath }`` (the model ZIP's blob path) so the server pairs
        the manifest with the model.
        """
        resp = self._post("/Models/ManifestUploadRequest", json={"BlobPath": blob_path})
        log.debug("POST /Models/ManifestUploadRequest -> HTTP %s", resp.status_code)
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return SasResponse.from_json(data)

    def upload_manifest(self, file_path: Path | str, ticket: SasResponse) -> None:
        """PUT the standalone ``.manifest.json`` to its own SAS blob."""
        self.upload_blob(file_path, ticket, content_type="application/json")

    def share_model(
        self,
        *,
        zip_path: Path | str,
        manifest_path: Path | str,
        model_info: dict[str, Any],
        progress: Callable[[int, int], None] | None = None,
    ) -> str | None:
        """Run the full share sequence and return the server's model UID.

        Order:
          1. FileUploadRequest  → SAS for the ZIP,
          2. PUT the ZIP        (progress reported),
          3. CompleteUpload,
          4. ManifestUploadRequest → SAS for the manifest,
          5. PUT the manifest by itself.

        The manifest is always uploaded once the model completes. Raises
        CommunityApiError on a failed handshake.
        """
        zip_path = Path(zip_path)
        manifest_path = Path(manifest_path)
        # Raises CommunityApiError (with the server's reason) if refused.
        ticket = self.request_file_upload(filename=zip_path.name, model_info=model_info)
        self.upload_blob(
            zip_path,
            ticket,
            content_type="application/zip",
            progress=progress,
        )
        self.complete_upload(ticket)
        final_uid = (
            ticket.model_info.get("ModelUID")
            or ticket.model_info.get("modelUID")
            or model_info.get("CommunityModelUID")
        )
        # Step 4/5: upload the manifest on its own. Best-effort but logged —
        # a missing BlobPath or SAS means the server didn't want it.
        if manifest_path.exists() and ticket.blob_path:
            manifest_sas = self.request_manifest_upload(ticket.blob_path)
            if manifest_sas is not None:
                self.upload_manifest(manifest_path, manifest_sas)
            else:
                log.debug("Manifest upload skipped: no SAS for %r", ticket.blob_path)
        return final_uid
