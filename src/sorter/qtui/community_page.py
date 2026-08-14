"""Community activity — browse, search and install published models.

Behavior reference: ``ui/tab_community.py``. Same catalogue, same filters, the
same download → ``import_model(community_download=True)`` path, and the same
post-install messaging; the layout is the table-plus-action-bar idiom the
Models activity uses rather than Tk's stack of wide cards, so the two libraries
read alike.

The bar carries the **whole local lifecycle** rather than Download alone: one
state-driven primary (download / update in place / nothing to do) plus Remove
for the installed copy, both scoped to the selected row and decided by
``installed_state`` against the library — re-derived on ``models/changed``, so
a delete anywhere is reflected here. A second click while a transfer runs
queues behind it (``_enqueue_download``) instead of being dropped. (Per-row
icon buttons were tried and dropped: JL lived with them and chose the bar.)

Four things worth knowing:

* **The page is only usable signed in.** Tk mounts the tab on sign-in and
  forgets it on sign-out; here the page always exists and shows a sign-in
  panel until there is an account, and ``app.py`` hides the sidebar entry the
  same way Tk hides the tab (see ``refresh_auth_state``).
* **A download is the publisher's model** — ``community_download=True`` stamps
  it ``CommunityManaged``, which is what keeps Train off it (CLAUDE.md §5).
  Nothing else in this module is allowed to weaken that.
* **The auth manager is constructed lazily, behind a user action only.**
  Building the page must never touch MSAL, so ``is_signed_in`` reads whatever
  the window already has and ``auth_manager()`` is what may create one.
* **``start_update`` is the Sort page's door into this one.** The status-bar
  model-update prompt drives the catalogue's own ``download()``, so there is
  one download+import path and one set of prompts, never a second copy.

``confirm`` / ``ask_download_choice`` / ``open_login`` / ``open_share`` /
``api_factory`` are instance attributes so a test drives every path with no
modal, no MSAL and no network.
"""

from __future__ import annotations

import shutil
import tempfile
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..community.community_api import CartridgeInfo, CommunityApi, ModelInfo
from ..data.model_io import import_model
from ..data.repository import ModelRepo
from . import formatting

COLUMNS = ("Model", "Cartridge", "Version", "Includes", "Headstamps", "Images", "Size", "Published", "Author", "State")
# What the search box narrows the loaded rows against, in-place.
LIVE_FILTER_COLUMNS = (COLUMNS.index("Model"), COLUMNS.index("Cartridge"), COLUMNS.index("Author"))

# Display label -> the server's ModelType query value (empty = no filter).
TYPE_FILTERS: dict[str, str] = {
    "All types": "",
    "Model and images": "ModelAndImages",
    "Model only": "ModelOnly",
    "Images only": "ImagesOnly",
}
ALL_CARTRIDGES = "All cartridges"

STATE_DOWNLOAD = "download"
STATE_UPDATE = "update"
STATE_INSTALLED = "installed"
STATE_LABELS = {
    STATE_DOWNLOAD: "Available",
    STATE_UPDATE: "Update available",
    STATE_INSTALLED: "Installed",
}
# The primary button says what the selected row's state makes possible; an
# installed, current row leaves it dead and hands the action to Remove, which
# is what closes the install → update → remove loop (JL).
ACTION_LABELS = {
    STATE_DOWNLOAD: "Download model",
    STATE_UPDATE: "Update model",
    STATE_INSTALLED: "Already installed",
}
ACTION_TOOLTIPS = {
    STATE_DOWNLOAD: "Download and install this model",
    STATE_UPDATE: "Update the installed copy to this version",
    STATE_INSTALLED: "This version is already installed",
}
DOWNLOADING_LABEL = "Downloading…"
REMOVE_LABEL = "Remove"
REMOVE_TOOLTIP = "Remove the installed copy from this machine"
# What the archive carries (the type filter's counterpart in the table), as
# humans read it; the raw WinForms enum name survives for anything unmapped.
INCLUDES_LABELS = {
    "ModelOnly": "Model",
    "ModelAndImages": "Model + images",
    "ImagesOnly": "Images",
}
# Sort order for the State column: needs-action states first, "nothing to do"
# last — alphabetizing the labels ("Available", "Installed", "Update
# available") would not read as a sane order.
_STATE_SORT_RANK = {STATE_DOWNLOAD: 0, STATE_UPDATE: 1, STATE_INSTALLED: 2}

EMPTY_VALUE = "—"
SELECT_HINT = "Select a model to see its description and install it."
SIGNED_OUT_TEXT = (
    "Sign in to browse and download community models.\n\n"
    "Everything else in the app works signed out — the community is the only part that needs an account."
)
DOWNLOAD_SECURITY_TITLE = "Download model — security notice"
UPDATE_TITLE = "Update installed model?"


def format_size(n: int) -> str:
    """Bytes as the catalogue shows them (Tk's ``_format_size``, verbatim)."""
    if n <= 0:
        return EMPTY_VALUE
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def format_date(iso: str) -> str:
    """Publish date, in the user's OS regional format (``qtui.formatting``).

    Thin wrapper kept for the module's existing call site and import; this
    used to hand-roll ``M/D/YYYY`` (the Tk look) — see ``qtui/formatting.py``
    for why that hardcoded policy was replaced (JL live-testing).
    """
    return formatting.format_date(iso)


class ApiWorker(QThread):
    """One community-API call off the UI thread, with a progress channel.

    ``work`` is handed a ``report(str)`` callable; both it and the result cross
    back as queued signals, i.e. on the main thread. Used by the two dialogs in
    this cluster, which take a bare parent and so cannot rely on the window's
    bus-backed ``run_worker``.
    """

    progress = Signal(str)
    done = Signal(object)
    failed = Signal(object)

    def __init__(self, parent: QWidget | None, work: Callable[[Callable[[str], None]], Any]) -> None:
        super().__init__(parent)
        self._work = work

    def run(self) -> None:
        try:
            result = self._work(self.progress.emit)
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(exc)
            return
        self.done.emit(result)


# Workers that must outlive the dialog that started them: an interactive
# sign-in or an upload keeps running after its window closes, and a QThread
# destroyed while running takes the process with it.
_ACTIVE_WORKERS: set[ApiWorker] = set()


def start_worker(worker: ApiWorker) -> ApiWorker:
    """Start a parentless worker, holding it alive until it finishes.

    Connections to a destroyed receiver are dropped by Qt, so a dialog that
    closes mid-flight simply stops hearing about the result.
    """
    _ACTIVE_WORKERS.add(worker)
    worker.finished.connect(lambda: _ACTIVE_WORKERS.discard(worker))
    worker.start()
    return worker


class _SortableItem(QTreeWidgetItem):
    """Row that sorts on typed values rather than its displayed text.

    Same pattern as ``dialog_model_evaluator._SortableItem`` (duplicated
    rather than shared — each qtui page/dialog stays self-contained). Lets
    "Version"/"Headstamps"/"Images"/"Size" sort numerically, "Published"
    sort chronologically on the parsed date rather than the locale-rendered
    text, and "State" sort by actionability rather than alphabetically.
    """

    def __init__(self, texts: Sequence[str], sort_values: Sequence[Any], uid: str) -> None:
        super().__init__(list(texts))
        self.sort_values = list(sort_values)
        self.setData(0, Qt.ItemDataRole.UserRole, uid)

    def __lt__(self, other: Any) -> bool:
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        try:
            return bool(self.sort_values[column] < other.sort_values[column])
        except (AttributeError, IndexError, TypeError):
            return super().__lt__(other)


class CommunityPage(QWidget):
    """The catalogue table, its filters, and the signed-out panel in front of them."""

    def __init__(self, win: Any) -> None:
        super().__init__()
        self._win = win
        self.db = win.db
        self._auth: Any | None = None
        self._models: list[ModelInfo] = []
        # One archive imports at a time (two would race the model tree), but
        # requests queue (JL): each entry is (info, name, update_existing,
        # is_update), prompts already answered at click time.
        self._download_queue: list[tuple[ModelInfo, str, bool, bool]] = []
        self._active_download_uid: str | None = None
        self._batch_total = 0
        self._batch_done = 0
        # The Models page posts this on every refresh: what's installed may
        # have changed (delete/import/activate there), and this table's State
        # cells and buttons must follow without a catalogue re-fetch.
        win.bus.subscribe("models/changed", lambda _payload: self.refresh_local_states())
        self._cartridges: list[CartridgeInfo] = []
        self._loaded = False
        self._busy = False
        self._columns_sized = False
        # No column clicked yet means the server's own result order stands,
        # unchanged, until the first header click — same contract as Models.
        self._sort_column: int | None = None
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
        self.community_username = ""
        self.can_contribute = False

        # Replaceable hooks — a test never opens a native dialog.
        self.confirm: Callable[[str, str], bool] = self._ask
        self.ask_download_choice: Callable[[str], str] = self._ask_download_choice
        self.open_login: Callable[[], None] = self._open_login
        self.open_share: Callable[[], None] = self._open_share
        self.api_factory: Callable[[], Any] = self._build_api
        # Set by app.py: the sidebar entry and the status-bar button follow it.
        self.on_auth_changed: Callable[[], None] | None = None

        self.stack = QStackedWidget(self)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.stack)
        self.stack.addWidget(self._build_signed_out())
        self.stack.addWidget(self._build_browser())

        # Download/import progress is posted from the worker thread; the drain
        # delivers it here on the main thread (never write the label directly).
        self._win.bus.subscribe("community/status", lambda message: self.set_page_status(str(message)))
        # Deliberately *not* loading here: building the page must not reach the
        # network or the token cache. The window calls refresh_auth_state()
        # once the shell is up, and again whenever the account changes.
        self.stack.setCurrentIndex(0)

    # ----- construction -------------------------------------------------------

    def _build_signed_out(self) -> QWidget:
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.addStretch(1)
        label = QLabel(SIGNED_OUT_TEXT, page)
        label.setObjectName("rowHint")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.sign_in_button = QPushButton("Sign in…", page)
        self.sign_in_button.setObjectName("action")
        self.sign_in_button.clicked.connect(lambda: self.open_login())
        row.addWidget(self.sign_in_button)
        row.addStretch(1)
        column.addLayout(row)
        column.addStretch(1)
        return page

    def _build_browser(self) -> QWidget:
        page = QWidget(self)
        column = QVBoxLayout(page)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(8)
        column.addLayout(self._build_filter_bar(page))
        column.addWidget(self._build_table(page), 1)
        self.hint_label = QLabel(SELECT_HINT, page)
        self.hint_label.setObjectName("rowHint")
        self.hint_label.setWordWrap(True)
        column.addWidget(self.hint_label)
        column.addLayout(self._build_action_row(page))
        self.status_label = QLabel("", page)
        self.status_label.setObjectName("mutedLabel")
        self.status_label.setWordWrap(True)
        column.addWidget(self.status_label)
        return page

    def _build_filter_bar(self, page: QWidget) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.cartridge_combo = QComboBox(page)
        self.cartridge_combo.setMinimumWidth(150)
        self.cartridge_combo.addItem(ALL_CARTRIDGES)
        self.cartridge_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.cartridge_combo)

        self.type_combo = QComboBox(page)
        self.type_combo.addItems(list(TYPE_FILTERS))
        self.type_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.type_combo)

        self.search_edit = QLineEdit(page)
        self.search_edit.setPlaceholderText("Search community models")
        self.search_edit.setClearButtonEnabled(True)
        # Enter (and the button) queries the server; typing only narrows what
        # is already on screen — every keystroke would otherwise be a request.
        self.search_edit.returnPressed.connect(self.refresh)
        self.search_edit.textChanged.connect(lambda _text: self.apply_live_filter())
        bar.addWidget(self.search_edit, 1)

        self.search_button = QPushButton("Search", page)
        self.search_button.setObjectName("action")
        self.search_button.clicked.connect(self.refresh)
        bar.addWidget(self.search_button)
        self.reload_button = QPushButton("Refresh", page)
        self.reload_button.clicked.connect(self.load_cartridges)
        bar.addWidget(self.reload_button)
        return bar

    def apply_live_filter(self) -> None:
        """Hide the rows that don't match what's typed — never drop them, so
        clearing the box brings the whole catalogue straight back."""
        needle = self.search_edit.text().strip().casefold()
        total = self.tree.topLevelItemCount()
        shown = 0
        for index in range(total):
            item = self.tree.topLevelItem(index)
            if item is None:
                continue
            match = not needle or any(needle in item.text(c).casefold() for c in LIVE_FILTER_COLUMNS)
            item.setHidden(not match)
            shown += int(match)
        if total:
            self.set_page_status(f"{shown} of {total} shown" if needle else f"{total} model(s) available")

    def _build_table(self, page: QWidget) -> QTreeWidget:
        self.tree = QTreeWidget(page)
        # Shares the models table's palette role — one table look in the app.
        self.tree.setObjectName("modelTable")
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        # Sorting is driven by hand, not `setSortingEnabled(True)`: that flag
        # re-sorts on every insert using whatever column/order the header
        # defaults to (column 0 descending), which would sort the table
        # before the user ever clicked anything — see models_page.py's
        # `_build_table` for the same reasoning.
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)
        self.tree.currentItemChanged.connect(lambda _cur, _prev: self._update_actions())
        return self.tree

    def _on_header_clicked(self, column: int) -> None:
        if self._sort_column == column:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column = column
            self._sort_order = Qt.SortOrder.AscendingOrder
        self.tree.header().setSortIndicator(self._sort_column, self._sort_order)
        self._apply_sort()

    def _apply_sort(self) -> None:
        if self._sort_column is not None:
            self.tree.sortItems(self._sort_column, self._sort_order)

    def _set_role(self, button: QPushButton, object_name: str) -> None:
        """Swap a button's palette role. QSS matches on the objectName, and
        a live widget has to be re-polished for the new rule to take."""
        if button.objectName() == object_name:
            return
        button.setObjectName(object_name)
        style = button.style()
        style.unpolish(button)
        style.polish(button)

    def _build_action_row(self, page: QWidget) -> QHBoxLayout:
        # Destructive alone on the far left, the state-driven primary on the
        # far right (JL, the Models bar's rule) — the two can't be neighbours.
        row = QHBoxLayout()
        self.remove_button = QPushButton(REMOVE_LABEL, page)
        self.remove_button.setObjectName("danger")
        self.remove_button.setToolTip(REMOVE_TOOLTIP)
        self.remove_button.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        # Sharing needs the Contribute role on the community server; the
        # button stays hidden until the role check answers, so a
        # non-contributor never sees an action they can't complete. Lives
        # here now (JL) — the identity/sign-out row above it was removed as
        # a duplicate of the status bar's own sign-out affordance.
        self.share_button = QPushButton("Share a model…", page)
        self.share_button.clicked.connect(lambda: self.open_share())
        self.share_button.setVisible(False)
        row.addWidget(self.share_button)
        self.download_button = QPushButton(ACTION_LABELS[STATE_DOWNLOAD], page)
        self.download_button.setObjectName("action")
        self.download_button.clicked.connect(self.download_selected)
        row.addWidget(self.download_button)
        return row

    # ----- auth ---------------------------------------------------------------

    def existing_auth_manager(self) -> Any | None:
        """The auth manager if one already exists — never constructs one.

        For read-only, display-only uses (the status bar's identity label)
        that must not risk touching MSAL when nothing is signed in yet. See
        ``auth_manager()`` for the constructing counterpart.
        """
        return getattr(self._win, "auth", None) or self._auth

    def is_signed_in(self) -> bool:
        """Whether there is an account — asked without ever creating a manager."""
        auth = self.existing_auth_manager()
        if auth is None:
            return False
        try:
            return bool(auth.is_authenticated())
        except Exception:
            return False

    def auth_manager(self) -> Any:
        """The shared ``AuthManager``, built on first use.

        Constructing one loads the MSAL token cache, so this is only ever
        reached from a user action (sign in, or a call that needs a token).
        The window's manager wins when it has one, and a manager built here is
        published back onto the window so both halves share a cache.
        """
        auth = getattr(self._win, "auth", None)
        if auth is not None:
            return auth
        if self._auth is None:
            from ..community.auth import AuthManager

            self._auth = AuthManager()
            try:
                self._win.auth = self._auth
            except Exception:
                # A window that won't take the attribute still gets a working
                # page; the manager just lives here instead.
                pass
        return self._auth

    def _build_api(self) -> Any:
        return CommunityApi(auth=self.auth_manager())

    def refresh_auth_state(self) -> None:
        """Show the browser or the sign-in panel, and load once when signed in."""
        signed_in = self.is_signed_in()
        self.stack.setCurrentIndex(1 if signed_in else 0)
        if not signed_in:
            self._loaded = False
            self.share_button.setVisible(False)
            self.can_contribute = False
            self.community_username = ""
            return
        if not self._loaded:
            self._loaded = True
            self.load_identity()
            self.load_cartridges()
            self.refresh()

    def _open_login(self) -> None:
        from .dialog_login import LoginDialog

        dialog = LoginDialog(self, auth=self.auth_manager())
        if dialog.exec():
            self.on_signed_in()

    def on_signed_in(self) -> None:
        """Post-sign-in: load the catalogue and let the window re-gate itself."""
        self._loaded = False
        self.refresh_auth_state()
        self._win.set_status("Signed in to the community.")
        if self.on_auth_changed is not None:
            self.on_auth_changed()

    def sign_out(self) -> None:
        if not self.confirm("Sign out", "Sign out of the community?"):
            return
        try:
            self.auth_manager().logout()
        except Exception as exc:
            self._win.notify("Sign-out failed", str(exc))
            return
        self._models = []
        self.tree.clear()
        self.refresh_auth_state()
        self._win.set_status("Signed out.")
        if self.on_auth_changed is not None:
            self.on_auth_changed()

    def load_identity(self) -> None:
        """Resolve the community handle and the Contribute role.

        ``get_user_metadata()`` always hits the network, so this runs on a
        worker. Display identity itself lives in the status bar now
        (app.py reads ``auth.identity()`` directly) — this page only needs
        the Contribute role (for the Share button) and the community handle.
        """

        def work() -> tuple[bool, str]:
            auth = self.auth_manager()
            name, email = auth.identity()
            try:
                meta = self.api_factory().get_user_metadata()
            except Exception:
                meta = None
            profile = (meta.profile_name if meta else "") or ""
            # The B2C token can carry no name claim; the community handle is
            # then the only human-readable thing to show beside the email.
            username = profile or name or (email.split("@")[0] if email else "")
            return bool(meta and meta.can_contribute()), username

        def done(payload: tuple[bool, str]) -> None:
            can_contribute, username = payload
            self.community_username = username
            self.can_contribute = can_contribute
            self.share_button.setVisible(can_contribute)

        def failed(_exc: Exception) -> None:
            self.can_contribute = False
            self.share_button.setVisible(False)

        self._win.run_worker(work, on_done=done, on_error=failed)

    # ----- catalogue ----------------------------------------------------------

    def load_cartridges(self) -> None:
        def done(cartridges: list[CartridgeInfo]) -> None:
            self._cartridges = list(cartridges)
            current = self.cartridge_combo.currentText()
            names = [ALL_CARTRIDGES] + [c.name for c in self._cartridges]
            blocked = self.cartridge_combo.blockSignals(True)
            self.cartridge_combo.clear()
            self.cartridge_combo.addItems(names)
            self.cartridge_combo.setCurrentText(current if current in names else ALL_CARTRIDGES)
            self.cartridge_combo.blockSignals(blocked)

        self._win.run_worker(
            lambda: self.api_factory().get_available_cartridges(),
            on_done=done,
            on_error=lambda exc: self.set_page_status(f"Could not load cartridges: {exc}"),
        )

    def refresh(self) -> None:
        """Re-query the catalogue for the current filters."""
        if not self.is_signed_in():
            return
        cartridge = self.cartridge_combo.currentText()
        cartridge = "" if cartridge in ("", ALL_CARTRIDGES) else cartridge
        model_type = TYPE_FILTERS.get(self.type_combo.currentText(), "")
        search = self.search_edit.text().strip()
        self.set_page_status("Loading…")

        def done(models: list[ModelInfo]) -> None:
            self._models = list(models)
            self._fill(self._models)
            self.set_page_status(
                f"{len(models)} model(s) available" if models else "No community models found for these filters."
            )

        self._win.run_worker(
            lambda: self.api_factory().get_models(search=search, model_type=model_type, cartridge=cartridge),
            on_done=done,
            on_error=lambda exc: self._on_list_failed(exc),
        )

    def _on_list_failed(self, exc: Exception) -> None:
        self._models = []
        self._fill([])
        self.set_page_status(f"Failed: {exc}")

    def _fill(self, models: list[ModelInfo]) -> None:
        selected = self.selected_uid()
        self.tree.clear()
        for info in models:
            state = self.installed_state(info)
            cartridge = info.cartridge_name or EMPTY_VALUE
            export_mode = INCLUDES_LABELS.get(info.export_mode, info.export_mode or EMPTY_VALUE)
            author = info.author or EMPTY_VALUE
            # Sort on the underlying date, not the locale-formatted text
            # displayed below — a locale format doesn't sort chronologically
            # in general (e.g. US "9/1/26" vs "12/1/25").
            published_key = formatting.parse(info.publish_date) or datetime.min
            item = _SortableItem(
                [
                    info.model_name,
                    cartridge,
                    f"v{info.model_version}",
                    export_mode,
                    str(info.headstamp_count),
                    str(info.image_count),
                    format_size(info.download_size),
                    format_date(info.publish_date),
                    author,
                    STATE_LABELS[state],
                ],
                [
                    info.model_name.casefold(),
                    cartridge.casefold(),
                    int(info.model_version),
                    export_mode.casefold(),
                    int(info.headstamp_count),
                    int(info.image_count),
                    int(info.download_size),
                    published_key,
                    author.casefold(),
                    _STATE_SORT_RANK[state],
                ],
                info.model_uid,
            )
            self.tree.addTopLevelItem(item)
            # The description is prose — too long for a column of its own (JL),
            # so it rides as the row's tooltip, on every cell rather than just
            # the name, and stays on the detail line under the table.
            if info.model_description:
                for index in range(len(COLUMNS)):
                    item.setToolTip(index, info.model_description)
        self._apply_sort()
        self._restore_selection(selected)
        self._autosize_columns()
        self.apply_live_filter()
        self._update_actions()

    def _autosize_columns(self) -> None:
        if self._columns_sized or self.tree.topLevelItemCount() == 0:
            return  # only once, and only before the user can have dragged them
        self._columns_sized = True
        for index in range(len(COLUMNS)):
            self.tree.resizeColumnToContents(index)
        self.tree.setColumnWidth(0, max(self.tree.columnWidth(0), 220))

    def _restore_selection(self, uid: str | None) -> None:
        items = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        rows = [item for item in items if item is not None]
        if not rows:
            return
        target = next((item for item in rows if uid and item.data(0, Qt.ItemDataRole.UserRole) == uid), rows[0])
        self.tree.setCurrentItem(target)

    def refresh_local_states(self) -> None:
        """Re-derive every row's State cell, and the bar, from the local
        library — no network, the catalogue listing itself is unchanged.

        What changes out from under the table is what's *installed*: a delete,
        import or update on the Models page (JL's lifecycle finding — a model
        deleted there kept offering removal here). Subscribed to
        ``models/changed``, which the Models page posts on every refresh.
        """
        state_column = COLUMNS.index("State")
        by_uid = {m.model_uid: m for m in self._models}
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item is None:
                continue
            info = by_uid.get(str(item.data(0, Qt.ItemDataRole.UserRole) or ""))
            if info is None:
                continue
            state = self.installed_state(info)
            item.setText(state_column, STATE_LABELS[state])
            if isinstance(item, _SortableItem) and len(item.sort_values) > state_column:
                item.sort_values[state_column] = _STATE_SORT_RANK[state]
        self._update_actions()

    def installed_state(self, info: ModelInfo) -> str:
        """Download / update / installed, decided by the local library.

        Matched on the community UID, so a model installed under a name the
        user changed is still recognized as the same model.
        """
        if not info.model_uid or self.db is None:
            return STATE_DOWNLOAD
        local = ModelRepo(self.db).find_by_community_uid(info.model_uid)
        if local is None:
            return STATE_DOWNLOAD
        if info.model_version > local.model_version:
            return STATE_UPDATE
        return STATE_INSTALLED

    # ----- selection ----------------------------------------------------------

    def selected_uid(self) -> str | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        uid = item.data(0, Qt.ItemDataRole.UserRole)
        return str(uid) if uid else None

    def selected_info(self) -> ModelInfo | None:
        uid = self.selected_uid()
        return next((m for m in self._models if m.model_uid == uid), None) if uid else None

    def _update_actions(self) -> None:
        """The bar and the detail line follow the selected row's state.

        Label, palette role and enabled state all come from
        ``installed_state`` — plus the queue, which is why a row already
        downloading or waiting says so instead of offering the click again.
        Download/update stays live during someone *else's* transfer: that is
        what queues (JL). Only removal waits for the imports to end.
        """
        info = self.selected_info()
        state = self.installed_state(info) if info is not None else STATE_DOWNLOAD
        uid = info.model_uid if info is not None else ""
        queue = [q[0].model_uid for q in self._download_queue]

        if info is not None and uid == self._active_download_uid:
            self.download_button.setText(DOWNLOADING_LABEL)
            self.download_button.setToolTip(DOWNLOADING_LABEL)
            self.download_button.setEnabled(False)
            self._set_role(self.download_button, "update")
        else:
            self.download_button.setText(ACTION_LABELS[state])
            if uid in queue:
                position = queue.index(uid) + 1
                self.download_button.setToolTip(f"Queued (#{position}) — starts after the current download")
            else:
                self.download_button.setToolTip(ACTION_TOOLTIPS[state])
            self.download_button.setEnabled(info is not None and state != STATE_INSTALLED and uid not in queue)
            # Blue for an update, green for a download — the theme's rule for
            # "replace something installed" vs "primary, go" (ui/theme.py).
            self._set_role(self.download_button, "update" if state == STATE_UPDATE else "action")

        self.remove_button.setEnabled(info is not None and state == STATE_INSTALLED and not self._busy)
        self.hint_label.setText(self._hint_for(info))

    def _hint_for(self, info: ModelInfo | None) -> str:
        if info is None:
            return SELECT_HINT
        if info.model_description:
            return info.model_description
        return f"{info.model_name} — no description published."

    def set_page_status(self, message: str) -> None:
        self.status_label.setText(str(message))

    def _post_progress(self, message: str) -> None:
        """Status for the window bar and the in-page label, from any thread."""
        self._win.bus.post("status", message)
        self._win.bus.post("community/status", message)

    # ----- download -----------------------------------------------------------

    def download_selected(self) -> None:
        """The bar's primary. No busy guard: a click during someone else's
        transfer is what queues (``_enqueue_download``), and ``download``
        itself refuses a row that is already installed and current."""
        info = self.selected_info()
        if info is not None:
            self.download(info)

    def remove_selected(self) -> None:
        """The bar's Remove, live only for an installed, current selection."""
        info = self.selected_info()
        if info is None or self._busy:  # no deletes while an import is in flight
            return
        if self.installed_state(info) == STATE_INSTALLED:
            self.remove_installed(info)

    def remove_installed(self, info: ModelInfo) -> None:
        """Delete the locally installed copy of a community model.

        Mirrors ``ModelsPage.delete_selected``: the repo owns the rules (the
        active model and the last model in a cartridge refuse) and this only
        reports them; the model's whole directory goes on a worker. The
        community listing is untouched — the row flips back to Download.
        """
        if self.db is None:
            return
        local = ModelRepo(self.db).find_by_community_uid(info.model_uid)
        if local is None:
            return
        name = local.name or info.model_name
        if not self.confirm(
            "Remove installed model?",
            f"Delete the installed copy of '{name}' and all associated headstamps "
            "and training images? The community version stays available for download.",
        ):
            return
        try:
            with self.db.transaction():
                ModelRepo(self.db).delete(local.id)
        except ValueError as exc:
            self._win.notify("Cannot delete", str(exc))
            return
        directory = paths.model_dir(local.id)
        if directory.exists():
            self._win.run_worker(lambda: shutil.rmtree(directory, ignore_errors=True))
        models_page = getattr(self._win, "models_page", None)
        if models_page is not None:
            models_page.refresh()  # posts models/changed → our State cells follow
        else:
            self.refresh_local_states()
        self._post_progress(f"Removed installed copy of {name}.")

    def download(self, info: ModelInfo) -> None:
        name = info.model_name or info.model_uid or "model"
        state = self.installed_state(info)
        if state == STATE_INSTALLED:
            return
        # An update refreshes the installed row in place (import_model matches
        # on the community UID), keeping its slot assignments and sorting
        # templates. Decided here, on the UI thread, and used for the messaging.
        if state == STATE_UPDATE:
            choice = self.ask_download_choice(name)
            if choice not in (STATE_UPDATE, "copy"):
                return
            update_existing = choice == STATE_UPDATE
        else:
            if not self.confirm(
                DOWNLOAD_SECURITY_TITLE,
                f'"{name}" is a community-published model. Models are loaded with PyTorch and can execute '
                "code embedded in the file, so only download models from authors you trust.\n\n"
                "Download and import this model?",
            ):
                return
            update_existing = True

        is_update = state == STATE_UPDATE and update_existing
        self._enqueue_download(info, name, update_existing, is_update)

    def _enqueue_download(self, info: ModelInfo, name: str, update_existing: bool, is_update: bool) -> None:
        """Start now, or queue behind the running download (JL: a second click
        must visibly queue, not vanish). Prompts were already answered."""
        if self._active_download_uid == info.model_uid or any(
            q[0].model_uid == info.model_uid for q in self._download_queue
        ):
            return
        self._batch_total += 1
        if self._busy:
            self._download_queue.append((info, name, update_existing, is_update))
            self._post_progress(f"Queued {name} ({len(self._download_queue)} waiting).")
            self._update_actions()
            return
        self._start_download(info, name, update_existing, is_update)

    def _start_download(self, info: ModelInfo, name: str, update_existing: bool, is_update: bool) -> None:
        self._batch_done += 1
        self._active_download_uid = info.model_uid
        self._set_busy(True)
        self._post_progress(f"{self._batch_prefix()}Downloading {name}…")
        self._win.run_worker(
            lambda: self._download_and_import(info, name, update_existing=update_existing),
            on_done=lambda result: self._on_imported(result, info, name, is_update=is_update),
            on_error=lambda exc: self._on_download_failed(exc),
        )

    def _batch_prefix(self) -> str:
        """ "(2 of 3) " once a queue exists; silent for a single download.

        A bare parenthetical, because the message it prefixes already leads
        with its own verb — "Downloading 1 of 2: Downloading X" read wrong
        (JL live-testing).
        """
        if self._batch_total <= 1:
            return ""
        return f"({self._batch_done} of {self._batch_total}) "

    def _finish_download(self) -> None:
        """One transfer ended (either way): start the next or stand down."""
        self._active_download_uid = None
        if self._download_queue:
            self._start_download(*self._download_queue.pop(0))
            return
        self._batch_total = 0
        self._batch_done = 0
        self._set_busy(False)

    def find_catalogue_entry(self, uid: str, api: Any | None = None) -> ModelInfo | None:
        """The catalogue row for ``uid``, from the last search or the server.

        Blocking when it has to ask the server (no per-model endpoint exists —
        ``GetModels`` unfiltered is the whole catalogue), so callers run it on a
        worker. Used by the Sort page's model-update prompt, which starts from
        a UID and needs the row's version, size, author and publish date.
        """
        cached = next((m for m in self._models if m.model_uid == uid), None)
        if cached is not None:
            return cached
        models = (api or self.api_factory()).get_models()
        return next((m for m in models if m.model_uid == uid), None)

    def start_update(self, info: ModelInfo) -> None:
        """Install ``info`` through the catalogue's own download path.

        The entry point for the Sort page's model-update prompt: same
        ``download()`` the table's action button runs, so there is one
        download+import path and one set of prompts, not two.
        """
        self.download(info)

    def _download_and_import(self, info: ModelInfo, name: str, *, update_existing: bool) -> tuple[int, int]:
        """Worker body: SAS URL → ZIP → ``import_model``. Never touches a widget."""
        api = self.api_factory()
        payload = api.request_download(info.model_uid)
        url = payload.get("FullUrl") or payload.get("fullUrl")
        if not url:
            raise RuntimeError("Server did not return a download URL")
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / f"{info.model_uid}.zip"
            api.download_to(
                url,
                zip_path,
                expected_total=info.download_size or None,
                progress=self._download_progress(name),
            )
            self._post_progress(f"Importing {name}…")
            # community_download=True marks the install as the publisher's,
            # which is what keeps Train off it (CLAUDE.md §5).
            result = import_model(
                zip_path,
                db=self.db,
                community_download=True,
                update_existing=update_existing,
                progress=self._import_progress(name),
            )
        self._record_installed_version(result[1], info)
        return result

    def _download_progress(self, name: str) -> Callable[[int, int | None], None]:
        """Throttle to whole percents — ``download_to`` fires per 64 KB chunk."""
        last = [-1]

        def report(done: int, total: int | None) -> None:
            if total and total > 0:
                pct = int(done * 100 / total)
                if pct == last[0]:
                    return
                last[0] = pct
                self._post_progress(
                    f"{self._batch_prefix()}Downloading {name}: {pct}% "
                    f"({done / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB)"
                )
                return
            megabytes = done / (1024 * 1024)
            if int(megabytes) != last[0]:
                last[0] = int(megabytes)
                self._post_progress(f"{self._batch_prefix()}Downloading {name}: {megabytes:.1f} MB")

        return report

    def _import_progress(self, name: str) -> Callable[[int, int], None]:
        last = [-1]

        def report(step: int, total: int) -> None:
            if total <= 0:
                return
            pct = int(step * 100 / total)
            if pct != last[0]:
                last[0] = pct
                self._post_progress(f"{self._batch_prefix()}Importing {name}: {pct}% ({step} / {total} files)")

        return report

    def _record_installed_version(self, model_id: int, info: ModelInfo) -> None:
        """Stamp the catalogue's version onto the freshly-installed model.

        The row's state compares the server's ``model_version`` against the
        local one, and the archive's own manifest can lag what the catalogue
        advertises — which would leave a just-installed model still reading
        "Update available". The bytes came from this entry, so its version is
        the authoritative one.

        Runs on the download worker; DB access is serialized by the Database lock.
        """
        if self.db is None:
            return
        repo = ModelRepo(self.db)
        model = repo.get(model_id)
        if model is None or int(info.model_version) <= int(model.model_version):
            return
        model.model_version = int(info.model_version)
        repo.update(model)

    def _on_imported(self, result: tuple[int, int], info: ModelInfo, name: str, *, is_update: bool) -> None:
        self._finish_download()
        _cartridge_id, model_id = result
        self._post_progress(f"Updated {name} to v{info.model_version}." if is_update else f"Imported {name}.")
        self._notify_import(model_id, updated=is_update)
        models_page = getattr(self._win, "models_page", None)
        if models_page is not None:
            models_page.refresh()
        self.refresh()

    def _notify_import(self, model_id: int, *, updated: bool) -> None:
        """Post-install messaging, verbatim from the Tk tab."""
        if updated:
            self._win.notify(
                "Update complete",
                "The installed model was updated in place — your slot assignments and sorting templates were kept.",
            )
            return
        model = ModelRepo(self.db).get(model_id) if self.db is not None else None
        if model is not None and model.feedback_loop_enabled and model.community_model_uid:
            self._win.notify(
                "Feedback loop enabled",
                "The feedback loop has been enabled for this model, which may include automatic upload of images "
                f"below the {model.feedback_loop_confidence_floor}% confidence threshold for the model owner "
                "to review.\n\nTo change the upload mode or opt out, open the model on the Models page, click "
                "Edit, and adjust the Community feedback loop settings.",
            )
            return
        self._win.notify("Download complete", "Model imported.")

    def _on_download_failed(self, exc: Exception) -> None:
        self._post_progress(f"Download failed: {exc}")
        self._win.notify("Download failed", str(exc))
        # After the messaging: a failure must not strand the queued rest.
        self._finish_download()

    def _set_busy(self, busy: bool) -> None:
        """One archive at a time — a second import would race the same tree."""
        self._busy = busy
        self._update_actions()

    # ----- share --------------------------------------------------------------

    def _open_share(self) -> None:
        from .dialog_share_model import ShareModelDialog

        if self.db is None or not ModelRepo(self.db).list():
            self._win.notify("No models to share", "Create and train a local model on the Models page first.")
            return
        dialog = ShareModelDialog(
            self,
            db=self.db,
            api_factory=self.api_factory,
            username=self.community_username,
        )
        dialog.exec()
        if dialog.shared_uid:
            models_page = getattr(self._win, "models_page", None)
            if models_page is not None:
                models_page.refresh()
            self.refresh()

    # ----- dialog hooks -------------------------------------------------------

    def _ask(self, title: str, text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        # Defaults to No: the notice is about running someone else's code.
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _ask_download_choice(self, name: str) -> str:
        """Update in place, install a separate copy, or cancel.

        Tk only offers update-or-cancel here; ``model_io`` has always been able
        to keep both, and the Models page already exposes that choice on a
        plain ZIP import. The security notice rides along rather than opening a
        second box in front of this one.
        """
        box = QMessageBox(self)
        box.setWindowTitle(UPDATE_TITLE)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f'"{name}" is already installed, and a newer version has been published.')
        box.setInformativeText(
            "Community models are loaded with PyTorch and can execute code embedded in the file, so only "
            "install models from authors you trust.\n\nUpdate the installed model in place — its slot "
            "assignments and sorting templates are kept — or install a separate copy."
        )
        update = box.addButton("Update installed", QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton("Install as copy", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(update)
        box.exec()
        clicked = box.clickedButton()
        if clicked is update:
            return STATE_UPDATE
        if clicked is copy:
            return "copy"
        return "cancel"


def build_community_page(win: Any) -> CommunityPage:
    return CommunityPage(win)
