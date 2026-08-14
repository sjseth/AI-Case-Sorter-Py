"""``ui.formatting`` — date/time rendering that follows the OS's regional
format (JL live-testing, 2026-08-13) rather than a hardcoded ISO policy.

Every assertion here pins ``_locale()`` to a specific ``QLocale`` instead of
asserting against the real system locale, which would make the suite's
result depend on whatever machine runs it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QLocale

from sorter.ui import formatting

SWEDISH = QLocale("sv_SE")
US = QLocale("en_US")


@pytest.fixture
def swedish(monkeypatch) -> None:
    monkeypatch.setattr(formatting, "_locale", lambda: SWEDISH)


@pytest.fixture
def american(monkeypatch) -> None:
    monkeypatch.setattr(formatting, "_locale", lambda: US)


# ----- format_date ------------------------------------------------------------


def test_swedish_locale_renders_iso(swedish) -> None:
    assert formatting.format_date("2026-08-03") == "2026-08-03"


def test_us_locale_renders_month_day_year(american) -> None:
    assert formatting.format_date("2026-08-03") == "8/3/26"


def test_format_date_accepts_a_datetime_object(swedish) -> None:
    assert formatting.format_date(datetime(2026, 8, 3, 9, 5)) == "2026-08-03"


def test_format_date_accepts_a_bare_date(swedish) -> None:
    from datetime import date

    assert formatting.format_date(date(2026, 8, 3)) == "2026-08-03"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_format_date_empty_input_is_the_empty_marker(swedish, value) -> None:
    assert formatting.format_date(value) == formatting.EMPTY_VALUE


def test_format_date_unparseable_text_is_the_empty_marker(swedish) -> None:
    assert formatting.format_date("not a date") == formatting.EMPTY_VALUE


# ----- format_datetime ---------------------------------------------------------


def test_swedish_locale_renders_24h_clock(swedish) -> None:
    assert formatting.format_datetime("2026-08-03 09:05") == "2026-08-03 09:05"


def test_us_locale_renders_12h_clock(american) -> None:
    # ICU inserts a narrow no-break space (U+202F) before the AM/PM marker;
    # assert on the parts rather than pin that separator by exact codepoint.
    rendered = formatting.format_datetime("2026-08-03 09:05")
    assert rendered.startswith("8/3/26 9:05")
    assert rendered.endswith("AM")


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-03T09:05:00",
        "2026-08-03T09:05:00Z",
        "2026-08-03 09:05:00",
        "2026-08-03 09:05",
    ],
)
def test_format_datetime_tolerates_iso_variants(swedish, raw) -> None:
    assert formatting.format_datetime(raw) == "2026-08-03 09:05"


@pytest.mark.parametrize("value", [None, ""])
def test_format_datetime_empty_input_is_the_empty_marker(swedish, value) -> None:
    assert formatting.format_datetime(value) == formatting.EMPTY_VALUE


# ----- parse (the sortable-key seam) -------------------------------------------


def test_parse_returns_none_for_garbage() -> None:
    assert formatting.parse("garbage") is None
    assert formatting.parse(None) is None
    assert formatting.parse("") is None


def test_parse_round_trips_a_datetime() -> None:
    dt = datetime(2026, 8, 3, 9, 5, 30)
    assert formatting.parse(dt) is dt


def test_parse_ignores_a_trailing_timezone_offset_beyond_the_recognized_prefix() -> None:
    # Only the fixed-width ISO prefix is parsed; anything after it (offset,
    # fractional seconds) is dropped rather than raising.
    parsed = formatting.parse("2026-08-03T09:05:00.123456+00:00")
    assert parsed == datetime(2026, 8, 3, 9, 5, 0)
