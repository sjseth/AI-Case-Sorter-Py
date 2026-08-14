"""In-app help viewer: topic mapping, Markdown rendering, and doc integrity.

Offscreen, no display. ``show_topic``/anchor-click tests load the REAL
``docs/guide/GUIDE.md`` file — that's the point: they double as an end-to-end
proof that the same Markdown GitHub renders also renders (and navigates)
inside the app. The docs-integrity test is the other half of that guarantee:
every intra-page anchor link in the guide has to actually resolve to a real
heading, or GitHub's rendering and the in-app rendering would silently
disagree.

Since the guide is a single file, ``browser.toPlainText()`` always contains
every section regardless of scroll position — it can no longer prove a
navigation happened (that only worked back when each page was a separate
file). Tests instead check the block the cursor landed on
(``browser.textCursor().block().text()``), which is exactly what
``_scroll_to_anchor`` moves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl

from sorter.qtui.help_viewer import HelpWindow, _slugify, build_help_window, topic_for

DOCS_GUIDE = Path(__file__).resolve().parents[3] / "docs" / "guide"
GUIDE_MD = DOCS_GUIDE / "GUIDE.md"


def _pump(qapp) -> None:
    qapp.processEvents()


def _block_text(window: HelpWindow) -> str:
    return window.browser.textCursor().block().text()


# ----- topic_for ------------------------------------------------------------


@pytest.mark.parametrize(
    ("page_name", "settings_section", "expected"),
    [
        ("Sort", None, "sort-dashboard"),
        ("Settings", "Serial", "serial"),
        ("Settings", "Camera", "camera"),
        ("Settings", "Image Processing", "image-processing"),
        ("Settings", "Theme", "theme"),
        ("Settings", None, "settings"),
        ("Train", None, "train"),
        ("AI Config", None, "ai-config"),
        ("Models", None, "models"),
        ("Community", None, "community"),
        ("", None, ""),
        ("Sort", "Serial", "sort-dashboard"),  # non-Settings pages ignore the section arg
    ],
)
def test_topic_for_maps_location_to_topic(page_name, settings_section, expected) -> None:
    assert topic_for(page_name, settings_section) == expected


# ----- show_topic against the real guide -------------------------------------


@pytest.fixture
def help_window(qapp):
    window = HelpWindow(None)
    window.resize(320, 140)  # small viewport: anchor scroll tests need real overflow
    window.show()
    yield window
    window.close()


def test_show_topic_scrolls_to_sort_dashboard(help_window, qapp) -> None:
    help_window.show_topic("sort-dashboard")
    _pump(qapp)
    assert _block_text(help_window) == "Sort dashboard"


def test_show_topic_scrolls_to_settings_subsections(help_window, qapp) -> None:
    help_window.show_topic("settings")
    _pump(qapp)
    assert _block_text(help_window) == "Settings"

    help_window.show_topic("serial")
    _pump(qapp)
    assert _block_text(help_window) == "Serial"

    help_window.show_topic("camera")
    _pump(qapp)
    assert _block_text(help_window) == "Camera"


def test_a_downward_jump_parks_the_heading_at_the_viewport_top(help_window, qapp) -> None:
    # ensureCursorVisible alone scrolls minimally, leaving a heading reached
    # from above at the bottom edge — the regression JL hit on Community.
    help_window.show_topic("")
    _pump(qapp)
    help_window.show_topic("community")
    _pump(qapp)
    assert _block_text(help_window) == "Community"
    rect = help_window.browser.cursorRect()
    assert 0 <= rect.top() <= rect.height()

    help_window.show_topic("models")  # and the same when jumping back up
    _pump(qapp)
    assert _block_text(help_window) == "Models"
    rect = help_window.browser.cursorRect()
    assert 0 <= rect.top() <= rect.height()


def test_empty_topic_scrolls_to_top(help_window, qapp) -> None:
    help_window.show_topic("settings")
    _pump(qapp)
    settings_scroll = help_window.browser.verticalScrollBar().value()

    help_window.show_topic("")
    _pump(qapp)
    assert help_window.browser.verticalScrollBar().value() < settings_scroll
    assert "AI Case Sorter — User Guide" in _block_text(help_window)


def test_unknown_topic_falls_back_to_top(help_window, qapp) -> None:
    help_window.show_topic("settings")
    _pump(qapp)
    settings_scroll = help_window.browser.verticalScrollBar().value()

    help_window.show_topic("this-topic-does-not-exist")
    _pump(qapp)
    assert help_window.browser.verticalScrollBar().value() < settings_scroll
    assert "AI Case Sorter — User Guide" in _block_text(help_window)


def test_toc_link_navigates_within_the_page(help_window, qapp) -> None:
    help_window.show_topic("")
    _pump(qapp)

    # "Trigger anchorClicked programmatically" — the URL a real click on
    # `[Settings](#settings)` resolves to: GUIDE.md's own file:// path plus
    # the fragment, confirmed against a live click in the exploration for
    # this module (a simulated QTest.mouseClick doesn't reliably fire the
    # signal under the offscreen platform, so it isn't used directly here).
    target = QUrl.fromLocalFile(str(GUIDE_MD))
    target.setFragment("settings")
    help_window._on_anchor_clicked(target)
    _pump(qapp)

    assert _block_text(help_window) == "Settings"


def test_build_help_window_returns_a_working_viewer(qapp) -> None:
    window = build_help_window(None)
    try:
        window.show()
        _pump(qapp)
        assert "AI Case Sorter — User Guide" in window.browser.toPlainText()
    finally:
        window.close()


# ----- mtime-based reload cache ----------------------------------------------


def test_guide_loads_once_and_reloads_only_on_mtime_change(qapp, tmp_path) -> None:
    guide = tmp_path / "GUIDE.md"
    guide.write_text("# First\n\nContent one.\n")

    window = HelpWindow(None, docs_root=tmp_path)
    window.show()
    try:
        _pump(qapp)
        assert "Content one" in window.browser.toPlainText()
        first_mtime = window._loaded_mtime

        # Navigating without touching the file must not reload it.
        window.show_topic("")
        _pump(qapp)
        assert window._loaded_mtime == first_mtime

        # Editing the file (with a forced mtime bump) must reload it.
        import os
        import time

        time.sleep(0.01)
        guide.write_text("# First\n\nContent TWO.\n")
        os.utime(guide, (time.time() + 1, time.time() + 1))
        window.show_topic("")
        _pump(qapp)
        assert "Content TWO" in window.browser.toPlainText()
        assert window._loaded_mtime != first_mtime
    finally:
        window.close()


# ----- back button: hand-rolled stack, not QTextBrowser history -------------

# Verified empirically (see the module docstring): QTextBrowser's own
# backward()/isBackwardAvailable() never register the hand-rolled anchor
# jumps show_topic performs — isBackwardAvailable() stays False across
# repeated anchor navigation, identical-URL setSource() calls, and even a
# URL carrying the target fragment (which resolves against the converted
# document, where no real anchors exist either). HelpWindow keeps its own
# back-stack of topics instead; these tests pin that behavior.


def test_back_button_returns_to_the_previous_topic(help_window, qapp) -> None:
    help_window.show_topic("sort-dashboard")
    _pump(qapp)
    help_window.show_topic("settings")
    _pump(qapp)
    assert _block_text(help_window) == "Settings"
    assert help_window._back_button.isEnabled()

    help_window._back_button.click()
    _pump(qapp)

    assert _block_text(help_window) == "Sort dashboard"


def test_back_button_chains_through_multiple_topics_then_disables(help_window, qapp) -> None:
    help_window.show_topic("sort-dashboard")
    _pump(qapp)
    help_window.show_topic("settings")
    _pump(qapp)
    help_window.show_topic("serial")
    _pump(qapp)

    help_window._back_button.click()  # -> settings
    _pump(qapp)
    assert _block_text(help_window) == "Settings"
    assert help_window._back_button.isEnabled()

    help_window._back_button.click()  # -> sort-dashboard
    _pump(qapp)
    assert _block_text(help_window) == "Sort dashboard"
    assert help_window._back_button.isEnabled()

    help_window._back_button.click()  # -> "" (the initial topic)
    _pump(qapp)
    assert "AI Case Sorter — User Guide" in _block_text(help_window)
    assert not help_window._back_button.isEnabled()


def test_qtextbrowser_history_does_not_cover_anchor_navigation(help_window, qapp) -> None:
    """Pins the finding driving the hand-rolled back-stack above."""
    help_window.show_topic("sort-dashboard")
    _pump(qapp)
    help_window.show_topic("settings")
    _pump(qapp)

    assert help_window.browser.isBackwardAvailable() is False


# ----- docs-root override (tests must not depend on the real docs/) ---------


def test_docs_root_override_reads_tmp_guide(qapp, tmp_path) -> None:
    (tmp_path / "GUIDE.md").write_text("# Tmp Guide\n\n## Elsewhere\n\n[Target](#target)\n\n## Target\n\nfound it\n")

    window = HelpWindow(None, docs_root=tmp_path)
    window.resize(320, 140)
    window.show()
    try:
        _pump(qapp)
        assert "Tmp Guide" in window.browser.toPlainText()

        window.show_topic("target")
        _pump(qapp)
        assert _block_text(window) == "Target"
    finally:
        window.close()


def test_docs_root_override_missing_file_does_not_crash(qapp, tmp_path) -> None:
    window = HelpWindow(None, docs_root=tmp_path)  # no GUIDE.md written
    window.show()
    try:
        _pump(qapp)
        window.show_topic("anything")
        _pump(qapp)
    finally:
        window.close()


# ----- docs integrity: every intra-page anchor link resolves ----------------

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def _headings(text: str) -> set[str]:
    return {_slugify(m.group(2)) for m in _HEADING_RE.finditer(text)}


def test_guide_exists() -> None:
    assert GUIDE_MD.is_file(), f"expected {GUIDE_MD} to exist"


def test_every_intra_page_anchor_link_resolves() -> None:
    text = GUIDE_MD.read_text(encoding="utf-8")
    headings = _headings(text)
    for link in _LINK_RE.findall(text):
        if link.startswith(("http://", "https://", "mailto:")):
            continue  # not ours to validate
        assert link.startswith("#"), (
            f"GUIDE.md: link {link!r} is not a same-page anchor link (the guide is a single file now)"
        )
        anchor = link[1:]
        assert anchor in headings, (
            f"GUIDE.md: link {link!r}'s anchor {anchor!r} matches no heading (have {sorted(headings)})"
        )


def test_every_topic_the_shell_can_ask_for_has_a_heading() -> None:
    """``topic_for``'s answers and the guide's headings must not drift apart.

    Every page and every Settings section resolves to an anchor here; a
    renamed heading (or a new section with no guide content) silently
    degrades to the top of the guide, which this catches instead.
    """
    from sorter.qtui.app import ACTIVITIES, MODE_ACTIVITIES, SETTINGS_SECTIONS

    headings = _headings(GUIDE_MD.read_text(encoding="utf-8"))
    topics = {topic_for(name) for _icon, name in (*ACTIVITIES, *MODE_ACTIVITIES)}
    topics |= {topic_for("Settings", section) for section in SETTINGS_SECTIONS}
    topics.add(topic_for("Settings"))
    for topic in topics:
        assert topic in headings, f"GUIDE.md has no heading for topic {topic!r}"


def test_no_duplicate_heading_slugs() -> None:
    """A slug collision would silently break both GitHub's and our own anchor resolution."""
    text = GUIDE_MD.read_text(encoding="utf-8")
    slugs = [_slugify(m.group(2)) for m in _HEADING_RE.finditer(text)]
    duplicates = {slug for slug in slugs if slugs.count(slug) > 1}
    assert not duplicates, f"duplicate heading slugs in GUIDE.md: {duplicates}"
