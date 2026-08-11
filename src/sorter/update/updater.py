"""In-app updater — check GitHub Releases, download, stage.

Deliberately **git-free**: `git clone`/`git pull` over HTTPS and a release
tarball over HTTPS have the same trust anchor (TLS to github.com), and at
this repo's size the delta-transfer advantage is worth nothing. Dropping git
means non-developers never install a 60 MB dependency to receive a 1 MB
update.

The downloaded archive is the project's own **sdist** (`ai_case_sorter-
<tag>.tar.gz`) — the same file `uv build`/`publish.yml` already produce and
attach to every release, not a separately built artifact. hatch-vcs's build
hook stamps `src/sorter/_version.py` into every build target, so the sdist
already carries the correct version with nothing extra to keep in sync.

The flow is **stage now, apply at next launch**:

    check_for_update()  →  stage_update()  →  [restart]  →  sorter.update.apply_update

Staging never touches the app folder. Windows keeps the venv's ``.pyd``/
``.dll`` files (opencv, numpy) locked while the app is running, so replacing
files in-place is unreliable by construction; bootstrap.py applies the staged
tree before ``uv sync`` runs. That also means a staged update's own
``pyproject.toml``/``uv.lock`` are in place before the sync, so dependency
changes install on the same restart.

Everything in the ``updates/`` tree lives under the data root, which is
outside the app folder (see ``sorter/paths.py`` at the package top level).

Environment overrides:
  ``CASESORTER_UPDATE_REPO``      — ``owner/repo`` to check (default: upstream)
  ``CASESORTER_UPDATE_API_BASE``  — GitHub API base, for testing
  ``CASESORTER_UPDATE_DISABLED``  — ``1`` disables checking entirely
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import requests

from .. import __version__, paths

DEFAULT_REPO = "sjseth/AI-Case-Sorter-Py"
DEFAULT_API_BASE = "https://api.github.com"

# Settings key (SettingsRepo) for the startup check opt-out.
SETTING_CHECK_ON_STARTUP = "updates.check_on_startup"

# GitHub's unauthenticated API allows 60 requests/hour per IP. A once-per-launch
# check is nowhere near that, but keep the timeout tight so a slow or blocked
# network never delays startup.
CHECK_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 60

# An update archive must look like this repo before we let it near the app
# folder — cheap insurance against a mis-tagged or truncated release. This is
# the *old*, pre-#58 flat layout (root `main.py`); kept as its own name
# because REQUIRED_ENTRY_SETS below still has to accept it.
REQUIRED_ENTRIES = ("main.py", "sorter/__init__.py")

# #58 moved the app to a `src/` layout (root `main.py` -> `src/sorter/__main__.py`).
# Accepting *either* set here — landed ahead of the move, in #62 — is what
# made the move possible at all: the updater that validates a *new* release
# archive is whatever version is already installed on a user's machine, so
# the relaxed acceptance had to already be running before a src/-layout
# release existed. There is no way to patch an already-installed updater
# after the fact, and updates are not cumulative (a user on the old updater
# who is offered the new layout directly would reject it) — so the old
# REQUIRED_ENTRIES tuple stays here, unused by a src/-layout release itself,
# purely so an updater built from *this* tree still recognizes one it somehow
# receives. `src/sorter/__init__.py` alone is sufficient to recognize the new
# layout: this check only needs to rule out "an archive that isn't this app
# at all", not assert every file the new layout ships.
REQUIRED_ENTRY_SETS: tuple[tuple[str, ...], ...] = (
    REQUIRED_ENTRIES,
    ("src/sorter/__init__.py",),
)

# Refuse absurd archives outright rather than filling the user's disk. The
# source tree is ~1 MB; 200 MB is orders of magnitude of headroom.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024

# MAX_ARCHIVE_BYTES alone doesn't bound an archive of *empty* entries: they
# contribute nothing to the byte total, compress ~165:1, and each still costs
# a TarInfo and a stat/mkdir at extraction. The tree is ~200 files.
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ENTRY_NAME_CHARS = 1024

# Only ever fetch from GitHub (or whatever CASESORTER_UPDATE_API_BASE points
# at, for tests). requests follows redirects by default, including an
# https -> http downgrade, so the final URL is checked too.
ALLOWED_DOWNLOAD_SCHEMES = ("https",)


class UpdateError(RuntimeError):
    """Raised when a check or download fails in a way worth showing the user."""


@dataclass(frozen=True)
class UpdateInfo:
    """A release newer than what's running."""

    version: str  # normalized, no leading "v"
    tag: str  # the tag as GitHub reports it
    url: str  # tarball to download
    notes: str = ""  # release body (markdown)
    size: int | None = None
    published_at: str = ""


@dataclass(frozen=True)
class PendingUpdate:
    """A downloaded, verified update waiting for the next launch."""

    version: str
    tag: str
    path: Path
    staged_at: str = ""


# ----- version comparison -----------------------------------------------------


def _parse_version(text: str) -> tuple[tuple[int, ...], int]:
    """Parse ``v1.2.3`` / ``1.2.3-rc1`` into a sortable key.

    Returns ``(numbers, rank)`` where rank is 0 for a pre-release and 1 for a
    final release, so ``1.2.0-rc1 < 1.2.0``. Non-numeric junk is ignored
    rather than raising — a malformed upstream tag should read as "not newer",
    never as a crash on startup.
    """
    s = (text or "").strip().lstrip("vV")
    pre = 0 if any(c in s for c in "-+") else 1
    core = s.split("-", 1)[0].split("+", 1)[0]

    nums: list[int] = []
    for part in core.split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        nums.append(int(digits))
    return tuple(nums), pre


def is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is a strictly newer version than ``current``."""
    cand_nums, cand_pre = _parse_version(candidate)
    cur_nums, cur_pre = _parse_version(current)
    if not cand_nums:
        return False
    # Zero-pad so 1.2 and 1.2.0 compare equal.
    width = max(len(cand_nums), len(cur_nums))
    cand_padded = cand_nums + (0,) * (width - len(cand_nums))
    cur_padded = cur_nums + (0,) * (width - len(cur_nums))
    return (cand_padded, cand_pre) > (cur_padded, cur_pre)


def current_version() -> str:
    return __version__


def launcher_path() -> Path | None:
    """The launcher script to re-exec on "Restart Now", if the install has one.

    Running from a bare ``python main.py`` checkout there may not be one — the
    caller then tells the user to relaunch by hand rather than guessing.
    """
    import sys

    name = "start.bat" if sys.platform == "win32" else "start.sh"
    candidate = paths.app_root() / name
    return candidate if candidate.is_file() else None


# ----- checking ---------------------------------------------------------------


def update_repo() -> str:
    return os.environ.get("CASESORTER_UPDATE_REPO") or DEFAULT_REPO


def checks_disabled() -> bool:
    return os.environ.get("CASESORTER_UPDATE_DISABLED", "").strip() in ("1", "true", "yes")


def _api_base() -> str:
    return (os.environ.get("CASESORTER_UPDATE_API_BASE") or DEFAULT_API_BASE).rstrip("/")


# A release tag reaches a URL and a filename, so keep it to the shape a tag
# can actually have -- no slashes, no query, no fragment.
_TAG_RE = re.compile(r"v?[0-9A-Za-z][0-9A-Za-z._-]{0,63}")


def _strip_tag_prefix(tag: str) -> str:
    """Drop one leading lowercase ``v``, exactly as the publish workflow does.

    This has to mirror ``${TAG#v}`` in publish.yml character for character,
    because the result is used to build the asset name that ``_pick_asset``
    matches exactly — any disagreement means the client silently misses the
    real asset and falls back to the source archive.

    Two ways to get that wrong, both verified against bash:

    * ``lstrip("vV")`` strips *every* leading v, so ``vv1.2.3`` would become
      ``1.2.3`` while the workflow produces ``v1.2.3``.
    * ``${TAG#v}`` is case-sensitive, so ``V1.2.3`` stays ``V1.2.3`` upstream.
      Stripping the capital here would disagree too.

    (``check-release.yml`` rejects any ``v``-prefixed tag, so neither case
    should reach a real release — this is about the two sides not drifting,
    not about supporting those tags.)
    """
    return tag[1:] if tag.startswith("v") else tag


def _expected_asset_name(tag: str) -> str:
    """Name the sdist `uv build` produces for ``tag``.

    Hatchling writes the underscore form of "ai-case-sorter" per PEP 625
    (not PEP 503 -- that normalizes *to* hyphens, and governs index URLs
    rather than sdist filenames). This is the file publish.yml already
    attaches; there is no separate app-archive asset to build.

    The version half is only equal to the tag because publish.yml asserts
    it: hatchling emits the PEP 440 *normalized* version, so a tag like
    ``1.2.3-rc1`` would be built as ``1.2.3rc1`` and never match this. That
    assertion failing the release is the intended outcome -- without it the
    client would silently miss the asset and fall back to the source
    archive, losing the baked-in version this whole path exists to deliver.
    """
    return f"ai_case_sorter-{_strip_tag_prefix(tag)}.tar.gz"


def _pick_asset(release: dict[str, Any], tag: str) -> tuple[str, int | None]:
    """Match the sdist by its exact name, not "any .tar.gz".

    A release also carries a wheel (``.whl``), which never matches this. But
    "the first .tar.gz asset" was never a safe rule on its own: the day
    anyone attached an unrelated ``.tar.gz`` to a release, in upload order
    ahead of the real one, it would have silently become the tree unpacked
    over the app folder. Falls back to the tag source archive if the named
    asset isn't published, same as before -- a release with no assets still
    updates correctly, just without a baked-in version (see apply_update's
    ``_stamp_version``).
    """
    expected = _expected_asset_name(tag)
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        url = asset.get("browser_download_url")
        if name == expected and url:
            size = asset.get("size")
            return str(url), int(size) if isinstance(size, int) else None
    repo = update_repo()
    return f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz", None


def check_for_update(
    *,
    current: str | None = None,
    session: requests.Session | None = None,
    timeout: int = CHECK_TIMEOUT,
) -> UpdateInfo | None:
    """Return the latest release if it's newer than what's running, else None.

    ``/releases/latest`` already excludes drafts and pre-releases, so users on
    the stable channel never see a release candidate.
    """
    if checks_disabled():
        return None

    cur = current or current_version()
    url = f"{_api_base()}/repos/{update_repo()}/releases/latest"
    get = (session or requests).get
    try:
        resp = get(
            url,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"CaseSorter/{cur}",
            },
        )
    except requests.RequestException as exc:
        raise UpdateError(f"Could not reach the update server: {exc}") from exc

    if resp.status_code == 404:
        # No releases published yet — not an error worth surfacing.
        return None
    if resp.status_code != 200:
        raise UpdateError(f"Update check failed (HTTP {resp.status_code}).")

    try:
        release = resp.json()
    except ValueError as exc:
        raise UpdateError("Update server returned a malformed response.") from exc

    tag = str(release.get("tag_name") or "").strip()
    if not tag:
        return None
    # The tag is interpolated into the fallback archive URL, and _parse_version
    # is lenient enough (it stops at the first non-digit) that something like
    # "1.0.0/../../someone-else/repo/archive/refs/tags/v1.tar.gz" would parse
    # as newer and then resolve to a different repository.
    if not _TAG_RE.fullmatch(tag):
        raise UpdateError(f"Update server returned an implausible tag: {tag!r}")
    version = _strip_tag_prefix(tag)
    if not is_newer(version, cur):
        return None

    asset_url, size = _pick_asset(release, tag)
    return UpdateInfo(
        version=version,
        tag=tag,
        url=asset_url,
        notes=str(release.get("body") or "").strip(),
        size=size,
        published_at=str(release.get("published_at") or ""),
    )


def list_releases(
    *,
    include_prereleases: bool = False,
    session: requests.Session | None = None,
    timeout: int = CHECK_TIMEOUT,
) -> list[UpdateInfo]:
    """Return published releases from newest to oldest, for the version picker.

    Unlike ``check_for_update``, this doesn't compare against the running
    version or stop at the first hit — the picker needs the whole list,
    including releases older than what's installed. Everything else mirrors
    ``/releases/latest``'s documented semantics so the two never disagree
    about what counts as a real, installable release:

    * Drafts are always excluded. The plain (list) endpoint can return them
      for a caller with push access, unlike ``/releases/latest`` which never
      does; the unauthenticated case here never sees one either, but the
      filter costs nothing and keeps the contract explicit.
    * Pre-releases are excluded unless ``include_prereleases=True``.
    * Every tag must pass ``_TAG_RE`` before it's trusted — see the comment
      on that pattern in ``check_for_update``: a malformed tag reaches the
      fallback archive URL and could redirect it to a different repo. A
      single-release check has no choice but to raise on that; a list does —
      skipping the one bad entry rather than hiding every legitimate release
      behind it.
    * Asset selection goes through ``_pick_asset``, same as the single-release
      path, so a picked version resolves to the same archive either endpoint
      would have handed back for it.
    """
    if checks_disabled():
        return []

    url = f"{_api_base()}/repos/{update_repo()}/releases"
    get = (session or requests).get
    try:
        resp = get(
            url,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"CaseSorter/{current_version()}",
            },
        )
    except requests.RequestException as exc:
        raise UpdateError(f"Could not reach the update server: {exc}") from exc

    if resp.status_code == 404:
        # No releases published yet — not an error worth surfacing.
        return []
    if resp.status_code != 200:
        raise UpdateError(f"Update check failed (HTTP {resp.status_code}).")

    try:
        releases = resp.json()
    except ValueError as exc:
        raise UpdateError("Update server returned a malformed response.") from exc

    if not isinstance(releases, list):
        raise UpdateError("Update server returned a malformed response.")

    out: list[UpdateInfo] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not include_prereleases:
            continue
        tag = str(release.get("tag_name") or "").strip()
        if not tag or not _TAG_RE.fullmatch(tag):
            continue
        version = _strip_tag_prefix(tag)
        asset_url, size = _pick_asset(release, tag)
        out.append(
            UpdateInfo(
                version=version,
                tag=tag,
                url=asset_url,
                notes=str(release.get("body") or "").strip(),
                size=size,
                published_at=str(release.get("published_at") or ""),
            )
        )
    return out


# ----- staging ----------------------------------------------------------------


def _pending_meta_path() -> Path:
    # Sibling of the payload, never inside it: anything in `pending/` gets
    # copied into the app folder verbatim.
    return paths.updates_dir() / "pending.json"


def pending_dir() -> Path:
    return paths.updates_dir() / "pending"


def pending_update() -> PendingUpdate | None:
    """The staged update waiting for the next launch, if any."""
    meta_path = _pending_meta_path()
    payload = pending_dir()
    if not meta_path.is_file() or not payload.is_dir():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = str(meta.get("version") or "")
    if not version:
        return None
    return PendingUpdate(
        version=version,
        tag=str(meta.get("tag") or version),
        path=payload,
        staged_at=str(meta.get("staged_at") or ""),
    )


def clear_pending() -> None:
    """Discard any staged update. Safe to call when there isn't one."""
    shutil.rmtree(pending_dir(), ignore_errors=True)
    try:
        _pending_meta_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _safe_members(tf: tarfile.TarFile) -> list[tuple[tarfile.TarInfo, PurePosixPath]]:
    """Validate archive entries and strip the sdist's top-level folder.

    Rejects the same shapes ``model_io`` rejects — ``..`` traversal and
    absolute paths — because this archive is written straight into the app
    folder. A tarball can additionally contain symlinks, hardlinks, and
    device/fifo entries, none of which a ZIP can express; unlike the
    traversal checks below, a symlink pointing outside the extraction
    directory is a real, well-known tarfile attack class, so anything that
    isn't a plain regular file is rejected outright rather than skipped —
    silently ignoring a symlink could still let it shadow a real file's
    resolution elsewhere in the tree. Both hatch's sdist and GitHub's
    tag-archive fallback nest everything under one top-level directory
    (``<name>-<version>/`` / ``<repo>-<tag>/``); that wrapper is stripped so
    the tree lines up with the app folder.

    Name validation runs *before* directory entries are skipped, so a
    hostile directory name is reported rather than silently discarded. The
    per-component ``:`` and ``\\`` rejections are load-bearing on Windows,
    not zip-era vestiges: ``PurePosixPath`` treats ``C:`` as an ordinary
    component, but rebuilding the path with ``Path()`` there re-anchors on
    the drive, so ``pkg/D:/evil.py`` would escape the staging directory
    entirely. ``stage_update`` re-checks containment against the resolved
    output path as the actual backstop; these keep the error message useful.

    Members are streamed rather than read via ``getmembers()``: that would
    walk — and so decompress — the entire archive before the first size or
    count check could fire.
    """
    raw: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    total = 0
    count = 0
    for entry in tf:
        # Not rewritten to "/" the way the ZIP version did: tar names are
        # slash-separated by spec, so a backslash here is either a legal
        # POSIX filename (which rewriting would mangle into a directory) or
        # a Windows path someone is hoping gets re-anchored. Rejected below
        # either way.
        name = entry.name

        count += 1
        if count > MAX_ARCHIVE_ENTRIES:
            raise UpdateError("Update archive has implausibly many entries; refusing to extract.")
        if len(entry.name) > MAX_ENTRY_NAME_CHARS:
            raise UpdateError("Update archive contains an implausibly long path; refusing to extract.")
        if name.startswith("/"):
            raise UpdateError(f"Update archive contains an absolute path: {entry.name}")
        rel = PurePosixPath(name.lstrip("/"))
        if any(part == ".." for part in rel.parts):
            raise UpdateError(f"Update archive contains a traversal path: {entry.name}")
        if any(":" in part or "\\" in part for part in rel.parts):
            raise UpdateError(f"Update archive contains a drive-qualified path: {entry.name}")

        # Trailing-slash names are directories too: tarfile only rewrites
        # those to DIRTYPE for the legacy AREGTYPE, so a REGTYPE member
        # named "pkg/sub/" would otherwise be written out as a file.
        if entry.isdir() or name.endswith("/"):
            continue
        if not entry.isreg():
            raise UpdateError(f"Update archive contains a non-regular-file entry: {entry.name}")

        total += entry.size
        if total > MAX_ARCHIVE_BYTES:
            raise UpdateError("Update archive is implausibly large; refusing to extract.")
        raw.append((entry, rel))

    if not raw:
        raise UpdateError("Update archive is empty.")

    # Strip a single common top-level directory, if every entry shares one.
    tops = {rel.parts[0] for _, rel in raw if len(rel.parts) > 1}
    singles = [rel for _, rel in raw if len(rel.parts) == 1]
    if len(tops) == 1 and not singles:
        return [(entry, PurePosixPath(*rel.parts[1:])) for entry, rel in raw]
    return raw


def _check_download_url(url: str) -> None:
    """Refuse anything but plaintext-free transport to the update host.

    The URL comes from the release JSON, so it is only as trustworthy as
    that response. A ``http://`` ``browser_download_url`` -- or a redirect
    chain that downgrades to one -- would hand the archive to anyone on the
    path, and the archive is executed at next launch.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise UpdateError(f"Refusing to download over {parsed.scheme or 'an unknown scheme'}.")


def _download(
    url: str,
    dest: Path,
    progress: Callable[[int, int | None], None] | None,
    session: requests.Session | None,
    timeout: int,
) -> Path:
    """Stream ``url`` to ``dest`` with an atomic ``.tmp`` → ``os.replace``."""
    _check_download_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    get = (session or requests).get
    try:
        with get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            # requests follows redirects silently, including https -> http.
            # The URL that actually served the bytes is the one that matters.
            _check_download_url(resp.url)
            total: int | None = None
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                try:
                    total = int(cl)
                except ValueError:
                    total = None
            done = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    done += len(chunk)
                    if done > MAX_ARCHIVE_BYTES:
                        raise UpdateError("Update download exceeded the size limit.")
                    fh.write(chunk)
                    if progress is not None:
                        progress(done, total)
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Download failed: {exc}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    os.replace(tmp, dest)
    return dest


def stage_update(
    info: UpdateInfo,
    *,
    progress: Callable[[int, int | None], None] | None = None,
    session: requests.Session | None = None,
    timeout: int = DOWNLOAD_TIMEOUT,
) -> PendingUpdate:
    """Download and verify ``info``, leaving it staged for the next launch.

    The app folder is not touched. On any failure the previous staged update
    (if any) is left intact and the partial work is cleaned up.
    """
    updates = paths.updates_dir()
    updates.mkdir(parents=True, exist_ok=True)
    archive = updates / "download.tar.gz"
    staging = updates / "staging"

    shutil.rmtree(staging, ignore_errors=True)
    try:
        _download(info.url, archive, progress, session, timeout)

        # No is_tarfile() pre-check: it accepts plain/bz2/xz tars that
        # mode="r:gz" then rejects, so the friendly message below was
        # bypassed by a bare ReadError. Catching TarError covers that, a
        # truncated gzip, and corruption found mid-read -- and parses the
        # file once instead of twice. "r:gz" stays exact rather than "r:*",
        # which would accept bz2/xz bombs.
        try:
            with tarfile.open(archive, mode="r:gz") as tf:
                members = _safe_members(tf)
                names = {str(rel) for _, rel in members}
                # Accepted if it satisfies *any one* required-entry set (see
                # REQUIRED_ENTRY_SETS) — not all of them. The reported
                # "missing" entries are always against the flat layout since
                # that's still what ships today; a src/-layout archive that
                # fails will report against src/sorter/__init__.py instead so
                # the message still points at what's actually absent.
                matched = next(
                    (required for required in REQUIRED_ENTRY_SETS if all(e in names for e in required)),
                    None,
                )
                if matched is None:
                    missing = [e for e in REQUIRED_ENTRIES if e not in names]
                    raise UpdateError(f"Update archive does not look like the app (missing {', '.join(missing)}).")
                staging.mkdir(parents=True, exist_ok=True)
                base = staging.resolve()
                for entry, rel in members:
                    # The backstop for _safe_members' name checks: compare the
                    # *resolved* destination against the staging root, which is
                    # what tarfile's own data filter does and what catches any
                    # platform-specific join surprise the string checks miss.
                    out = (base / Path(*rel.parts)).resolve()
                    # Strictly below the root: every entry here is a file, so
                    # base itself resolving as the target is wrong too.
                    if base not in out.parents:
                        raise UpdateError(f"Update archive entry escapes the staging directory: {entry.name}")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    # extractfile() returns None only for non-regular members,
                    # which _safe_members already rejected -- this narrows the
                    # IO[bytes] | None type, it is not a reachable branch.
                    src = tf.extractfile(entry)
                    if src is None:
                        raise UpdateError(f"Could not read {entry.name} from the update archive.")
                    with src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except tarfile.TarError as exc:
            raise UpdateError("Downloaded file is not a valid tar.gz archive.") from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
        raise

    archive.unlink(missing_ok=True)

    # Swap staging into place last, so `pending/` only ever exists complete.
    clear_pending()
    os.replace(staging, pending_dir())

    staged_at = datetime.now(UTC).isoformat(timespec="seconds")
    _pending_meta_path().write_text(
        json.dumps(
            {
                "version": info.version,
                "tag": info.tag,
                "staged_at": staged_at,
                "from_version": current_version(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return PendingUpdate(version=info.version, tag=info.tag, path=pending_dir(), staged_at=staged_at)
