"""Context-aware in-app help: renders ``docs/guide/GUIDE.md``, GitHub and in-app alike.

Single-source-of-truth docs: the same Markdown GitHub renders for
``docs/guide/GUIDE.md`` is loaded here via ``QTextBrowser.setSource`` —
verified in PySide6 6.11.1 to auto-detect and render ``.md`` files with no
extra dependency.

One gap found: Qt's markdown-to-richtext conversion never emits a named HTML
anchor for a heading — checked against ``QTextDocument.toHtml()``, and even a
literal ``<a name="...">`` written into the Markdown source doesn't survive
the conversion — so ``QTextBrowser.scrollToAnchor`` never has anything to
find (confirmed it *does* work, against a document built with real anchors
via ``setHtml``; the gap is specifically the Markdown path). Anchors are
resolved by hand instead: walk the rendered document's blocks for a heading
whose GitHub-style slug matches, then move the cursor there —
``setTextCursor`` alone was enough to scroll the view in this Qt build;
``ensureCursorVisible`` is kept as a belt-and-braces call.

A second gap, found while porting to a single-file guide: ``QTextBrowser``'s
back/forward history is source-load based, not scroll-position based — it
never gains an entry from our hand-rolled cursor moves, confirmed empirically
(``isBackwardAvailable()`` stayed ``False`` across repeated anchor jumps,
identical-URL ``setSource`` calls, and even a URL carrying the target
fragment directly, since that fragment resolves against the *converted*
document, which has no real anchors either — see above). ``HelpWindow`` keeps
its own back-stack of topics instead.

``topic_for`` is the seam the app shell binds to F1 / Help menu: it turns
"where the user currently is" into a topic string this viewer understands —
now a bare anchor into the one merged guide (``""`` means the top of the
document).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..paths import app_root

DEFAULT_TOPIC = ""

# page name -> topic, for pages whose topic doesn't depend on anything else.
# "Settings" isn't here: its topic also depends on settings_section (its
# sections' names are the guide's headings, so they slugify straight to the
# anchor). A page with no entry — or an anchor the guide doesn't have yet —
# falls back to DEFAULT_TOPIC: F1 always opens *something* useful.
_PAGE_TOPICS = {
    "Sort": "sort-dashboard",
    "Train": "train",
    "AI Config": "ai-config",
    "Models": "models",
    "Community": "community",
}

_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")
_SLUG_SPACE_RE = re.compile(r"\s+")


def _slugify(text: str) -> str:
    """GitHub-style heading anchor: lowercase, spaces to hyphens, punctuation dropped."""
    text = _SLUG_STRIP_RE.sub("", text.strip().lower())
    return _SLUG_SPACE_RE.sub("-", text)


def topic_for(page_name: str, settings_section: str | None = None) -> str:
    """Map the shell's current location to a help topic — a bare anchor into ``GUIDE.md``.

    A page or section this guide doesn't document (yet) still returns a
    string ``show_topic`` can use: if the anchor isn't found, it falls back
    to the top of the guide rather than erroring.
    """
    if page_name == "Settings":
        return _slugify(settings_section) if settings_section else "settings"
    return _PAGE_TOPICS.get(page_name, DEFAULT_TOPIC)


class HelpWindow(QWidget):
    """Markdown viewer over ``docs/guide/GUIDE.md`` — the User Guide panel's content."""

    def __init__(self, parent: QWidget | None = None, *, docs_root: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Case Sorter Help")
        self.resize(640, 520)
        self._docs_root = docs_root if docs_root is not None else app_root() / "docs" / "guide"
        self._guide_path = self._docs_root / "GUIDE.md"
        self._loaded_mtime: float | None = None

        # Our own back-stack: QTextBrowser's history tracks source loads, not
        # the hand-rolled cursor moves anchor navigation uses here — see the
        # module docstring.
        self._current_topic: str | None = None
        self._back_stack: list[str] = []

        column = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self._back_button = QPushButton("< Back", self)
        self._back_button.setEnabled(False)
        toolbar.addWidget(self._back_button)
        toolbar.addStretch(1)
        column.addLayout(toolbar)

        self.browser = QTextBrowser(self)
        # We route every navigation through show_topic ourselves (anchors need
        # the hand-rolled scroll above); Qt's own auto-open would bypass it.
        self.browser.setOpenLinks(False)
        column.addWidget(self.browser, 1)

        self._back_button.clicked.connect(self._go_back)
        self.browser.anchorClicked.connect(self._on_anchor_clicked)

        self.show_topic(DEFAULT_TOPIC)

    # ----- navigation -----------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load ``GUIDE.md`` once; reload only if it changed on disk since.

        ``QTextBrowser`` caches a loaded resource by URL, so a plain
        ``setSource`` on the *same* path is a no-op on a second call — proven
        empirically (the mtime tracking updated but the text didn't). Its
        ``reload()`` (Qt 5.14+) is what actually discards that cache.
        """
        try:
            mtime = self._guide_path.stat().st_mtime
        except OSError:
            return
        if self._loaded_mtime is None:
            self.browser.setSource(QUrl.fromLocalFile(str(self._guide_path)))
            self._loaded_mtime = mtime
        elif mtime != self._loaded_mtime:
            self.browser.reload()
            self._loaded_mtime = mtime

    def show_topic(self, topic: str) -> None:
        """Scroll to ``topic``'s heading anchor; empty or unresolved falls back to the top."""
        self._ensure_loaded()
        if self._current_topic is not None and topic != self._current_topic:
            self._back_stack.append(self._current_topic)
            self._back_button.setEnabled(True)
        self._current_topic = topic
        if not topic or not self._scroll_to_anchor(topic):
            self._scroll_to_top()

    def _go_back(self) -> None:
        if not self._back_stack:
            return
        previous = self._back_stack.pop()
        self._current_topic = previous
        if not previous or not self._scroll_to_anchor(previous):
            self._scroll_to_top()
        self._back_button.setEnabled(bool(self._back_stack))

    def _scroll_to_top(self) -> None:
        cursor = self.browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.browser.setTextCursor(cursor)
        self.browser.ensureCursorVisible()

    def _scroll_to_anchor(self, anchor: str) -> bool:
        """Find the heading whose slug matches and scroll it to the viewport top.

        ``scrollToAnchor`` can't do this for a Markdown-loaded document — see
        the module docstring. ``ensureCursorVisible`` alone scrolls minimally,
        which parks a heading reached from above at the *bottom* edge; the
        scrollbar nudge after it aligns the heading to the top instead.
        """
        block = self.browser.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() > 0 and _slugify(block.text()) == anchor:
                self.browser.setTextCursor(QTextCursor(block))
                self.browser.ensureCursorVisible()
                bar = self.browser.verticalScrollBar()
                bar.setValue(bar.value() + self.browser.cursorRect().top())
                return True
            block = block.next()
        return False

    def _on_anchor_clicked(self, url: QUrl) -> None:
        """Route an internal link back through ``show_topic`` so anchors keep working.

        Every link in the guide is now same-page (``#anchor``); Qt resolves
        it against the loaded ``GUIDE.md``, so only the fragment matters.
        """
        if url.scheme() not in ("", "file"):
            return  # an external link (http, mailto, ...): not ours to open
        self.show_topic(url.fragment())


def build_help_window(win: Any, *, docs_root: Path | None = None) -> HelpWindow:
    """The app shell's single entry point — one instance, hosted by the guide panel."""
    return HelpWindow(win, docs_root=docs_root)
