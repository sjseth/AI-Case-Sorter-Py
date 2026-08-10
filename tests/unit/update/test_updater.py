"""Tests for the update check + staging half of the updater."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
import requests

from sorter import updater
from sorter.updater import UpdateError, UpdateInfo


@pytest.fixture(autouse=True)
def _isolated_data_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CASESORTER_UPDATE_DISABLED", raising=False)
    monkeypatch.delenv("CASESORTER_UPDATE_REPO", raising=False)
    monkeypatch.delenv("CASESORTER_UPDATE_API_BASE", raising=False)
    return tmp_path


# ----- version comparison -----------------------------------------------------


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("0.2.0", "0.1.0", True),
        ("v0.2.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("1.0.0", "0.9.9", True),
        ("0.10.0", "0.9.0", True),  # numeric, not lexicographic
        ("0.2", "0.2.0", False),  # zero-padded to equal
        ("0.2.1", "0.2", True),
        ("0.2.0-rc1", "0.1.0", True),  # prerelease still newer than 0.1.0
        ("0.2.0-rc1", "0.2.0", False),  # ...but older than its own release
        ("0.2.0", "0.2.0-rc1", True),
        ("garbage", "0.1.0", False),  # malformed tag reads as "not newer"
        ("", "0.1.0", False),
    ],
)
def test_is_newer(candidate: str, current: str, expected: bool) -> None:
    assert updater.is_newer(candidate, current) is expected


# ----- checking ---------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _release(tag: str = "v0.9.0", assets=None, body: str = "notes") -> dict:
    return {"tag_name": tag, "body": body, "assets": assets or [], "published_at": "2026-01-01T00:00:00Z"}


def test_check_returns_info_when_newer(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _release()))
    info = updater.check_for_update(current="0.1.0")
    assert info is not None
    assert info.version == "0.9.0"
    assert info.tag == "v0.9.0"
    assert info.notes == "notes"
    # No sdist asset published → fall back to the tag source archive.
    assert info.url.endswith("/archive/refs/tags/v0.9.0.tar.gz")


def test_check_prefers_the_named_sdist_asset(monkeypatch) -> None:
    assets = [
        {"name": "checksums.txt", "browser_download_url": "https://x/c.txt"},
        {"name": "ai_case_sorter-0.9.0.tar.gz", "browser_download_url": "https://x/app.tar.gz", "size": 4242},
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _release(assets=assets)))
    info = updater.check_for_update(current="0.1.0")
    assert info is not None
    assert info.url == "https://x/app.tar.gz"
    assert info.size == 4242


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3"),
        ("V1.2.3", "V1.2.3"),  # ${TAG#v} is case-sensitive; don't strip the capital
        ("vv1.2.3", "v1.2.3"),  # ...and it strips one character, not every leading v
        ("0.1.0rc1", "0.1.0rc1"),
    ],
)
def test_tag_prefix_stripping_matches_the_sdist_filename(tag: str, expected: str) -> None:
    """_pick_asset matches the sdist filename *exactly*, so this has to agree
    with what hatchling actually writes. Any disagreement means the client
    silently misses the real asset and falls back to the source archive,
    which carries no _version.py and so re-prompts forever (see
    test_apply_update.py).

    Note what this does *not* prove: hatchling names the file from the PEP
    440 *normalized* version, and _strip_tag_prefix is not a normalizer --
    see test_expected_asset_name_does_not_normalize below. publish.yml's
    "Assert the sdist is named and stamped as the updater expects" step is
    what actually holds the two sides together.

    Both plausible shortcuts have already been wrong here once: lstrip("vV")
    strips every leading v, and stripping a capital V disagrees with bash.
    """
    assert updater._strip_tag_prefix(tag) == expected
    assert updater._expected_asset_name(tag) == f"ai_case_sorter-{expected}.tar.gz"


@pytest.mark.parametrize(
    ("tag", "hatchling_builds"),
    [
        ("1.2.3-rc1", "ai_case_sorter-1.2.3rc1.tar.gz"),
        ("1.2.3.RC1", "ai_case_sorter-1.2.3rc1.tar.gz"),
        ("1.0", "ai_case_sorter-1.0.tar.gz"),
    ],
)
def test_expected_asset_name_does_not_normalize(tag: str, hatchling_builds: str) -> None:
    """Pins the known gap rather than pretending it isn't there.

    hatchling normalizes the version per PEP 440 when naming the sdist;
    _expected_asset_name only strips a leading "v". For a non-canonical tag
    the two disagree, the client misses the asset, and it degrades to the
    source archive *silently* -- which is the whole failure this asserts
    against in CI instead of at the user.

    "1.0" is here as the control: it round-trips, so the mismatch is about
    normalization specifically, not about every unusual tag.
    """
    computed = updater._expected_asset_name(tag)
    if tag == "1.0":
        assert computed == hatchling_builds
    else:
        assert computed != hatchling_builds


def test_check_ignores_a_stray_tarball_asset(monkeypatch) -> None:
    """A wheel/sdist-publishing release could carry other .tar.gz-ish files
    (e.g. an unrelated attachment). Only the exact expected sdist name should
    ever be picked -- "first asset ending in .tar.gz" would let any of these
    silently become the tree unpacked over the app folder."""
    assets = [
        {"name": "some-unrelated-thing.tar.gz", "browser_download_url": "https://x/bogus.tar.gz", "size": 999},
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _release(assets=assets)))
    info = updater.check_for_update(current="0.1.0")
    assert info is not None
    # Falls through to the tag source archive, not the stray tarball.
    assert info.url.endswith("/archive/refs/tags/v0.9.0.tar.gz")


def test_check_ignores_the_wheel(monkeypatch) -> None:
    """Confirms the real-world case this hardening exists for: a release
    with a wheel (attached by the publish workflow) but no sdist falls back
    correctly, exactly as a release with no assets at all always has."""
    assets = [
        {"name": "ai_case_sorter-0.9.0-py3-none-any.whl", "browser_download_url": "https://x/w.whl"},
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _release(assets=assets)))
    info = updater.check_for_update(current="0.1.0")
    assert info is not None
    assert info.url.endswith("/archive/refs/tags/v0.9.0.tar.gz")


@pytest.mark.parametrize(
    "tag",
    [
        "1.0.0/../../someone-else/repo/archive/refs/tags/v1.tar.gz",
        "1.0.0?x=y",
        "1.0.0#frag",
        "1.0.0 with spaces",
    ],
)
def test_check_rejects_an_implausible_tag(monkeypatch, tag: str) -> None:
    """The tag is interpolated into the fallback archive URL, and
    _parse_version stops at the first non-digit -- so a tag beginning with a
    plausible version but continuing into a path would read as "newer" and
    then resolve to a different repository's archive."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _release(tag=tag)))
    with pytest.raises(UpdateError, match="implausible tag"):
        updater.check_for_update(current="0.1.0")


def test_download_refuses_a_non_https_url(tmp_path: Path) -> None:
    """browser_download_url comes from the release JSON, so it is only as
    trustworthy as that response -- and the archive is executed at next
    launch."""
    with pytest.raises(UpdateError, match="Refusing to download over http"):
        updater._download("http://example.invalid/app.tar.gz", tmp_path / "a.tar.gz", None, None, 5)


def test_check_returns_none_when_current(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _release(tag="v0.1.0")))
    assert updater.check_for_update(current="0.1.0") is None


def test_check_returns_none_when_no_releases(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(404))
    assert updater.check_for_update(current="0.1.0") is None


def test_check_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_UPDATE_DISABLED", "1")

    def _boom(*a, **k):
        raise AssertionError("network must not be touched when disabled")

    monkeypatch.setattr(requests, "get", _boom)
    assert updater.check_for_update(current="0.1.0") is None


def test_check_raises_on_server_error(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(500))
    with pytest.raises(UpdateError):
        updater.check_for_update(current="0.1.0")


def test_check_wraps_network_failure(monkeypatch) -> None:
    def _boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(UpdateError, match="Could not reach"):
        updater.check_for_update(current="0.1.0")


# ----- listing releases for the picker -----------------------------------------


def test_list_releases_returns_newest_first_excluding_drafts_and_prereleases(monkeypatch) -> None:
    releases = [
        {
            "tag_name": "v0.3.0",
            "body": "three",
            "assets": [],
            "published_at": "2026-03-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v0.3.0-rc1",
            "body": "rc",
            "assets": [],
            "published_at": "2026-02-15T00:00:00Z",
            "draft": False,
            "prerelease": True,
        },
        {
            "tag_name": "v0.2.0",
            "body": "draft, must never appear",
            "assets": [],
            "published_at": "2026-02-01T00:00:00Z",
            "draft": True,
            "prerelease": False,
        },
        {
            "tag_name": "v0.1.0",
            "body": "one",
            "assets": [],
            "published_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, releases))
    result = updater.list_releases()
    # Drafts are excluded unconditionally; prereleases are excluded by
    # default -- same rule /releases/latest applies for check_for_update.
    assert [r.tag for r in result] == ["v0.3.0", "v0.1.0"]


def test_list_releases_can_opt_into_prereleases(monkeypatch) -> None:
    releases = [
        {
            "tag_name": "v0.3.0",
            "body": "three",
            "assets": [],
            "published_at": "2026-03-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v0.3.0-rc1",
            "body": "rc",
            "assets": [],
            "published_at": "2026-02-15T00:00:00Z",
            "draft": False,
            "prerelease": True,
        },
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, releases))
    result = updater.list_releases(include_prereleases=True)
    assert [r.tag for r in result] == ["v0.3.0", "v0.3.0-rc1"]


def test_list_releases_skips_an_implausible_tag_rather_than_failing_the_whole_list(monkeypatch) -> None:
    """Unlike check_for_update (a single release, so a bad tag has to raise),
    a malformed entry here should be dropped rather than hide every
    legitimate release behind it. The tag still has to pass _TAG_RE for the
    same reason as check_for_update: it's interpolated into the fallback
    archive URL."""
    releases = [
        {
            "tag_name": "1.0.0/../../someone-else/repo/archive/refs/tags/v1.tar.gz",
            "body": "x",
            "assets": [],
            "published_at": "",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v0.1.0",
            "body": "one",
            "assets": [],
            "published_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, releases))
    result = updater.list_releases()
    assert [r.tag for r in result] == ["v0.1.0"]


def test_list_releases_goes_through_pick_asset(monkeypatch) -> None:
    assets = [
        {"name": "checksums.txt", "browser_download_url": "https://x/c.txt"},
        {"name": "ai_case_sorter-0.1.0.tar.gz", "browser_download_url": "https://x/app.tar.gz", "size": 111},
    ]
    releases = [
        {
            "tag_name": "v0.1.0",
            "body": "one",
            "assets": assets,
            "published_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
        },
    ]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, releases))
    result = updater.list_releases()
    assert result[0].url == "https://x/app.tar.gz"
    assert result[0].size == 111


def test_list_releases_empty_when_none_published(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(404))
    assert updater.list_releases() == []


def test_list_releases_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_UPDATE_DISABLED", "1")

    def _boom(*a, **k):
        raise AssertionError("network must not be touched when disabled")

    monkeypatch.setattr(requests, "get", _boom)
    assert updater.list_releases() == []


def test_list_releases_wraps_network_failure(monkeypatch) -> None:
    def _boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(UpdateError, match="Could not reach"):
        updater.list_releases()


def test_list_releases_raises_on_server_error(monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(500))
    with pytest.raises(UpdateError):
        updater.list_releases()


# ----- staging ----------------------------------------------------------------


def _tar_bytes(
    entries: dict[str, str],
    *,
    special: tuple[str, bytes, str] | None = None,
) -> bytes:
    """Build a .tar.gz. ``special`` adds one (name, typeflag, linkname) entry.

    The typeflag is passed through raw so a test can add the entry shapes a
    ZIP cannot express -- symlink, hardlink, fifo, device -- which is the
    class of thing _safe_members exists to reject.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if special is not None:
            name, typeflag, linkname = special
            info = tarfile.TarInfo(name=name)
            info.type = typeflag
            info.linkname = linkname
            tf.addfile(info)
    return buf.getvalue()


def _good_archive(prefix: str = "ai_case_sorter-0.9.0/") -> bytes:
    return _tar_bytes(
        {
            f"{prefix}main.py": "print('new')\n",
            f"{prefix}sorter/__init__.py": '__version__ = "0.9.0"\n',
            f"{prefix}sorter/updater.py": "# new\n",
            f"{prefix}PKG-INFO": "Metadata-Version: 2.1\n",
        }
    )


class _StreamResp:
    def __init__(self, payload: bytes, url: str = "https://x/app.tar.gz") -> None:
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload))}
        # requests exposes the *final* URL after redirects; _download
        # re-checks it, since a redirect can downgrade https -> http.
        self.url = url

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 1):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, payload: bytes) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _StreamResp(payload))


def _info() -> UpdateInfo:
    return UpdateInfo(version="0.9.0", tag="v0.9.0", url="https://x/app.tar.gz")


def test_stage_extracts_and_strips_the_sdist_wrapper(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    pending = updater.stage_update(_info())

    assert pending.version == "0.9.0"
    assert (pending.path / "main.py").read_text(encoding="utf-8") == "print('new')\n"
    assert (pending.path / "sorter" / "__init__.py").is_file()
    # PKG-INFO is a harmless byproduct of the sdist format; it should still
    # land in the staged tree like any other file, not be specially dropped.
    assert (pending.path / "PKG-INFO").is_file()
    # The wrapper directory must be gone, not nested.
    assert not (pending.path / "ai_case_sorter-0.9.0").exists()


def test_stage_records_pending_metadata(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    updater.stage_update(_info())

    found = updater.pending_update()
    assert found is not None
    assert found.version == "0.9.0"
    assert found.tag == "v0.9.0"
    assert found.staged_at

    meta = json.loads((updater.paths.updates_dir() / "pending.json").read_text())
    assert meta["from_version"] == updater.current_version()


def test_stage_reports_progress(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    seen: list[tuple[int, int | None]] = []
    updater.stage_update(_info(), progress=lambda d, t: seen.append((d, t)))
    assert seen
    assert seen[-1][0] == seen[-1][1]  # finished at 100%


def test_stage_rejects_traversal_entries(monkeypatch) -> None:
    _serve(
        monkeypatch,
        _tar_bytes(
            {
                "pkg/main.py": "x",
                "pkg/sorter/__init__.py": "x",
                "pkg/../../evil.py": "pwned",
            }
        ),
    )
    with pytest.raises(UpdateError, match="traversal"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


@pytest.mark.parametrize(
    ("label", "typeflag", "linkname"),
    [
        ("symlink", tarfile.SYMTYPE, "/etc/passwd"),
        ("hardlink", tarfile.LNKTYPE, "pkg/main.py"),
        ("fifo", tarfile.FIFOTYPE, ""),
        ("chardev", tarfile.CHRTYPE, ""),
        ("blockdev", tarfile.BLKTYPE, ""),
    ],
)
def test_stage_rejects_non_regular_entries(monkeypatch, label: str, typeflag: bytes, linkname: str) -> None:
    """Unlike a ZIP, a tarball can carry all of these -- a real extraction
    attack class if one is allowed to point outside the staged tree. Every
    non-regular-file entry is rejected outright, not just symlinks that look
    suspicious, since silently skipping one could still shadow a real file.

    Parametrized over the whole set rather than symlinks alone: they share a
    code path today, but "the docstring claims hardlinks and devices too" is
    only true if something checks.
    """
    _serve(
        monkeypatch,
        _tar_bytes(
            {"pkg/main.py": "x", "pkg/sorter/__init__.py": "x"},
            special=(f"pkg/evil-{label}", typeflag, linkname),
        ),
    )
    with pytest.raises(UpdateError, match="non-regular-file"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_rejects_an_absolute_path(monkeypatch) -> None:
    _serve(
        monkeypatch,
        _tar_bytes({"pkg/main.py": "x", "pkg/sorter/__init__.py": "x", "/etc/evil.py": "pwned"}),
    )
    with pytest.raises(UpdateError, match="absolute path"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


@pytest.mark.parametrize("name", ["pkg/D:/evil.py", "pkg/C:evil.py", "pkg/sub\\evil.py"])
def test_stage_rejects_drive_qualified_paths(monkeypatch, name: str) -> None:
    """A drive letter in a *later* component used to slip past the
    absolute-path check, which only looked at position 1 of the whole name.

    PurePosixPath treats "C:" as an ordinary component, but rebuilding with
    Path() on Windows re-anchors on the drive, so "pkg/D:/evil.py" resolved
    to "D:evil.py" -- drive-relative, and entirely outside the staging
    directory. On a machine whose app folder is on that drive it lands in
    the live app folder, defeating the stage-then-apply-at-next-launch
    design that exists so an update can be rolled back.

    Rejected here on every platform: the check is about what the archive
    claims, not about what this particular OS would do with it.
    """
    _serve(
        monkeypatch,
        _tar_bytes({"pkg/main.py": "x", "pkg/sorter/__init__.py": "x", name: "pwned"}),
    )
    with pytest.raises(UpdateError, match="drive-qualified"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_rejects_too_many_entries(monkeypatch) -> None:
    """MAX_ARCHIVE_BYTES counts declared payload bytes, so it does not bound
    an archive of *empty* entries: they cost nothing against it, compress
    ~165:1, and each still costs a TarInfo and a mkdir at extraction."""
    monkeypatch.setattr(updater, "MAX_ARCHIVE_ENTRIES", 10)
    _serve(monkeypatch, _tar_bytes({f"pkg/f{i}.py": "" for i in range(50)}))
    with pytest.raises(UpdateError, match="implausibly many entries"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_rejects_a_gzip_bomb(monkeypatch) -> None:
    """The *uncompressed* guard, not the download cap.

    A megabyte of zeros compresses to about a kilobyte, so the download cap
    (which sees compressed bytes) passes happily and only the per-member
    total catches it. Sizing the payload so the compressed stream is well
    under the limit and the expanded one is well over is what makes this
    test the bomb case rather than a second test of the download cap.
    """
    monkeypatch.setattr(updater, "MAX_ARCHIVE_BYTES", 100_000)
    payload = _tar_bytes(
        {
            "pkg/main.py": "x",
            "pkg/sorter/__init__.py": "x",
            "pkg/bomb.bin": "0" * (2 * 1024 * 1024),
        }
    )
    assert len(payload) < 100_000, "payload must pass the download cap to reach the member guard"
    _serve(monkeypatch, payload)
    with pytest.raises(UpdateError, match="implausibly large"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_strips_the_github_source_archive_wrapper(monkeypatch) -> None:
    """_pick_asset still falls back to GitHub's tag archive, which nests
    under <repo>-<tag>/ rather than the sdist's <name>-<version>/. Both
    shapes have to strip, and only the sdist one was still covered."""
    _serve(monkeypatch, _good_archive(prefix="AI-Case-Sorter-Py-v0.9.0/"))
    pending = updater.stage_update(_info())
    assert (pending.path / "main.py").is_file()
    assert (pending.path / "sorter" / "__init__.py").is_file()


def test_stage_rejects_a_redirect_that_downgrades_to_http(monkeypatch) -> None:
    """requests follows redirects silently, so checking only the URL we asked
    for would miss an https -> http downgrade on the way to the bytes."""
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _StreamResp(_good_archive(), url="http://evil.invalid/app.tar.gz"),
    )
    with pytest.raises(UpdateError, match="Refusing to download over http"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_rejects_a_plain_tar(monkeypatch) -> None:
    """is_tarfile() accepted uncompressed/bz2/xz tars that mode="r:gz" then
    refused, so the friendly message was bypassed by a bare ReadError."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:  # no compression
        data = b"x"
        info = tarfile.TarInfo(name="pkg/main.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    _serve(monkeypatch, buf.getvalue())
    with pytest.raises(UpdateError, match="not a valid tar.gz"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_rejects_an_archive_that_is_not_the_app(monkeypatch) -> None:
    _serve(monkeypatch, _tar_bytes({"wrong/readme.txt": "hello"}))
    with pytest.raises(UpdateError, match="does not look like the app"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_required_entry_sets_cover_both_layouts() -> None:
    """Pins the two shapes stage_update has to accept -- see #58: the src/
    layout has to be accepted by every already-installed updater *before*
    main.py actually moves, or the move permanently breaks in-app updates
    for every install that predates it."""
    assert updater.REQUIRED_ENTRIES in updater.REQUIRED_ENTRY_SETS
    assert ("src/sorter/__init__.py",) in updater.REQUIRED_ENTRY_SETS


def test_stage_accepts_the_flat_layout(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    pending = updater.stage_update(_info())
    assert (pending.path / "main.py").is_file()
    assert (pending.path / "sorter" / "__init__.py").is_file()


def test_stage_accepts_the_src_layout(monkeypatch) -> None:
    """A future release built from the src/ layout (#58) has no root main.py
    or sorter/__init__.py at all -- REQUIRED_ENTRY_SETS has to accept this
    shape too, not just the current one."""
    _serve(
        monkeypatch,
        _tar_bytes(
            {
                "ai_case_sorter-0.9.0/src/sorter/__init__.py": '__version__ = "0.9.0"\n',
                "ai_case_sorter-0.9.0/src/sorter/__main__.py": "# entry point\n",
                "ai_case_sorter-0.9.0/pyproject.toml": "[project]\n",
            }
        ),
    )
    pending = updater.stage_update(_info())
    assert (pending.path / "src" / "sorter" / "__init__.py").is_file()
    assert not (pending.path / "main.py").exists()


def test_stage_still_rejects_an_archive_matching_neither_layout(monkeypatch) -> None:
    _serve(monkeypatch, _tar_bytes({"pkg/readme.txt": "hello", "pkg/lib/thing.py": "x"}))
    with pytest.raises(UpdateError, match="does not look like the app"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_stage_rejects_a_non_tarball(monkeypatch) -> None:
    _serve(monkeypatch, b"definitely not a tarball")
    with pytest.raises(UpdateError, match="not a valid tar.gz"):
        updater.stage_update(_info())
    assert updater.pending_update() is None


def test_failed_stage_leaves_the_previous_pending_update_intact(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    updater.stage_update(_info())
    assert updater.pending_update() is not None

    _serve(monkeypatch, b"corrupt")
    with pytest.raises(UpdateError):
        updater.stage_update(UpdateInfo(version="1.0.0", tag="v1.0.0", url="https://x/b.tar.gz"))

    still = updater.pending_update()
    assert still is not None and still.version == "0.9.0"


def test_stage_leaves_no_scratch_files_behind(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    updater.stage_update(_info())
    updates = updater.paths.updates_dir()
    assert not (updates / "download.tar.gz").exists()
    assert not (updates / "staging").exists()


def test_clear_pending(monkeypatch) -> None:
    _serve(monkeypatch, _good_archive())
    updater.stage_update(_info())
    updater.clear_pending()
    assert updater.pending_update() is None


def test_pending_update_none_without_metadata() -> None:
    assert updater.pending_update() is None
