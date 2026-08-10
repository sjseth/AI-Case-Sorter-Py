"""Azure B2C authentication via MSAL.

Uses the same client/tenant, B2C policy, redirect URI, and scopes as the
legacy Windows app so accounts interchange. The token cache is a file under
the platform-specific user data dir.

Authentication is OPTIONAL — the app boots fine without it. The community
tab is the only gated surface.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msal

from .. import paths

CLIENT_ID = "9b0e9de2-e652-47f6-ad7d-c93a90be8a2e"
TENANT_ID = "704a9dfb-f600-47db-b95f-28ea72de1ab3"
B2C_AUTHORITY = "https://sjsreloadingrecipes.b2clogin.com/tfp/sjsreloadingrecipes.onmicrosoft.com/B2C_1_SignInUp"
REDIRECT_URI = "http://localhost:44300/"
REDIRECT_PORT = 44300

LOGIN_SCOPES: list[str] = []  # B2C: AcquireTokenInteractive injects openid/offline_access
API_SCOPES: list[str] = ["https://sjsreloadingrecipes.onmicrosoft.com/7756be51-5446-43ca-9742-4693f53ad48a/ApiRead"]


@dataclass
class AuthResult:
    access_token: str
    name: str | None
    email: str | None
    expires_in: int | None
    raw: dict[str, Any]


class AuthError(Exception):
    """Raised for auth failures the UI should surface."""


class PortInUseError(AuthError):
    """The local redirect port is already bound; MSAL cannot complete the flow."""


class _FileTokenCache(msal.SerializableTokenCache):
    """`SerializableTokenCache` that hydrates from / flushes to a single file."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        if path.exists():
            try:
                self.deserialize(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def flush(self) -> None:
        if not self.has_state_changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(self.serialize(), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows can throw NotImplementedError for the bits we use; ignore.
            pass


class AuthManager:
    """Thin wrapper around `msal.PublicClientApplication` + file cache."""

    def __init__(
        self,
        cache_path: Path | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self.cache_path = cache_path or paths.token_cache_path()
        self._cache = _FileTokenCache(self.cache_path)
        if client_factory is not None:
            self._client = client_factory(token_cache=self._cache)
        else:
            self._client = msal.PublicClientApplication(
                client_id=CLIENT_ID,
                authority=B2C_AUTHORITY,
                token_cache=self._cache,
            )
        # Stash of id_token_claims from the most recent silent / interactive
        # token call. Survives across calls to identity() within this process
        # so a freshly-logged-in user's name + email is available immediately
        # — no need to wait for the MSAL cache search to pick up the new
        # entry, which can race the cache-flush on some MSAL versions.
        self._last_claims: dict[str, Any] | None = None

    # ----- introspection ------------------------------------------------------

    def accounts(self) -> list[dict[str, Any]]:
        return list(self._client.get_accounts() or [])

    def current_account(self) -> dict[str, Any] | None:
        accts = self.accounts()
        return accts[0] if accts else None

    def is_authenticated(self) -> bool:
        return self.current_account() is not None

    # ----- token acquisition --------------------------------------------------

    def acquire_token_silent(self, scopes: list[str] | None = None) -> AuthResult | None:
        acct = self.current_account()
        if acct is None:
            return None
        result = self._client.acquire_token_silent(scopes or API_SCOPES, account=acct)
        if not result or "access_token" not in result:
            return None
        self._cache.flush()
        self._remember_claims(result)
        return _to_auth_result(result)

    def login_interactive(self, scopes: list[str] | None = None) -> AuthResult:
        """Spawn a local listener + browser. Blocks until login completes."""
        try:
            result = self._client.acquire_token_interactive(
                scopes=scopes or API_SCOPES,
                prompt="select_account",
                port=REDIRECT_PORT,
            )
        except OSError as exc:
            # Port-in-use is common on dev machines that already ran the app.
            if "address already in use" in str(exc).lower() or getattr(exc, "errno", None) == 98:
                raise PortInUseError(
                    f"Local redirect port {REDIRECT_PORT} is in use. Close the app currently bound to it and try again."
                ) from exc
            raise AuthError(f"Failed to start auth listener: {exc}") from exc

        if not result or "access_token" not in result:
            err = (result or {}).get("error_description") or "no access_token returned"
            raise AuthError(f"Login failed: {err}")
        self._cache.flush()
        self._remember_claims(result)
        return _to_auth_result(result)

    def logout(self) -> None:
        for acct in self.accounts():
            self._client.remove_account(acct)
        self._cache.flush()
        self._last_claims = None

    def _remember_claims(self, msal_result: dict[str, Any]) -> None:
        """Capture id_token_claims from a fresh MSAL result.

        Used so `identity()` can return the freshly-logged-in user's name
        and email without re-querying the cache (where the new entry may
        not yet be visible to ``search``).
        """
        claims = msal_result.get("id_token_claims")
        if isinstance(claims, dict) and claims:
            self._last_claims = dict(claims)

    # ----- displayable identity -----------------------------------------------

    def identity(self) -> tuple[str | None, str | None]:
        """Best-effort ``(display_name, email)`` for the signed-in account.

        The MSAL account's ``username`` for Azure B2C is a policy-prefixed
        object id (``<guid>-b2c_1_signinup.<guid>``), not worth showing a
        user. The human-readable name + email live in the ID token claims.

        Resolution order, most-to-least reliable:
          1. ``_last_claims`` — id_token_claims captured from the most
             recent silent/interactive call. Covers the in-process
             post-login case where the cache `search` race can return
             nothing.
          2. Decode the cached ID-token JWT directly. Works on a cold
             restart (no network, survives access-token expiry).
          3. ``id_token_claims`` merged onto the account dict (newer MSAL).
          4. A silent token read (may touch the network).
        """
        acct = self.current_account()
        if acct is None:
            return None, None

        claims = self._last_claims or {}
        if not claims:
            claims = self._cached_id_claims(acct)
        if not claims:
            claims = acct.get("id_token_claims") or {}
        name = _extract_name(claims)
        email = _extract_email(claims)

        if not (name or email):
            try:
                result = self.acquire_token_silent()
            except Exception:
                result = None
            if result is not None:
                rc = result.raw.get("id_token_claims") or {}
                name = name or _extract_name(rc) or result.name
                email = email or _extract_email(rc) or result.email
        return name, email

    def _cached_id_claims(self, acct: dict[str, Any]) -> dict[str, Any]:
        """Decode the ID token stored in the MSAL cache for `acct`.

        Returns {} if nothing is cached or the cache shape isn't what we
        expect — callers fall back to other claim sources.
        """
        home_account_id = acct.get("home_account_id")
        try:
            import msal

            id_token_type = msal.TokenCache.CredentialType.ID_TOKEN
            query = {"home_account_id": home_account_id} if home_account_id else None
            # `search` is the modern API; `find` is deprecated but present on
            # older MSAL. Prefer whichever this version offers.
            if hasattr(self._cache, "search"):
                entries = list(self._cache.search(id_token_type, query=query))
            else:
                entries = self._cache.find(id_token_type, query=query)
        except Exception:
            return {}
        for entry in entries or []:
            secret = entry.get("secret") if isinstance(entry, dict) else None
            if secret:
                claims = _decode_jwt_claims(secret)
                if claims:
                    return claims
        return {}


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Base64url-decode a JWT's payload segment into a claims dict.

    Signature is NOT verified — we only read identity fields for display
    from a token MSAL already validated and cached, never for authz.
    """
    import base64
    import json

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_name(claims: dict[str, Any]) -> str | None:
    """Display name from id_token_claims, tolerating B2C claim variations."""
    name = claims.get("name")
    if name:
        return str(name)
    given = (claims.get("given_name") or "").strip()
    family = (claims.get("family_name") or "").strip()
    full = f"{given} {family}".strip()
    return full or None


def _extract_email(claims: dict[str, Any]) -> str | None:
    """Pull a usable email out of B2C id_token_claims.

    B2C puts addresses in an ``emails`` array; some flows use a scalar
    ``emails`` or a standard ``email`` claim instead.
    """
    emails = claims.get("emails")
    if isinstance(emails, list) and emails:
        return emails[0]
    if isinstance(emails, str) and emails:
        return emails
    if claims.get("email"):
        return claims["email"]
    return None


def _to_auth_result(raw: dict[str, Any]) -> AuthResult:
    claims = raw.get("id_token_claims") or {}
    return AuthResult(
        access_token=raw["access_token"],
        name=_extract_name(claims),
        email=_extract_email(claims),
        expires_in=raw.get("expires_in"),
        raw=raw,
    )
