"""Tests for Community-tab helpers that don't require a live Tk display.

Importing the module is display-free (tkinter only needs a display when a
root window is actually created), so the static `_format_identity` helper
can be exercised directly.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("cv2")

from sorter.ui.tab_community import CommunityTab


def test_format_identity_name_and_email() -> None:
    assert CommunityTab._format_identity("Jane Doe", "jane@example.com") == "Jane Doe (jane@example.com)"


def test_format_identity_name_only() -> None:
    assert CommunityTab._format_identity("Jane Doe", None) == "Jane Doe"
    assert CommunityTab._format_identity("Jane Doe", "  ") == "Jane Doe"


def test_format_identity_email_only() -> None:
    assert CommunityTab._format_identity(None, "jane@example.com") == "jane@example.com"


def test_format_identity_neither() -> None:
    assert CommunityTab._format_identity(None, None) == "(unknown)"
    assert CommunityTab._format_identity("", "") == "(unknown)"
