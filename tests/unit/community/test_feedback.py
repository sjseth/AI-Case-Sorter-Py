"""Tests for the community feedback-loop service (sorter/community/feedback.py).

The queue is the on-disk ``data/models/<id>/feedback_images/`` folder — no DB
rows. The upload path is exercised with a fake ``CommunityApi`` (monkeypatched
onto the module the service lazily imports) and a fake auth — no network.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sorter import paths
from sorter.community.feedback import FeedbackService, is_feedback_model
from sorter.data.db import Database
from sorter.data.models import Model
from sorter.data.repository import CartridgeRepo, ModelRepo
from sorter.training.dataset import feedback_filename, parse_feedback_filename


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    database = Database(tmp_path / "fb.db")
    database.ensure_initialized()
    return database


def _model(db, *, enabled=True, floor=95, mode="Instant", community="uid-1") -> Model:
    cart = CartridgeRepo(db).get_or_create("9mm")
    m = Model(
        name="Comm",
        cartridge_id=cart.id,
        community_model_uid=community,
        model_version=2,
        feedback_loop_enabled=enabled,
        feedback_loop_confidence_floor=floor,
        feedback_loop_upload_mode=mode,
    )
    return ModelRepo(db).create(m)


def _img() -> np.ndarray:
    return np.full((480, 480, 3), 128, dtype=np.uint8)


# ----- fakes -----------------------------------------------------------------


class _FakeTicket:
    def __init__(self, accepted: bool = True) -> None:
        self.feedback_accepted = accepted
        self.feedback_message = "" if accepted else "rate limited"


class _FakeApi:
    accept = True
    last: _FakeApi | None = None

    def __init__(self, auth=None, **kw) -> None:
        self.requests: list[dict] = []
        self.uploads: list[tuple[str, int]] = []
        self.completes = 0
        _FakeApi.last = self

    def request_feedback_upload(self, **kw):
        self.requests.append(kw)
        return _FakeTicket(_FakeApi.accept)

    def upload_feedback_blob(self, path, ticket, *, version):
        self.uploads.append((str(path), version))

    def complete_feedback_upload(self, ticket):
        self.completes += 1
        return True


class _OkAuth:
    def acquire_token_silent(self, scopes=None):
        return object()


# ----- filename convention ---------------------------------------------------


def test_feedback_filename_round_trips() -> None:
    name = feedback_filename("WIN", 87.4)
    assert name.startswith("WIN__87__")
    assert parse_feedback_filename(name) == ("WIN", 87)


def test_parse_feedback_filename_rejects_non_feedback_names() -> None:
    assert parse_feedback_filename("WIN__639180.jpg") is None  # training name (2 parts)
    assert parse_feedback_filename("nope.jpg") is None


# ----- policy ----------------------------------------------------------------


def test_is_feedback_model() -> None:
    assert is_feedback_model(Model(community_model_uid="u", feedback_loop_enabled=True))
    assert not is_feedback_model(Model(community_model_uid="u", feedback_loop_enabled=False))
    assert not is_feedback_model(Model(community_model_uid=None, feedback_loop_enabled=True))
    assert not is_feedback_model(None)


def test_effective_floor_clamped_to_50(db) -> None:
    svc = FeedbackService(db)
    assert svc.effective_floor(_model(db, floor=95)) == 95
    assert svc.effective_floor(_model(db, floor=30, community="uid-2")) == 50


def test_should_capture_matrix(db) -> None:
    svc = FeedbackService(db)
    m = _model(db, floor=95)
    assert svc.should_capture(m, 80.0) is True
    assert svc.should_capture(m, 94.9) is True
    assert svc.should_capture(m, 95.0) is False  # at floor → not captured
    assert svc.should_capture(m, 99.0) is False
    assert svc.should_capture(m, -1) is False  # unknown confidence
    assert svc.should_capture(_model(db, enabled=False, community="u2"), 10) is False
    assert svc.should_capture(_model(db, community=None), 10) is False
    assert svc.should_capture(None, 10) is False


def test_should_capture_floor_below_50_is_clamped(db) -> None:
    svc = FeedbackService(db)
    m = _model(db, floor=30)
    assert svc.should_capture(m, 40) is True  # < 50 effective floor
    assert svc.should_capture(m, 55) is False  # >= 50 effective floor


# ----- capture ---------------------------------------------------------------


def test_capture_writes_file_to_model_folder(db) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    path = svc.capture(m, _img(), "WIN", 42.0)
    assert path and Path(path).exists()
    assert Path(path).parent == paths.model_feedback_dir(m.id)
    assert Path(path).name.startswith("WIN__42__")
    assert svc.count_pending(m.id) == 1
    assert svc.has_pending(m.id) is True


def test_capture_empty_label_uses_unknown(db) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    path = svc.capture(m, _img(), "", 10.0)
    assert path and Path(path).name.startswith("unknown__")


# ----- upload ----------------------------------------------------------------


def test_upload_pending_happy_path(db, monkeypatch) -> None:
    monkeypatch.setattr("sorter.community.community_api.CommunityApi", _FakeApi)
    _FakeApi.accept = True
    svc = FeedbackService(db)
    m = _model(db, mode="Manual")
    p1 = svc.capture(m, _img(), "WIN", 10)
    p2 = svc.capture(m, _img(), "FC", 20)
    assert p1 is not None and p2 is not None  # capture only returns None if the save itself fails
    result = svc.upload_pending(m.id, auth=_OkAuth())
    assert result["uploaded"] == 2
    assert result["declined"] is False
    assert svc.count_pending(m.id) == 0
    assert not Path(p1).exists() and not Path(p2).exists()
    # Blob metadata version mirrors the model version; request carries label+confidence.
    assert _FakeApi.last is not None
    assert all(v == 2 for _, v in _FakeApi.last.uploads)
    confidences = sorted(r["confidence"] for r in _FakeApi.last.requests)
    assert confidences == [10, 20]
    assert _FakeApi.last.completes == 2


def test_upload_pending_declined_stops_and_drops(db, monkeypatch) -> None:
    monkeypatch.setattr("sorter.community.community_api.CommunityApi", _FakeApi)
    _FakeApi.accept = False
    svc = FeedbackService(db)
    m = _model(db)
    svc.capture(m, _img(), "WIN", 10)
    svc.capture(m, _img(), "FC", 20)
    result = svc.upload_pending(m.id, auth=_OkAuth())
    assert result["declined"] is True
    assert result["uploaded"] == 0
    assert svc.count_pending(m.id) == 0  # remaining files dropped


def test_upload_pending_no_auth_drops_silently(db) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    svc.capture(m, _img(), "WIN", 10)
    result = svc.upload_pending(m.id, auth=None)
    assert result["skipped"] is True
    assert svc.count_pending(m.id) == 0


def test_upload_pending_opted_out_drops(db) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    svc.capture(m, _img(), "WIN", 10)
    m.feedback_loop_enabled = False  # user opts out after capture
    ModelRepo(db).update(m)
    result = svc.upload_pending(m.id, auth=_OkAuth())
    assert result["uploaded"] == 0
    assert svc.count_pending(m.id) == 0


def test_upload_pending_empty_queue_is_noop(db) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    result = svc.upload_pending(m.id, auth=_OkAuth())
    assert result == {"uploaded": 0, "dropped": 0, "declined": False, "skipped": False}


# ----- wish list (model-balancing feedback) ----------------------------------


class _WishApi:
    """Fake CommunityApi that records the UID it was asked about."""

    names: list[str] = []
    boom = False
    calls: list[str] = []

    def __init__(self, auth=None, **kw) -> None:
        pass

    def fetch_wish_list(self, uid: str) -> list[str]:
        _WishApi.calls.append(uid)
        if _WishApi.boom:
            raise RuntimeError("offline")
        return list(_WishApi.names)


@pytest.fixture
def wish_api(monkeypatch):
    import sorter.community.community_api as ca

    _WishApi.names, _WishApi.boom, _WishApi.calls = [], False, []
    monkeypatch.setattr(ca, "CommunityApi", _WishApi)
    return _WishApi


def test_set_wish_list_normalises_and_clears(db) -> None:
    svc = FeedbackService(db)
    # Deliberately includes a `None` — this list models the wire shape from
    # `GET /Models/FetchWishList` (untrusted JSON), and `set_wish_list`'s
    # `if n and n.strip()` filter is what makes that safe; the type signature
    # (`Iterable[str]`) is the caller's contract, not what's fed here on purpose.
    svc.set_wish_list(1, ["  FC ", "R-P", "", None, "fc"])  # ty: ignore[invalid-argument-type]
    assert svc.wish_list() == ["fc", "r-p"]
    svc.clear_wish_list()
    assert svc.wish_list() == []


def test_wish_list_captures_regardless_of_confidence(db) -> None:
    svc = FeedbackService(db)
    m = _model(db, floor=95)
    svc.set_wish_list(m.id, ["WIN 9MM"])
    # Well above the floor, but wanted → captured (case-insensitively).
    assert svc.should_capture(m, 100.0, "win 9mm", wish_list=True) is True
    # Not wanted, above floor → still not captured.
    assert svc.should_capture(m, 100.0, "FC", wish_list=True) is False


def test_wish_list_ignored_outside_a_continuous_run(db) -> None:
    """Manual Feed passes wish_list=False and keeps confidence-only behavior."""
    svc = FeedbackService(db)
    m = _model(db, floor=95)
    svc.set_wish_list(m.id, ["WIN"])
    assert svc.should_capture(m, 100.0, "WIN") is False
    assert svc.should_capture(m, 90.0, "WIN") is True  # confidence rule still applies


def test_wish_list_is_bound_to_its_model(db) -> None:
    svc = FeedbackService(db)
    m = _model(db, floor=95)
    other = _model(db, floor=95, community="uid-2")
    svc.set_wish_list(other.id, ["WIN"])
    assert svc.should_capture(m, 100.0, "WIN", wish_list=True) is False


def test_wish_list_requires_a_feedback_model(db) -> None:
    svc = FeedbackService(db)
    m = _model(db, enabled=False)
    svc.set_wish_list(m.id, ["WIN"])
    assert svc.should_capture(m, 100.0, "WIN", wish_list=True) is False


def test_wish_list_capped_per_label_per_run(db) -> None:
    from sorter.community.feedback import MAX_WISH_LIST_CAPTURES_PER_LABEL as CAP

    svc = FeedbackService(db)
    m = _model(db, floor=95)
    svc.set_wish_list(m.id, ["WIN", "FC"])
    assert all(svc.should_capture(m, 100.0, "WIN", wish_list=True) for _ in range(CAP))
    assert svc.should_capture(m, 100.0, "WIN", wish_list=True) is False
    # The cap is per classification…
    assert svc.should_capture(m, 100.0, "FC", wish_list=True) is True
    # …and the confidence rule stays unbounded.
    assert svc.should_capture(m, 10.0, "WIN", wish_list=True) is True
    # A new run reinstalls the list and refills the quota.
    svc.set_wish_list(m.id, ["WIN"])
    assert svc.should_capture(m, 100.0, "WIN", wish_list=True) is True


def test_below_floor_capture_does_not_spend_wish_quota(db) -> None:
    from sorter.community.feedback import MAX_WISH_LIST_CAPTURES_PER_LABEL as CAP

    svc = FeedbackService(db)
    m = _model(db, floor=95)
    svc.set_wish_list(m.id, ["WIN"])
    for _ in range(CAP):
        assert svc.should_capture(m, 10.0, "WIN", wish_list=True) is True
    assert svc.should_capture(m, 100.0, "WIN", wish_list=True) is True


def test_refresh_wish_list_installs_server_names(db, wish_api) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    wish_api.names = ["FC", "R-P"]
    assert svc.refresh_wish_list(m, auth=_OkAuth()) == ["FC", "R-P"]
    assert svc.wish_list() == ["fc", "r-p"]
    assert wish_api.calls == ["uid-1"]


def test_refresh_wish_list_skips_non_feedback_models_and_signed_out(db, wish_api) -> None:
    """The auth path is never touched when the loop is off or nobody's signed in."""
    svc = FeedbackService(db)
    wish_api.names = ["FC"]
    assert svc.refresh_wish_list(_model(db, enabled=False), auth=_OkAuth()) == []
    assert svc.refresh_wish_list(_model(db, community=None), auth=_OkAuth()) == []
    assert svc.refresh_wish_list(None, auth=_OkAuth()) == []
    assert svc.refresh_wish_list(_model(db, community="uid-9"), auth=None) == []
    assert wish_api.calls == []


def test_refresh_wish_list_fails_open_on_error(db, wish_api) -> None:
    svc = FeedbackService(db)
    m = _model(db)
    svc.set_wish_list(m.id, ["stale"])
    wish_api.boom = True
    assert svc.refresh_wish_list(m, auth=_OkAuth()) == []
    assert svc.wish_list() == []  # the stale list is dropped too
