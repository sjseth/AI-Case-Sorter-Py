"""Developer environment overrides: .env loading, API base, TLS trust.

The insecure-TLS switch is the interesting part — it must apply for a loopback
backend and must NOT apply to anything else, however it is spelled.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import requests

from sorter import appenv
from sorter.auth import AuthManager


@pytest.fixture(autouse=True)
def _reset_warnings():
    """Warnings are one-shot per process; clear them between tests."""
    appenv._warned.clear()


# ----- .env parsing ----------------------------------------------------------


def test_parse_env_text_handles_comments_quotes_and_export() -> None:
    parsed = appenv.parse_env_text(
        "\n"
        "# a comment\n"
        "CASESORTER_API_BASE=https://localhost:7043/api\n"
        "  export QUOTED = 'value with spaces' \n"
        'DOUBLE="dq"\n'
        "novalue\n"
        "EMPTY=\n"
    )
    assert parsed == {
        "CASESORTER_API_BASE": "https://localhost:7043/api",
        "QUOTED": "value with spaces",
        "DOUBLE": "dq",
        "EMPTY": "",
    }


def test_load_dotenv_applies_file_without_clobbering_real_env(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("CASESORTER_API_BASE=https://localhost:7043/api\nOTHER=x\n")
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(env_file))
    monkeypatch.setenv("OTHER", "already-set")

    applied = appenv.load_dotenv()

    assert env_file in applied
    assert appenv.api_base() == "https://localhost:7043/api"
    # A real environment variable outranks the file.
    assert os.environ["OTHER"] == "already-set"


def test_load_dotenv_is_a_noop_without_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert appenv.load_dotenv() == []
    assert appenv.api_base() == appenv.DEFAULT_API_BASE


def test_load_dotenv_survives_an_unreadable_file(tmp_path, monkeypatch) -> None:
    """A developer convenience must never stop the app from starting."""
    bad = tmp_path / "dir.env"
    bad.mkdir()  # is_file() is False → skipped, not raised
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(bad))
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert appenv.load_dotenv() == []


# ----- api base --------------------------------------------------------------


def test_api_base_default_and_override(monkeypatch) -> None:
    assert appenv.api_base() == "https://www.reloadingrecipes.com/api"
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost:7043/api/")
    assert appenv.api_base() == "https://localhost:7043/api"  # trailing / trimmed
    monkeypatch.setenv("CASESORTER_API_BASE", "   ")
    assert appenv.api_base() == appenv.DEFAULT_API_BASE  # blank → default


# ----- tls trust -------------------------------------------------------------


def test_tls_verify_defaults_to_system_trust() -> None:
    assert appenv.tls_verify() is True


def test_ca_bundle_is_used_when_it_exists(tmp_path, monkeypatch) -> None:
    pem = tmp_path / "devcert.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("CASESORTER_API_CA_BUNDLE", str(pem))
    assert appenv.tls_verify() == str(pem)


def test_missing_ca_bundle_falls_back_to_system_trust(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_API_CA_BUNDLE", str(tmp_path / "gone.pem"))
    assert appenv.tls_verify() is True


def test_ca_bundle_wins_over_insecure(tmp_path, monkeypatch) -> None:
    pem = tmp_path / "devcert.pem"
    pem.write_text("x")
    monkeypatch.setenv("CASESORTER_API_CA_BUNDLE", str(pem))
    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost:7043/api")
    assert appenv.tls_verify() == str(pem)


def test_insecure_honoured_for_loopback(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")
    for base in (
        "https://localhost:7043/api",
        "https://127.0.0.1:7043/api",
        "https://127.0.1.5/api",
        "https://app.localhost/api",
        "https://[::1]:7043/api",
    ):
        monkeypatch.setenv("CASESORTER_API_BASE", base)
        appenv._warned.clear()
        assert appenv.tls_verify() is False, base


def test_insecure_ignored_for_remote_hosts(monkeypatch) -> None:
    """A stray CASESORTER_API_INSECURE can never weaken production traffic."""
    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")
    for base in (
        appenv.DEFAULT_API_BASE,
        "https://evil.example.com/api",
        "https://localhost.evil.example.com/api",  # not a loopback host
    ):
        monkeypatch.setenv("CASESORTER_API_BASE", base)
        appenv._warned.clear()
        assert appenv.tls_verify() is True, base


def test_insecure_ignored_with_the_default_base(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")  # no base override at all
    assert appenv.tls_verify() is True


def test_insecure_flag_spellings(monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost/api")
    for value, expected in (
        ("1", False),
        ("true", False),
        ("TRUE", False),
        ("yes", False),
        ("on", False),
        ("0", True),
        ("false", True),
        ("", True),
        ("maybe", True),
    ):
        monkeypatch.setenv("CASESORTER_API_INSECURE", value)
        appenv._warned.clear()
        assert appenv.tls_verify() is expected, value


def test_describe_reports_the_effective_settings(tmp_path, monkeypatch) -> None:
    assert "system trust" in appenv.describe()
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost:7043/api")
    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")
    text = appenv.describe()
    assert "localhost:7043" in text and "DISABLED" in text


def test_warnings_are_emitted_once(capsys, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost/api")
    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")
    for _ in range(3):
        appenv.tls_verify()
    assert capsys.readouterr().err.count("TLS verification is DISABLED") == 1


# ----- wiring into the API client --------------------------------------------


def test_community_api_picks_up_the_override(monkeypatch) -> None:
    from sorter.community_api import API_BASE, CommunityApi

    class _Auth(AuthManager):
        def __init__(self) -> None:
            pass  # skip MSAL setup

        def acquire_token_silent(self, scopes=None):
            return None

    class _Session(requests.Session):
        verify = True

    assert CommunityApi(auth=_Auth(), session=_Session()).base_url == API_BASE
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost:7043/api/")
    assert CommunityApi(auth=_Auth(), session=_Session()).base_url == "https://localhost:7043/api"


def test_community_api_resolves_tls_trust(tmp_path, monkeypatch) -> None:
    from sorter.community_api import CommunityApi

    class _Auth(AuthManager):
        def __init__(self) -> None:
            pass  # skip MSAL setup

        def acquire_token_silent(self, scopes=None):
            return None

    class _Session(requests.Session):
        verify = True

    pem = tmp_path / "devcert.pem"
    pem.write_text("x")
    monkeypatch.setenv("CASESORTER_API_CA_BUNDLE", str(pem))
    assert CommunityApi(auth=_Auth(), session=_Session()).verify == str(pem)


def test_explicit_verify_argument_wins(monkeypatch) -> None:
    from sorter.community_api import CommunityApi

    class _Auth(AuthManager):
        def __init__(self) -> None:
            pass  # skip MSAL setup

        def acquire_token_silent(self, scopes=None):
            return None

    class _Session(requests.Session):
        verify = True

    monkeypatch.setenv("CASESORTER_API_INSECURE", "1")
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost/api")
    assert CommunityApi(auth=_Auth(), session=_Session(), verify=True).verify is True


def test_env_file_candidate_order(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(tmp_path / "explicit.env"))
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    candidates = appenv.env_file_candidates()
    assert candidates[0] == tmp_path / "explicit.env"
    assert candidates[1] == tmp_path / "data" / "config" / ".env"
    assert candidates[2] == Path(appenv.__file__).resolve().parent.parent / ".env"


def test_verify_is_passed_per_request_not_on_the_session(tmp_path, monkeypatch) -> None:
    """Regression guard: session.verify is not enough.

    requests lets REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in the environment
    outrank ``session.verify``, so a session-level setting is silently ignored
    on any machine that sets one (corporate proxies do). Only a per-request
    ``verify=`` wins.
    """
    from tests.unit.test_community_api import _api, _FakeResp, _FakeSession

    pem = tmp_path / "devcert.pem"
    pem.write_text("x")
    monkeypatch.setenv("CASESORTER_API_CA_BUNDLE", str(pem))

    s = _FakeSession()
    api = _api(s)
    url = f"{api.base_url}/Models/FetchWishList?communityModelId=uid-1"
    s.next_responses[url] = _FakeResp(json_data=["FC"])
    api.fetch_wish_list("uid-1")
    assert s.verify_args == [str(pem)]


def test_default_verify_stays_true_so_env_ca_bundles_still_work(tmp_path) -> None:
    """Passing True (not None) keeps requests' own env-bundle handling intact."""
    from tests.unit.test_community_api import _api, _FakeResp, _FakeSession

    s = _FakeSession()
    api = _api(s)
    url = f"{api.base_url}/Models/FetchWishList?communityModelId=uid-1"
    s.next_responses[url] = _FakeResp(json_data=[])
    api.fetch_wish_list("uid-1")
    assert s.verify_args == [True]


# ----- .env encodings (Windows) ----------------------------------------------


BODY = (
    "CASESORTER_API_BASE=https://localhost:5001/api\n"
    "#CASESORTER_API_CA_BUNDLE=./devcert.pem\n"
    "CASESORTER_API_INSECURE=1\n"
)

ENCODINGS = {
    "plain_lf": BODY.encode(),
    "crlf": BODY.replace("\n", "\r\n").encode(),
    # Notepad's default. The BOM used to corrupt the first key's NAME, so the
    # base URL was silently dropped and the app kept talking to production.
    "utf8_bom": b"\xef\xbb\xbf" + BODY.encode(),
    "utf8_bom_crlf": b"\xef\xbb\xbf" + BODY.replace("\n", "\r\n").encode(),
    # PowerShell's `>` redirection.
    "utf16_le_bom": BODY.encode("utf-16"),
    "trailing_space": BODY.replace("\n", "   \n").encode(),
}


@pytest.mark.parametrize("name", sorted(ENCODINGS))
def test_env_file_applies_whatever_the_editor_saved(name, tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_bytes(ENCODINGS[name])
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(env_file))

    assert appenv.load_dotenv() == [env_file]
    assert appenv.api_base() == "https://localhost:5001/api"
    assert appenv.tls_verify() is False  # loopback + insecure
    # The commented-out line stays commented out.
    assert not os.environ.get("CASESORTER_API_CA_BUNDLE")


@pytest.mark.parametrize("raw", [b"", b"\x00\x01\x02", bytes(range(256))])
def test_unparseable_env_file_never_crashes_startup(raw, tmp_path, monkeypatch) -> None:
    """os.environ rejects keys with NULs — garbage must not reach it."""
    env_file = tmp_path / "dev.env"
    env_file.write_bytes(raw)
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(env_file))
    appenv.load_dotenv()  # must not raise
    assert appenv.api_base() == appenv.DEFAULT_API_BASE


def test_parse_drops_keys_that_are_not_env_var_names() -> None:
    parsed = appenv.parse_env_text("GOOD=1\nbad key=2\n\x00NUL=3\n9LEADING=4\ndot.ted=5\n")
    assert parsed == {"GOOD": "1"}


# ----- startup diagnostics ----------------------------------------------------


def test_startup_report_is_silent_by_default() -> None:
    assert appenv.startup_report([]) == []


def test_startup_report_names_the_file_its_keys_and_the_effect(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_bytes(ENCODINGS["utf8_bom"])
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(env_file))
    lines = appenv.startup_report(appenv.load_dotenv())
    assert str(env_file) in lines[0]
    assert "CASESORTER_API_BASE" in lines[0]
    assert "localhost:5001" in lines[1] and "DISABLED" in lines[1]


def test_startup_report_flags_a_file_that_held_nothing(tmp_path, monkeypatch) -> None:
    """A found-but-useless file reads as such instead of looking applied."""
    env_file = tmp_path / "dev.env"
    env_file.write_text("# just a comment\n")
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(env_file))
    lines = appenv.startup_report(appenv.load_dotenv())
    assert "no settings found" in lines[0]
    assert appenv.DEFAULT_API_BASE in lines[1]


def test_startup_report_hints_at_a_misnamed_env_file(tmp_path, monkeypatch) -> None:
    """Explorer hides .txt, so 'Save as .env' in Notepad yields '.env.txt'."""
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(tmp_path / ".env"))
    (tmp_path / ".env.txt").write_text("CASESORTER_API_BASE=https://localhost:5001/api\n")
    lines = appenv.startup_report(appenv.load_dotenv())
    assert lines and "misnamed" in lines[0] and ".env.txt" in lines[0]


def test_no_misnaming_hint_once_settings_are_in_effect(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("CASESORTER_API_BASE", "https://localhost:5001/api")
    (tmp_path / ".env.txt").write_text("x")
    lines = appenv.startup_report(appenv.load_dotenv())
    assert not any("misnamed" in line for line in lines)
