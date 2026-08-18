"""Environment overrides for the community backend (developer settings).

Production needs no configuration: the community client points at
``https://www.reloadingrecipes.com/api`` and verifies TLS the normal way. This
module exists so a developer can point the app at a locally-running copy of
that backend — and trust its certificate — without editing source.

Settings come from environment variables. A ``.env`` file is a convenience for
setting them; **real environment variables always win**, so a shell export
overrides a checked-out ``.env``. Files are looked for in this order, and every
one that exists is applied (first value wins):

  1. ``$CASESORTER_ENV_FILE`` — explicit path,
  2. ``<data dir>/config/.env`` — per-installation,
  3. ``<app>/.env`` — next to ``bootstrap.py``, the usual developer spot.

Recognised keys (see ``.env.example``):

``CASESORTER_API_BASE``
    Base URL of the community API. Default
    ``https://www.reloadingrecipes.com/api``.
``CASESORTER_API_CA_BUNDLE``
    Path to a PEM certificate/bundle to trust *in addition to* nothing else —
    it replaces the system trust store for community calls. This is the right
    way to talk to a local HTTPS dev server: export its cert and point here.
``CASESORTER_API_INSECURE``
    ``1`` skips TLS verification entirely. **Loopback only** — it is ignored
    unless ``CASESORTER_API_BASE`` points at localhost, so it can never
    silently weaken production traffic.

``CASESORTER_DATA_DIR`` (see ``paths``) is deliberately NOT read from a ``.env``
in the data directory — that file lives inside the directory it would be
relocating. Set it as a real environment variable, or in the app-root ``.env``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .. import paths

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://www.reloadingrecipes.com/api"

ENV_API_BASE = "CASESORTER_API_BASE"
ENV_CA_BUNDLE = "CASESORTER_API_CA_BUNDLE"
ENV_INSECURE = "CASESORTER_API_INSECURE"

_TRUTHY = {"1", "true", "yes", "on"}

# A plain environment-variable name; anything else in a .env is discarded.
_VALID_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# One-shot warnings: these are evaluated per request, and nobody needs the same
# line a thousand times in a run.
_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning("%s", msg)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


# ----- .env loading -----------------------------------------------------------


def read_env_text(path: Path) -> str:
    """Decode a ``.env`` whatever the editor saved it as.

    Notepad writes UTF-8 **with a BOM** by default and PowerShell's ``>``
    redirection writes UTF-16 — both are what a Windows user is most likely to
    end up with, and both broke naive UTF-8 decoding (a BOM silently corrupts
    the first key's name; UTF-16 raises). ``utf-8-sig`` strips a BOM if present
    and is otherwise plain UTF-8; ``utf-16`` picks LE/BE off its own BOM;
    latin-1 cannot fail, so decoding always succeeds.
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # NUL characters mean we guessed the codec wrong (UTF-8 happily decodes
        # the zero bytes of UTF-16 text). Keep looking.
        if "\x00" in text:
            continue
        return text
    return ""


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Blank lines and ``#`` comments are skipped.

    Tolerates a leading ``export ``, surrounding whitespace, single or double
    quotes around the value, CRLF line endings, and a stray byte-order mark.
    Deliberately minimal — no interpolation, no multi-line values — so there is
    no dependency on python-dotenv.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        # Anything that isn't a plain environment-variable name is garbage from
        # a mis-decoded file. Dropping it beats handing os.environ a key it
        # rejects (an embedded NUL raises ValueError) at app startup.
        if not _VALID_KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def env_file_candidates() -> list[Path]:
    """Where a ``.env`` may live, most specific first."""
    candidates: list[Path] = []
    explicit = os.environ.get("CASESORTER_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(paths.config_dir() / ".env")
    candidates.append(paths.app_root() / ".env")
    return candidates


# Names a ``.env`` commonly ends up with by accident. Windows Explorer hides
# known extensions, so "Save as .env" in Notepad quietly produces ".env.txt"
# and the file looks correct in the folder listing.
_NEAR_MISS_NAMES = (".env.txt", "env.txt", "env", ".env.env")


def near_miss_env_files() -> list[Path]:
    """Files that look like a misnamed ``.env`` next to a candidate location."""
    found: list[Path] = []
    for candidate in env_file_candidates():
        for name in _NEAR_MISS_NAMES:
            sibling = candidate.parent / name
            if sibling.is_file() and sibling not in found:
                found.append(sibling)
    return found


def load_dotenv() -> list[Path]:
    """Apply every ``.env`` that exists into ``os.environ``; return those files.

    Never overwrites a variable that is already set, so the real environment
    (and, between files, the more specific one) wins. Safe to call more than
    once. A file that can't be read or parsed is reported and skipped rather
    than fatal — a developer convenience must never stop the app from starting.
    """
    applied: list[Path] = []
    seen: set[Path] = set()
    for path in env_file_candidates():
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not path.is_file():
            continue
        try:
            values = parse_env_text(read_env_text(path))
        except Exception as exc:
            _warn_once(
                f"envfile:{path}",
                f"could not read {path} ({exc.__class__.__name__}: {exc}) — ignoring it.",
            )
            continue
        for key, value in values.items():
            os.environ.setdefault(key, value)
        applied.append(path)
    return applied


# ----- resolved settings ------------------------------------------------------


def api_base() -> str:
    """Effective community API base URL, without a trailing slash."""
    return (os.environ.get(ENV_API_BASE, "").strip() or DEFAULT_API_BASE).rstrip("/")


def is_loopback(url: str) -> bool:
    """True when ``url``'s host is this machine.

    Guards the insecure-TLS switch: a developer pointing at their own box gets
    the escape hatch, and nobody can turn verification off against a remote
    host by leaving a stray variable set.
    """
    host = (urlsplit(url).hostname or "").strip().lower()
    if not host:
        return False
    return (
        host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost") or host.startswith("127.")
    )


def tls_verify() -> bool | str:
    """What to hand ``requests`` as ``verify=`` for community calls.

    A CA bundle (preferred) wins over the insecure switch, which is honored
    only for a loopback API base. Anything unusable falls back to normal
    system-trust verification — the safe direction.
    """
    bundle = os.environ.get(ENV_CA_BUNDLE, "").strip()
    if bundle:
        path = Path(bundle).expanduser()
        if path.is_file() or path.is_dir():
            return str(path)
        _warn_once(
            "ca-missing",
            f"{ENV_CA_BUNDLE}={bundle!r} does not exist — using system trust instead.",
        )
    if _flag(ENV_INSECURE):
        base = api_base()
        if not is_loopback(base):
            _warn_once(
                "insecure-remote",
                f"{ENV_INSECURE} is set but {ENV_API_BASE}={base} is not a loopback "
                "address — ignoring it and verifying TLS normally.",
            )
            return True
        _warn_once(
            "insecure-loopback",
            f"TLS verification is DISABLED for community calls to {base} ({ENV_INSECURE}=1). Development only.",
        )
        _silence_insecure_warnings()
        return False
    return True


def _silence_insecure_warnings() -> None:
    """Mute urllib3's per-request unverified-HTTPS warning.

    We print one explicit warning of our own instead; the alternative is the
    same paragraph repeated for every request of the session.
    """
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


def startup_report(applied: list[Path] | None = None) -> list[str]:
    """Lines describing where the community settings came from.

    Silent in the default case — a normal user with no ``.env`` and no
    overrides sees nothing. When anything IS configured it says which file was
    read, which keys it held, and the settings that actually took effect, so a
    file that was found but didn't apply (a misspelled key, a value overridden
    by a real environment variable) is visible instead of mysterious.
    """
    lines: list[str] = []
    applied = list(applied or [])
    for path in applied:
        try:
            keys = ", ".join(parse_env_text(read_env_text(path))) or "no settings found"
        except Exception:
            keys = "unreadable"
        lines.append(f"loaded {path} ({keys})")

    configured = bool(
        os.environ.get(ENV_API_BASE, "").strip() or os.environ.get(ENV_CA_BUNDLE, "").strip() or _flag(ENV_INSECURE)
    )
    if applied or configured:
        lines.append(describe())
    else:
        for miss in near_miss_env_files():
            lines.append(
                f"note: {miss} looks like a misnamed .env and is being ignored "
                "(Windows Explorer hides the .txt extension)."
            )
    return lines


def describe() -> str:
    """One-line summary of the effective settings, for startup logging."""
    base = api_base()
    verify = tls_verify()
    if verify is False:
        tls = "TLS verification DISABLED"
    elif isinstance(verify, str):
        tls = f"CA bundle {verify}"
    else:
        tls = "system trust"
    return f"community API: {base} ({tls})"
