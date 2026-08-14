"""App-wide date/time rendering — the OS's regional format, not a fixed policy.

JL's ask (live-testing, 2026-08-13): dates/times must follow the *regional*
format the OS reports — Control Panel on Windows, ``LC_TIME`` on Linux —
independent of UI language. That's what makes an English-language system
with Swedish regional settings render ``YYYY-MM-DD`` on a 24h clock while a
US-format machine renders its own conventions. ``QLocale.system()`` already
resolves exactly that, so this module is a thin, testable wrapper around it
rather than a hardcoded format string — never ``strftime``'s C-locale
defaults, never a fixed ISO pattern.

Every user-visible date/time in the UI routes through ``format_date``/
``format_datetime``. Anything that needs a *sortable* key alongside the
display text (the Models table, the evaluator's report history) should sort
on the raw value via ``parse()``, not on the locale-rendered string — a
locale format doesn't sort chronologically in general (``8/3/26`` vs.
``12/1/25``).
"""

from __future__ import annotations

from datetime import date, datetime

from PySide6.QtCore import QDate, QDateTime, QLocale, QTime

EMPTY_VALUE = "—"

# Fixed widths so a shorter pattern's prefix can't accidentally match inside
# a longer, higher-precision timestamp (fractional seconds, a timezone
# offset) — only the recognized prefix is parsed, the rest is ignored.
_FORMATS: tuple[tuple[str, int], ...] = (
    ("%Y-%m-%dT%H:%M:%S", 19),
    ("%Y-%m-%d %H:%M:%S", 19),
    ("%Y-%m-%d %H:%M", 16),
    ("%Y-%m-%d", 10),
)


def _locale() -> QLocale:
    """The locale to format with — a seam tests pin instead of asserting
    against whatever locale happens to be installed on the machine running
    the suite."""
    return QLocale.system()


def parse(value: datetime | date | str | None) -> datetime | None:
    """Coerce a ``datetime``, a ``date``, or an ISO-ish string to a ``datetime``.

    Tolerant of ``None``/``""`` and of text that isn't a recognized
    timestamp — both return ``None``, which the ``format_*`` helpers below
    turn into ``EMPTY_VALUE``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = value.strip().rstrip("Z")
    if not text:
        return None
    for fmt, width in _FORMATS:
        try:
            return datetime.strptime(text[:width], fmt)
        except ValueError:
            continue
    return None


def format_date(value: datetime | date | str | None) -> str:
    """``value`` rendered as the OS's regional short date, or ``EMPTY_VALUE``."""
    parsed = parse(value)
    if parsed is None:
        return EMPTY_VALUE
    qdate = QDate(parsed.year, parsed.month, parsed.day)
    return _locale().toString(qdate, QLocale.FormatType.ShortFormat)


def format_datetime(value: datetime | date | str | None) -> str:
    """``value`` rendered as the OS's regional short date + time, or ``EMPTY_VALUE``."""
    parsed = parse(value)
    if parsed is None:
        return EMPTY_VALUE
    qdt = QDateTime(
        QDate(parsed.year, parsed.month, parsed.day),
        QTime(parsed.hour, parsed.minute, parsed.second),
    )
    return _locale().toString(qdt, QLocale.FormatType.ShortFormat)
