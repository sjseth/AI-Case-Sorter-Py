"""Tests for the auth manager.

These tests mock `msal.PublicClientApplication` to avoid any real network
interaction. Real interactive login is a manual smoke test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sorter.community.auth import AuthError, AuthManager, PortInUseError


class _FakeClient:
    def __init__(self, *, token_cache: Any) -> None:
        self.token_cache = token_cache
        self._accounts: list[dict[str, Any]] = []
        self.next_interactive_result: dict[str, Any] | None = None
        self.next_interactive_exc: BaseException | None = None
        self.next_silent_result: dict[str, Any] | None = None

    def get_accounts(self) -> list[dict[str, Any]]:
        return list(self._accounts)

    def acquire_token_interactive(self, **_kwargs: Any) -> dict[str, Any]:
        if self.next_interactive_exc is not None:
            raise self.next_interactive_exc
        self._accounts = [{"home_account_id": "u1", "username": "u1@example.com"}]
        return self.next_interactive_result or {}

    def acquire_token_silent(self, _scopes: Any, account: Any) -> dict[str, Any] | None:
        return self.next_silent_result

    def remove_account(self, account: dict[str, Any]) -> None:
        self._accounts = [a for a in self._accounts if a != account]


def _mgr(tmp_path: Path) -> AuthManager:
    return AuthManager(cache_path=tmp_path / "cache.bin", client_factory=_FakeClient)


def _fake_client(mgr: AuthManager) -> _FakeClient:
    """`AuthManager._client` is typed `Any | PublicClientApplication` since it
    can hold a real MSAL client; every test here builds it via
    `client_factory=_FakeClient`, so narrow back to the fake to poke its
    test-only knobs."""
    assert isinstance(mgr._client, _FakeClient)
    return mgr._client


def test_initially_not_authenticated(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.current_account() is None
    assert not mgr.is_authenticated()
    assert mgr.acquire_token_silent() is None


def test_login_interactive_success(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_result = {
        "access_token": "TOKEN_A",
        "expires_in": 3600,
        "id_token_claims": {"name": "Test User", "emails": ["test@example.com"]},
    }
    result = mgr.login_interactive()
    assert result.access_token == "TOKEN_A"
    assert result.name == "Test User"
    assert result.email == "test@example.com"
    assert mgr.is_authenticated()


def test_login_failure_raises_auth_error(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_result = {"error_description": "user cancelled"}
    with pytest.raises(AuthError) as exc:
        mgr.login_interactive()
    assert "user cancelled" in str(exc.value)


def test_port_in_use_surfaced(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_exc = OSError(98, "address already in use")
    with pytest.raises(PortInUseError):
        mgr.login_interactive()


def test_acquire_silent_after_login(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_result = {
        "access_token": "T1",
        "id_token_claims": {"name": "X", "emails": ["x@x.com"]},
    }
    mgr.login_interactive()
    _fake_client(mgr).next_silent_result = {
        "access_token": "T2",
        "id_token_claims": {"name": "X", "emails": ["x@x.com"]},
    }
    res = mgr.acquire_token_silent()
    assert res is not None
    assert res.access_token == "T2"


def test_logout_clears_account(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_result = {
        "access_token": "T",
        "id_token_claims": {"emails": ["x@x.com"]},
    }
    mgr.login_interactive()
    assert mgr.is_authenticated()
    mgr.logout()
    assert not mgr.is_authenticated()


def test_identity_returns_none_when_signed_out(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    assert mgr.identity() == (None, None)


def test_identity_prefers_account_claims(tmp_path: Path) -> None:
    """When the account dict carries id_token_claims, use them directly
    (no silent token call needed)."""
    mgr = _mgr(tmp_path)
    _fake_client(mgr)._accounts = [
        {
            "home_account_id": "u1",
            # B2C username is the ugly policy-prefixed object id.
            "username": "abc123-b2c_1_signinup.def456",
            "id_token_claims": {"name": "Jane Doe", "emails": ["jane@example.com"]},
        }
    ]
    name, email = mgr.identity()
    assert name == "Jane Doe"
    assert email == "jane@example.com"


def test_identity_falls_back_to_silent_token(tmp_path: Path) -> None:
    """Account dict without claims → resolve via a silent token read."""
    mgr = _mgr(tmp_path)
    _fake_client(mgr)._accounts = [
        {
            "home_account_id": "u1",
            "username": "abc123-b2c_1_signinup.def456",
        }
    ]
    _fake_client(mgr).next_silent_result = {
        "access_token": "T",
        "id_token_claims": {"name": "Bob", "emails": ["bob@example.com"]},
    }
    name, email = mgr.identity()
    assert name == "Bob"
    assert email == "bob@example.com"


def _make_jwt(claims: dict) -> str:
    """Build an unsigned JWT (header.payload.sig) carrying `claims`."""
    import base64
    import json

    def _seg(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_seg({'alg': 'none'})}.{_seg(claims)}.sig"


def test_decode_jwt_claims_roundtrip() -> None:
    from sorter.community.auth import _decode_jwt_claims

    token = _make_jwt({"name": "Jane", "emails": ["jane@example.com"]})
    claims = _decode_jwt_claims(token)
    assert claims["name"] == "Jane"
    assert claims["emails"] == ["jane@example.com"]


def test_decode_jwt_claims_garbage_returns_empty() -> None:
    from sorter.community.auth import _decode_jwt_claims

    assert _decode_jwt_claims("not-a-jwt") == {}
    assert _decode_jwt_claims("") == {}


def test_extract_name_falls_back_to_given_family() -> None:
    from sorter.community.auth import _extract_name

    assert _extract_name({"name": "Full Name"}) == "Full Name"
    assert _extract_name({"given_name": "Ada", "family_name": "Lovelace"}) == "Ada Lovelace"
    assert _extract_name({"given_name": "Ada"}) == "Ada"
    assert _extract_name({}) is None


def test_identity_uses_stashed_claims_after_login(tmp_path: Path, monkeypatch) -> None:
    """After login_interactive, identity() must immediately return the new
    user's name+email — even before the MSAL cache search would surface
    the freshly-written token. Regression: re-login within the same
    process used to leave the Community Info label blank until restart.
    """
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_result = {
        "access_token": "T",
        "id_token_claims": {"name": "Fresh User", "emails": ["fresh@example.com"]},
    }
    mgr.login_interactive()
    # Even with no cache hit + no account claims, the stashed claims win.
    monkeypatch.setattr(mgr._cache, "search", lambda *_a, **_k: [])
    _fake_client(mgr)._accounts[0].pop("id_token_claims", None)
    name, email = mgr.identity()
    assert name == "Fresh User"
    assert email == "fresh@example.com"


def test_identity_stash_cleared_on_logout(tmp_path: Path) -> None:
    """A subsequent identity() call after logout must not return the
    previous user's claims."""
    mgr = _mgr(tmp_path)
    _fake_client(mgr).next_interactive_result = {
        "access_token": "T",
        "id_token_claims": {"name": "Old User", "emails": ["old@example.com"]},
    }
    mgr.login_interactive()
    mgr.logout()
    assert mgr.identity() == (None, None)
    assert mgr._last_claims is None


def test_identity_decodes_cached_id_token(tmp_path: Path, monkeypatch) -> None:
    """The primary path: decode the ID token JWT held in the MSAL cache.

    This is what makes the label survive a cold restart — no account-dict
    claims, no silent token, just the cached id token.
    """
    mgr = _mgr(tmp_path)
    _fake_client(mgr)._accounts = [
        {
            "home_account_id": "u1",
            "username": "abc123-b2c_1_signinup.def456",
        }
    ]
    jwt = _make_jwt({"name": "Cached User", "emails": ["cached@example.com"]})
    # Stub the cache search to return an ID-token entry carrying the JWT.
    monkeypatch.setattr(mgr._cache, "search", lambda *_a, **_k: [{"secret": jwt}])
    name, email = mgr.identity()
    assert name == "Cached User"
    assert email == "cached@example.com"
    # No silent call should have been needed.
    assert _fake_client(mgr).next_silent_result is None


def test_cache_file_persisted_and_chmod(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.bin"
    mgr = AuthManager(cache_path=cache_path, client_factory=_FakeClient)
    # Force a write by toggling the internal state flag.
    mgr._cache.has_state_changed = True
    mgr._cache.flush()
    assert cache_path.exists()
    import os
    import sys

    if sys.platform == "win32":
        # os.chmod on Windows doesn't raise for the bits sorter/community/auth.py sets
        # (matching its own try/except OSError comment), but it also doesn't
        # apply POSIX permission semantics -- real access control there is
        # NTFS ACLs, not st_mode. A new file's owner-only ACL is inherited
        # regardless of what st_mode reports, so there's nothing meaningful
        # to assert about st_mode on this platform.
        return

    mode = os.stat(cache_path).st_mode & 0o777
    if mode != 0:
        assert mode in (0o600, 0o400, 0o700)
