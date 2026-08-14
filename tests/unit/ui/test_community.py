"""The Community activity, sign-in and share dialogs, offscreen.

No network and no MSAL anywhere: the seams are ``page.api_factory`` (a fake
``CommunityApi`` with canned catalogue rows) and the auth object the page reads
off the window (a fake ``AuthManager``). Building the page must not construct a
real one, and one test pins exactly that.

The download and share tests are end-to-end on purpose. The fake API hands back
a **real** model ZIP built by ``model_io.export_model`` from a seeded model, and
the page runs the actual ``import_model`` — so ownership (``CommunityManaged``,
``is_trainable`` False), in-place updates and the version stamp are asserted
against the real persistence layer, not a mock of it. The share path likewise
packages a real archive and records what would have been uploaded.

``CASESORTER_DATA_DIR`` is redirected autouse: imports write model directories
and shares write scratch archives, so an unset data root would have these tests
rummaging in the developer's own library.
"""

from __future__ import annotations

import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from sorter import paths
from sorter.community.auth import AuthError, PortInUseError
from sorter.community.community_api import CartridgeInfo, ModelInfo, UserMetaData
from sorter.data.model_io import ExportMode, export_model
from sorter.data.models import Model, is_trainable
from sorter.data.repository import CartridgeRepo, HeadstampRepo, ModelRepo
from sorter.ui.community_page import (
    ACTION_LABELS,
    ACTION_TOOLTIPS,
    ALL_CARTRIDGES,
    COLUMNS,
    DOWNLOADING_LABEL,
    REMOVE_LABEL,
    STATE_DOWNLOAD,
    STATE_INSTALLED,
    STATE_LABELS,
    STATE_UPDATE,
    build_community_page,
    format_date,
    format_size,
)
from sorter.ui.dialog_login import LoginDialog
from sorter.ui.dialog_share_model import (
    MISSING_NAME_TITLE,
    NO_CHECKPOINT_TITLE,
    ShareModelDialog,
)

from .conftest import drain_until


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    assert paths.app_data_dir() == tmp_path / "data"


# ----- fakes -------------------------------------------------------------------


class _Recorder:
    """Stand-in for ``notify`` — a modal would hang the offscreen run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.calls.append((title, text))

    @property
    def titles(self) -> list[str]:
        return [title for title, _text in self.calls]


class FakeAuth:
    """The slice of ``AuthManager`` the community surface actually uses."""

    def __init__(
        self,
        *,
        signed_in: bool = True,
        name: str | None = "Ada Lovelace",
        email: str | None = "ada@example.com",
        login_error: Exception | None = None,
    ) -> None:
        self.signed_in = signed_in
        self.name = name
        self.email = email
        self.login_error = login_error
        self.logins = 0
        self.logouts = 0

    def is_authenticated(self) -> bool:
        return self.signed_in

    def identity(self) -> tuple[str | None, str | None]:
        return self.name, self.email

    def login_interactive(self) -> str:
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error
        self.signed_in = True
        return "token"

    def logout(self) -> None:
        self.logouts += 1
        self.signed_in = False


class FakeApi:
    """Canned catalogue + a real ZIP on the wire, recording every call."""

    def __init__(
        self,
        *,
        models: list[ModelInfo] | None = None,
        cartridges: list[CartridgeInfo] | None = None,
        metadata: UserMetaData | None = None,
        archive: Path | None = None,
        list_error: Exception | None = None,
        share_error: Exception | None = None,
        server_uid: str | None = None,
    ) -> None:
        self.models = list(models or [])
        self.cartridges = list(cartridges or [])
        self.metadata = metadata
        self.archive = archive
        self.list_error = list_error
        self.share_error = share_error
        self.server_uid = server_uid
        self.queries: list[tuple[str, str, str]] = []
        self.download_requests: list[str] = []
        self.shares: list[dict[str, Any]] = []

    def get_user_metadata(self) -> UserMetaData | None:
        return self.metadata

    def get_available_cartridges(self) -> list[CartridgeInfo]:
        return list(self.cartridges)

    def get_models(self, search: str = "", model_type: str = "", cartridge: str = "") -> list[ModelInfo]:
        self.queries.append((search, model_type, cartridge))
        if self.list_error is not None:
            raise self.list_error
        return list(self.models)

    def request_download(self, model_uid: str) -> dict[str, Any]:
        self.download_requests.append(model_uid)
        return {"FullUrl": f"https://example.invalid/{model_uid}.zip"}

    def download_to(self, url: str, dest: Any, *, expected_total: Any = None, progress: Any = None) -> Path:
        assert self.archive is not None, "this test needs an archive on the fake API"
        shutil.copyfile(self.archive, dest)
        size = Path(dest).stat().st_size
        if progress is not None:
            progress(size // 2, size)
            progress(size, size)
        return Path(dest)

    def share_model(
        self, *, zip_path: Any, manifest_path: Any, model_info: dict[str, Any], progress: Any = None
    ) -> str:
        with zipfile.ZipFile(zip_path) as zf:
            entries = sorted(zf.namelist())
        self.shares.append(
            {
                "model_info": dict(model_info),
                "entries": entries,
                "manifest_exists": Path(manifest_path).exists(),
            }
        )
        if self.share_error is not None:
            raise self.share_error
        if progress is not None:
            progress(1, 2)
            progress(2, 2)
        return self.server_uid or str(model_info.get("CommunityModelUID") or "")


def info(
    uid: str = "uid-1",
    *,
    name: str = "Range brass",
    version: int = 1,
    cartridge: str = "9mm",
    description: str = "",
    size: int = 2048,
    published: str = "2026-03-04T10:00:00",
    author: str = "publisher",
    headstamps: int = 3,
    images: int = 12,
    export_mode: str = "ModelAndImages",
) -> ModelInfo:
    return ModelInfo(
        model_uid=uid,
        model_name=name,
        cartridge_name=cartridge,
        cartridge_id=1,
        model_version=version,
        model_description=description,
        author=author,
        publish_date=published,
        image_count=images,
        headstamp_count=headstamps,
        download_size=size,
        export_mode=export_mode,
    )


# ----- fixtures / helpers ------------------------------------------------------


@pytest.fixture
def api() -> FakeApi:
    return FakeApi(
        models=[info()],
        cartridges=[CartridgeInfo(id=1, name="9mm"), CartridgeInfo(id=2, name=".223")],
        metadata=UserMetaData(profile_name="ada", roles=1),
    )


def build_page(window: Any, api: FakeApi, *, auth: FakeAuth | None = None) -> Any:
    """A community page on this window, with every seam replaced."""
    window.auth = auth
    window.notify = _Recorder()
    page = build_community_page(window)
    page.api_factory = lambda: api
    page.confirm = lambda _title, _text: pytest.fail("unexpected confirmation")
    page.ask_download_choice = lambda _name: pytest.fail("unexpected update prompt")
    page.open_login = lambda: pytest.fail("unexpected login dialog")
    page.open_share = lambda: pytest.fail("unexpected share dialog")
    return page


@pytest.fixture
def page(window, api):
    """Signed in, catalogue loaded."""
    built = build_page(window, api, auth=FakeAuth())
    built.refresh_auth_state()
    assert drain_until(window, lambda: built.tree.topLevelItemCount() > 0), "the catalogue never loaded"
    return built


def cell(page: Any, row: int, column: str) -> str:
    return page.tree.topLevelItem(row).text(COLUMNS.index(column))


def names(page: Any) -> list[str]:
    return [cell(page, row, "Model") for row in range(page.tree.topLevelItemCount())]


def visible_names(page: Any) -> list[str]:
    """The rows the live filter left on screen, in view order."""
    return [
        cell(page, row, "Model")
        for row in range(page.tree.topLevelItemCount())
        if not page.tree.topLevelItem(row).isHidden()
    ]


def select_row(page: Any, index: int) -> None:
    item = page.tree.topLevelItem(index)
    assert item is not None, f"no row {index}"
    page.tree.setCurrentItem(item)


def primary_for(page: Any, index: int) -> Any:
    """The bar's primary button, with row ``index`` selected.

    Everything about it — label, palette role, enabled state — is scoped to
    the selection, so selecting is part of reading it.
    """
    select_row(page, index)
    return page.download_button


def remove_for(page: Any, index: int) -> Any:
    """The bar's Remove button, with row ``index`` selected."""
    select_row(page, index)
    return page.remove_button


def pump(qapp: Any, predicate: Any, timeout_s: float = 10.0) -> bool:
    """Pump the Qt loop until ``predicate`` holds — the QThread dialogs' drain."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    for _ in range(5):  # flush signals queued right before the worker finished
        qapp.processEvents()
    return predicate()


def make_model(config: Any, name: str, **fields: Any) -> Model:
    cartridge = CartridgeRepo(config.db).list()[0]
    return ModelRepo(config.db).create(Model(name=name, cartridge_id=cartridge.id, **fields))


def seed_exportable(config: Any, name: str, **fields: Any) -> Model:
    """A model with a headstamp, a training image and a checkpoint on disk."""
    model = make_model(config, name, **fields)
    HeadstampRepo(config.db).add(model.id, "9mm FC", 1)
    images = paths.model_images_dir(model.id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "9mm FC__1.jpg").write_bytes(b"jpeg-ish")
    trained = paths.model_trained_dir(model.id)
    trained.mkdir(parents=True, exist_ok=True)
    checkpoint = trained / f"{model.id}.pth"
    checkpoint.write_bytes(b"not-really-a-checkpoint")
    model.model_path = str(checkpoint)
    ModelRepo(config.db).update(model)
    fresh = ModelRepo(config.db).get(model.id)
    assert fresh is not None
    return fresh


def write_archive(config: Any, dest: Path, model: Model, **overrides: Any) -> Path:
    """A real community-shaped ZIP for ``model``, as a publisher would ship it."""
    cartridge = CartridgeRepo(config.db).get(model.cartridge_id)
    assert cartridge is not None
    published = Model(**{**model.__dict__, **overrides})
    return export_model(
        dest,
        published,
        cartridge.name,
        HeadstampRepo(config.db).list_for_model(model.id),
        mode=ExportMode.MODEL_AND_IMAGES,
        model_file=Path(model.model_path) if model.model_path else None,
        images_dir=paths.model_images_dir(model.id),
    )


def download(page: Any, window: Any, *, row: int = 0) -> None:
    """Click Download on a row and wait for the import to land."""
    select_row(page, row)
    before = len(window.notify.calls)
    page.download_selected()
    assert drain_until(window, lambda: len(window.notify.calls) > before), "the download worker never finished"


# ----- signed-out state --------------------------------------------------------


def test_signed_out_shows_the_sign_in_panel(window, api) -> None:
    page = build_page(window, api, auth=None)

    assert page.stack.currentIndex() == 0
    assert not page.is_signed_in()
    # Nothing was asked of the network, and no manager exists to ask with.
    assert api.queries == []
    assert page._auth is None


def test_building_the_page_never_constructs_an_auth_manager(window, api, monkeypatch) -> None:
    built: list[Any] = []

    class _Manager:
        def __init__(self) -> None:
            built.append(self)

        def is_authenticated(self) -> bool:
            return False

    monkeypatch.setattr("sorter.community.auth.AuthManager", _Manager)
    page = build_page(window, api, auth=None)

    assert built == []

    # Only a user action reaches for one — and the window then shares it.
    manager = page.auth_manager()

    assert built == [manager]
    assert window.auth is manager


def test_a_signed_out_page_does_not_query_the_catalogue(window, api) -> None:
    page = build_page(window, api, auth=FakeAuth(signed_in=False))

    page.refresh()

    assert api.queries == []
    assert page.stack.currentIndex() == 0


def test_signing_in_switches_the_page_and_tells_the_window(window, api) -> None:
    auth = FakeAuth(signed_in=False)
    page = build_page(window, api, auth=auth)
    changed: list[int] = []
    page.on_auth_changed = lambda: changed.append(1)

    auth.signed_in = True
    page.on_signed_in()

    assert page.stack.currentIndex() == 1
    assert changed == [1]
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)


# ----- the catalogue -----------------------------------------------------------


def test_the_list_carries_the_facts_the_tk_cards_showed(window, api, monkeypatch) -> None:
    from PySide6.QtCore import QLocale

    from sorter.ui import formatting

    monkeypatch.setattr(formatting, "_locale", lambda: QLocale("sv_SE"))
    api.models = [info(description="Mixed range pickup", size=5 * 1024 * 1024)]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)

    assert cell(page, 0, "Model") == "Range brass"
    assert cell(page, 0, "Cartridge") == "9mm"
    assert cell(page, 0, "Version") == "v1"
    # Human-readable, not the WinForms enum name (JL: the type filter's
    # counterpart must be visible in the table).
    assert cell(page, 0, "Includes") == "Model + images"
    assert cell(page, 0, "Headstamps") == "3"
    assert cell(page, 0, "Images") == "12"
    assert cell(page, 0, "Size") == "5.00 MB"
    # Published date renders in the OS's regional format (ui.formatting),
    # pinned to sv_SE here rather than the CI machine's real locale.
    assert cell(page, 0, "Published") == "2026-03-04"
    assert cell(page, 0, "Author") == "publisher"
    assert cell(page, 0, "State") == STATE_LABELS[STATE_DOWNLOAD]
    # The description follows the selection rather than repeating per row.
    select_row(page, 0)
    assert page.hint_label.text() == "Mixed range pickup"


def test_size_and_date_formatting_degrade_gracefully(monkeypatch) -> None:
    from PySide6.QtCore import QLocale

    from sorter.ui import formatting

    monkeypatch.setattr(formatting, "_locale", lambda: QLocale("sv_SE"))
    assert format_size(0) == "—"
    assert format_size(900) == "900 B"
    assert format_size(1536) == "1.50 KB"
    # ``format_date`` is now a thin wrapper over ``ui.formatting`` — see
    # that module's own tests for the locale-formatting behavior itself.
    assert format_date("") == "—"
    assert format_date("not a date") == "—"
    assert format_date("2026-01-09") == "2026-01-09"


# ----- column sorting -----------------------------------------------------------


def click_header(page: Any, column_name: str) -> None:
    """A genuine mouse click on the header section, not ``.emit()`` on the
    signal — this is what proves click *delivery* (geometry, clickability)
    actually reaches the handler the way a real user's click does, not just
    that the handler is correct once invoked. See models_page's test module
    for the full offscreen-quirk rationale this mirrors: the page here is a
    standalone top-level widget in these tests (never inserted into a real
    window's ``QStackedWidget``), so showing it directly is enough to make
    its header paintable — no page navigation needed, unlike Models.
    """
    if not page.isVisible():
        page.resize(1000, 400)
        page.show()
        QTest.qWait(0)
    header = page.tree.header()
    column = COLUMNS.index(column_name)

    def click_column(index: int) -> None:
        x = header.sectionViewportPosition(index) + header.sectionSize(index) // 2
        QTest.mouseClick(
            header.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(x, header.height() // 2),
        )

    prev_column, prev_order = page._sort_column, page._sort_order
    if column == prev_column:
        expected_order = (
            Qt.SortOrder.DescendingOrder if prev_order == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder
        )
    else:
        expected_order = Qt.SortOrder.AscendingOrder

    attempts = 6
    for _attempt in range(attempts):
        click_column(column)
        if page._sort_column == column and page._sort_order == expected_order:
            return
        click_column(column - 1 if column else min(column + 1, len(COLUMNS) - 1))
    raise AssertionError(f"header click on {column_name!r} never registered after {attempts} attempts")


def synthetic_order(page: Any, wanted: tuple[str, ...]) -> list[str]:
    """The relative order of just the given model names among all rows."""
    shown = names(page)
    return [n for n in shown if n in wanted]


def test_images_column_sorts_numerically_not_lexically(window, api) -> None:
    # Lexical order would read "10" < "2" < "3"; numeric order must not.
    api.models = [
        info(uid="ten", name="Ten", images=10),
        info(uid="two", name="Two", images=2),
        info(uid="three", name="Three", images=3),
    ]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 3)

    click_header(page, "Images")

    assert [cell(page, row, "Images") for row in range(3)] == ["2", "3", "10"]


def test_published_column_sorts_chronologically_not_by_locale_text(window, api) -> None:
    # The displayed text is locale-formatted (ui.formatting), which
    # doesn't sort chronologically in general — the typed sort key must be
    # the underlying date instead.
    api.models = [
        info(uid="newer", name="Newer", published="2026-08-10T09:00:00"),
        info(uid="older", name="Older", published="2025-01-05T09:00:00"),
    ]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)

    click_header(page, "Published")

    assert names(page) == ["Older", "Newer"]


def test_state_column_sorts_by_actionability_not_alphabetically(window, api, config) -> None:
    # Alphabetizing the labels ("Available", "Installed", "Update available")
    # would not read as a sane order — "needs action" belongs first.
    installed = info(uid="already-installed", name="Already installed")
    ModelRepo(config.db).create(
        Model(
            name="Already installed",
            cartridge_id=CartridgeRepo(config.db).list()[0].id,
            community_model_uid="already-installed",
            model_version=installed.model_version,
        )
    )
    api.models = [installed, info(uid="brand-new", name="Brand new")]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)

    click_header(page, "State")

    assert names(page) == ["Brand new", "Already installed"]


def test_every_column_sorts_and_toggles_on_a_real_header_click(window, api) -> None:
    """EVERY column, via genuine mouse clicks (not signal.emit()): a first
    click sorts ascending, a second click on the same header reverses it.

    Three rows, each column given genuinely distinct values, so a column
    that silently didn't sort can't hide behind ties or coincidental order.
    """
    api.models = [
        info(
            uid="bravo",
            name="Bravo",
            cartridge=".223",
            version=5,
            export_mode="ModelOnly",
            headstamps=2,
            images=5,
            size=1000,
            published="2025-06-01T08:00:00",
            author="zoe",
        ),
        info(
            uid="alpha",
            name="Alpha",
            cartridge="22-250",
            version=20,
            export_mode="ImagesOnly",
            headstamps=9,
            images=20,
            size=2_000_000,
            published="2026-01-01T08:00:00",
            author="amy",
        ),
        info(
            uid="charlie",
            name="Charlie",
            cartridge="6.5 CM",
            version=1,
            export_mode="ModelAndImages",
            headstamps=1,
            images=1,
            size=500,
            published="2024-01-01T08:00:00",
            author="mo",
        ),
    ]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 3)

    wanted = ("Bravo", "Alpha", "Charlie")
    for column_name in (
        "Model",
        "Cartridge",
        "Version",
        "Includes",
        "Headstamps",
        "Images",
        "Size",
        "Published",
        "Author",
    ):
        click_header(page, column_name)
        ascending = synthetic_order(page, wanted)
        assert len(ascending) == 3, f"{column_name!r}: a synthetic row went missing after sorting"

        click_header(page, column_name)
        descending = synthetic_order(page, wanted)
        assert descending == list(reversed(ascending)), (
            f"{column_name!r} didn't reverse on a second (real) click: {ascending} -> {descending}"
        )


def test_default_order_is_unchanged_until_a_header_is_clicked(window, api) -> None:
    api.models = [info(uid="z", name="Zed"), info(uid="a", name="Alpha")]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)

    # The server's own result order stands — no sort has been requested yet.
    assert names(page) == ["Zed", "Alpha"]


def test_selection_survives_a_header_click_sort(window, api) -> None:
    api.models = [info(uid="z", name="Zed"), info(uid="a", name="Alpha")]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)
    select_row(page, 0)
    assert page.selected_uid() == "z"

    click_header(page, "Model")

    assert page.selected_uid() == "z"


def test_column_resizing_stays_interactive_after_sorting_is_wired(page) -> None:
    from PySide6.QtWidgets import QHeaderView

    header = page.tree.header()
    click_header(page, "Model")

    for column in range(len(COLUMNS)):
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive


def test_the_filters_are_what_the_server_is_asked_for(page, window, api) -> None:
    def last_query_is(expected: tuple[str, str, str]) -> bool:
        return drain_until(window, lambda: bool(api.queries) and api.queries[-1] == expected)

    api.queries.clear()
    page.type_combo.setCurrentText("Model only")
    assert last_query_is(("", "ModelOnly", "")), api.queries

    page.cartridge_combo.setCurrentText(".223")
    assert last_query_is(("", "ModelOnly", ".223")), api.queries

    # Typing is not a query — every keystroke would be a request. Enter (or
    # the Search button, same slot) is what asks.
    page.search_edit.setText("  federal  ")
    assert api.queries[-1] == ("", "ModelOnly", ".223")

    page.refresh()
    assert last_query_is(("federal", "ModelOnly", ".223")), api.queries


def test_typing_narrows_the_loaded_rows_without_re_querying(window, api) -> None:
    api.models = [
        info("uid-1", name="Range brass", cartridge="9mm", author="ada"),
        info("uid-2", name="Match prep", cartridge=".223", author="publisher"),
        info("uid-3", name="Mixed pickup", cartridge="45 ACP", author="ada"),
    ]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 3)
    api.queries.clear()

    page.search_edit.setText("MATCH")  # case-insensitive, on the name

    assert visible_names(page) == ["Match prep"]
    assert page.status_label.text() == "1 of 3 shown"
    assert api.queries == []  # nothing left the machine

    page.search_edit.setText("ada")  # the author counts too
    assert visible_names(page) == ["Range brass", "Mixed pickup"]

    page.search_edit.setText("9mm")  # and the cartridge
    assert visible_names(page) == ["Range brass"]

    # Hidden, never dropped: clearing the box brings the catalogue straight back.
    page.search_edit.clear()
    assert visible_names(page) == ["Range brass", "Match prep", "Mixed pickup"]
    assert page.tree.topLevelItemCount() == 3
    assert page.status_label.text() == "3 model(s) available"


def test_the_filter_survives_a_re_fill(page, window, api) -> None:
    api.models = [info("uid-1", name="Range brass"), info("uid-2", name="Match prep")]
    page.search_edit.setText("range")
    page.refresh()

    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)
    # The server saw the term as well, so the count line is the plain one; the
    # rows the fill rebuilt are still filtered by what is in the box.
    assert visible_names(page) == ["Range brass"]


def test_the_filter_reload_button_says_what_it_does(page) -> None:
    assert page.reload_button.text() == "Refresh"


def test_the_cartridge_filter_comes_from_the_server(page) -> None:
    values = [page.cartridge_combo.itemText(i) for i in range(page.cartridge_combo.count())]

    assert values == [ALL_CARTRIDGES, "9mm", ".223"]


def test_a_failed_query_reports_and_empties_the_table(window, api) -> None:
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)
    api.list_error = RuntimeError("GetModels → 503")

    page.refresh()

    assert drain_until(window, lambda: page.status_label.text().startswith("Failed:"))
    assert page.tree.topLevelItemCount() == 0


def test_an_empty_catalogue_says_so(window, api) -> None:
    api.models = []
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()

    assert drain_until(window, lambda: page.status_label.text().startswith("No community models"))


# ----- installed state ---------------------------------------------------------


def test_the_bar_is_dead_with_nothing_selected(window, api) -> None:
    api.models = []
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.status_label.text().startswith("No community models"))

    assert page.selected_uid() is None
    assert not page.download_button.isEnabled()
    assert not page.remove_button.isEnabled()


def test_the_state_column_and_action_follow_the_local_library(window, api, config) -> None:
    make_model(config, "Installed one", community_model_uid="uid-installed", model_version=2)
    make_model(config, "Older one", community_model_uid="uid-old", model_version=1)
    api.models = [
        info("uid-new", name="Not installed", version=1),
        info("uid-installed", name="Installed one", version=2),
        info("uid-old", name="Older one", version=3),
    ]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 3)

    assert [cell(page, row, "State") for row in range(3)] == [
        STATE_LABELS[STATE_DOWNLOAD],
        STATE_LABELS[STATE_INSTALLED],
        STATE_LABELS[STATE_UPDATE],
    ]

    # The bar follows the selection: label, enabled state and palette role all
    # come from the selected row's state.
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_DOWNLOAD]
    assert primary_for(page, 0).isEnabled()
    assert primary_for(page, 0).objectName() == "action"
    assert primary_for(page, 0).toolTip() == ACTION_TOOLTIPS[STATE_DOWNLOAD]
    assert not remove_for(page, 0).isEnabled()

    # Installed-and-current leaves the primary dead and hands the action to
    # Remove (JL: the loop is install -> update -> remove), in destructive red.
    assert primary_for(page, 1).text() == ACTION_LABELS[STATE_INSTALLED]
    assert not primary_for(page, 1).isEnabled()
    assert remove_for(page, 1).isEnabled()
    assert remove_for(page, 1).text() == REMOVE_LABEL
    assert remove_for(page, 1).objectName() == "danger"

    assert primary_for(page, 2).text() == ACTION_LABELS[STATE_UPDATE]
    assert primary_for(page, 2).isEnabled()
    # Blue, not the download green: this replaces something already installed.
    assert primary_for(page, 2).objectName() == "update"
    # An update is not a removal — the local copy is being replaced, not dropped.
    assert not remove_for(page, 2).isEnabled()


def test_the_bar_removes_the_installed_copy(window, api, config) -> None:
    model = make_model(config, "Installed one", community_model_uid="uid-installed", model_version=2)
    make_model(config, "Keeper")  # a second model, so the cartridge keeps one
    api.models = [info("uid-installed", name="Installed one", version=2)]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)
    asked: list[str] = []
    page.confirm = lambda title, _text: asked.append(title) or True

    remove_for(page, 0).click()
    assert drain_until(window, lambda: ModelRepo(config.db).get(model.id) is None)

    assert asked == ["Remove installed model?"]
    # The catalogue row survives and flips back to a plain download.
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_DOWNLOAD]
    assert primary_for(page, 0).objectName() == "action"
    assert not remove_for(page, 0).isEnabled()


def test_removing_the_active_model_is_refused(window, api, config) -> None:
    from sorter.data.repository import SettingsRepo

    model = make_model(config, "Installed one", community_model_uid="uid-installed", model_version=2)
    SettingsRepo(config.db).set_active_model_id(model.id)
    api.models = [info("uid-installed", name="Installed one", version=2)]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)
    page.confirm = lambda _title, _text: True
    notified = _Recorder()
    window.notify = notified

    remove_for(page, 0).click()

    assert drain_until(window, lambda: notified.titles == ["Cannot delete"])
    assert ModelRepo(config.db).get(model.id) is not None  # still installed


# ----- the install lifecycle, cross-page (JL) ---------------------------------
#
# The row's State cell and the bar must follow "what is installed" wherever it
# changes — the Models page included, which posts models/changed on refresh.


def test_deleting_on_the_models_page_flips_the_community_row(window, api, config) -> None:
    model = make_model(config, "Installed one", community_model_uid="uid-installed", model_version=2)
    make_model(config, "Keeper")  # the cartridge keeps a model, so delete is legal
    api.models = [info("uid-installed", name="Installed one", version=2)]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_INSTALLED]

    with config.db.transaction():
        ModelRepo(config.db).delete(model.id)
    window.models_page.refresh()  # any Models-page mutation ends in a refresh
    window.bus.drain()

    assert cell(page, 0, "State") == STATE_LABELS[STATE_DOWNLOAD]
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_DOWNLOAD]
    assert primary_for(page, 0).objectName() == "action"
    assert not remove_for(page, 0).isEnabled()


def test_an_install_appearing_flips_the_community_row(window, api, config) -> None:
    api.models = [info("uid-x", name="Range brass", version=1)]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_DOWNLOAD]

    make_model(config, "Range brass", community_model_uid="uid-x", model_version=1)
    window.models_page.refresh()
    window.bus.drain()

    assert cell(page, 0, "State") == STATE_LABELS[STATE_INSTALLED]
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_INSTALLED]
    assert not primary_for(page, 0).isEnabled()
    assert remove_for(page, 0).isEnabled()


def test_an_older_install_appearing_flips_the_row_to_update(window, api, config) -> None:
    api.models = [info("uid-x", name="Range brass", version=3)]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)

    make_model(config, "Range brass", community_model_uid="uid-x", model_version=1)
    window.models_page.refresh()
    window.bus.drain()

    assert cell(page, 0, "State") == STATE_LABELS[STATE_UPDATE]
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_UPDATE]
    assert primary_for(page, 0).objectName() == "update"


def test_the_primary_installs_the_selected_row(window, api) -> None:
    api.models = [info(uid="first", name="First"), info(uid="second", name="Second")]
    page = build_page(window, api, auth=FakeAuth())
    started: list[str] = []
    page.download = lambda entry: started.append(entry.model_uid)
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)

    select_row(page, 1)
    page.download_button.click()

    assert started == ["second"]


def test_the_bar_runs_the_real_download_path(page, window, api, config, tmp_path) -> None:
    source = seed_exportable(config, "Source")
    api.archive = write_archive(config, tmp_path / "m.zip", source, name="Range brass")
    page.confirm = lambda _title, _text: True

    primary_for(page, 0).click()

    assert drain_until(window, lambda: window.notify.titles == ["Download complete"])
    assert api.download_requests == ["uid-1"]
    assert any(m.model_type == "CommunityManaged" for m in ModelRepo(config.db).list())


def test_a_second_download_queues_and_both_install(page, window, api, config, tmp_path) -> None:
    """JL: clicking a second model mid-download must visibly queue, not vanish.

    Both rows install from one FakeApi archive; the queue is observed through
    the status stream ("Queued", then the "2 of 2" prefix on the second) and
    through the bar, which says where the selected row sits in the queue.
    """
    source = seed_exportable(config, "Source")
    api.archive = write_archive(config, tmp_path / "m.zip", source, name="Range brass")
    api.models = [info("uid-a", name="First"), info("uid-b", name="Second")]
    page.refresh()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)
    page.confirm = lambda _title, _text: True
    statuses: list[str] = []
    window.bus.subscribe("community/status", statuses.append)

    primary_for(page, 0).click()
    primary_for(page, 1).click()  # while the first is (at least logically) in flight

    # Row 1 is selected and queued behind row 0: the bar says so rather than
    # offering the click again, and row 0 says it is the one downloading.
    assert not page.download_button.isEnabled()
    assert page.download_button.toolTip().startswith("Queued (#1)")
    assert primary_for(page, 0).text() == DOWNLOADING_LABEL
    assert not primary_for(page, 0).isEnabled()

    assert drain_until(window, lambda: len(api.download_requests) == 2)
    assert drain_until(window, lambda: not page._busy)
    assert api.download_requests == ["uid-a", "uid-b"]  # queue order preserved
    assert any(s.startswith("Queued Second") for s in statuses)
    assert any(s.startswith("(2 of 2) Downloading") for s in statuses)
    # Stand-down after the batch: counters reset, the bar live again.
    assert page._download_queue == [] and page._active_download_uid is None
    assert primary_for(page, 0).isEnabled()


def test_a_failed_download_does_not_strand_the_queue(page, window, api, config, tmp_path) -> None:
    source = seed_exportable(config, "Source")
    api.archive = write_archive(config, tmp_path / "m.zip", source, name="Range brass")
    api.models = [info("uid-a", name="First"), info("uid-b", name="Second")]
    page.refresh()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)
    page.confirm = lambda _title, _text: True
    # First request explodes; the second must still run.
    real_request = api.request_download
    api.request_download = lambda uid: (
        (_ for _ in ()).throw(RuntimeError("boom")) if uid == "uid-a" else real_request(uid)
    )

    primary_for(page, 0).click()
    primary_for(page, 1).click()

    assert drain_until(window, lambda: "uid-b" in api.download_requests)
    assert drain_until(window, lambda: not page._busy)
    assert "Download failed" in window.notify.titles


def test_the_bar_follows_the_selection_through_a_header_click_sort(window, api, config) -> None:
    make_model(config, "Zed", community_model_uid="z", model_version=1)
    api.models = [info(uid="z", name="Zed"), info(uid="a", name="Alpha")]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)
    select_row(page, 0)
    assert page.selected_uid() == "z"

    click_header(page, "Model")

    assert names(page) == ["Alpha", "Zed"]
    # The selection survives the sort, so the bar still describes the same
    # model — the installed one, whatever row it has moved to.
    assert page.selected_uid() == "z"
    assert page.download_button.text() == ACTION_LABELS[STATE_INSTALLED]
    assert page.remove_button.isEnabled()
    assert primary_for(page, 0).text() == ACTION_LABELS[STATE_DOWNLOAD]


def test_the_description_is_the_rows_tooltip_on_every_column(window, api) -> None:
    # Prose doesn't fit a column (JL), so hovering anywhere on the row shows
    # it — and the selected-row detail line still carries it too.
    api.models = [info(description="Mixed range pickup"), info(uid="bare", name="Bare")]
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 2)

    described = page.tree.topLevelItem(0)
    assert [described.toolTip(i) for i in range(len(COLUMNS))] == ["Mixed range pickup"] * len(COLUMNS)
    assert all(page.tree.topLevelItem(1).toolTip(i) == "" for i in range(len(COLUMNS)))
    select_row(page, 0)
    assert page.hint_label.text() == "Mixed range pickup"


# ----- share gating, sign-out ---------------------------------------------------
# Identity display moved to the status bar (JL): the page used to carry its own
# "Signed in as ... [Sign out]" row, duplicating the status bar's Sign out
# button — see test_chrome.py for the status-bar identity_label coverage.


def test_read_only_role_never_shows_the_share_button(window, api) -> None:
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()

    assert drain_until(window, lambda: page.community_username != "")
    assert not page.can_contribute
    assert page.share_button.isHidden()


def test_a_contributor_gets_the_share_button(window, api) -> None:
    api.metadata = UserMetaData(profile_name="ada", roles=3)
    page = build_page(window, api, auth=FakeAuth())
    page.refresh_auth_state()

    assert drain_until(window, lambda: page.can_contribute)
    assert not page.share_button.isHidden()
    assert page.community_username == "ada"


def test_the_community_handle_stands_in_for_a_missing_name(window, api) -> None:
    api.metadata = UserMetaData(profile_name="ada", roles=3)
    page = build_page(window, api, auth=FakeAuth(name=None))
    page.refresh_auth_state()

    assert drain_until(window, lambda: page.community_username == "ada")


def test_sign_out_needs_confirmation(page, window) -> None:
    auth = window.auth
    page.confirm = lambda _title, _text: False

    page.sign_out()

    assert auth.logouts == 0
    assert page.stack.currentIndex() == 1


def test_sign_out_clears_the_page_and_tells_the_window(page, window) -> None:
    auth = window.auth
    changed: list[int] = []
    page.on_auth_changed = lambda: changed.append(1)
    page.confirm = lambda _title, _text: True

    page.sign_out()

    assert auth.logouts == 1
    assert page.stack.currentIndex() == 0
    assert page.tree.topLevelItemCount() == 0
    assert changed == [1]


# ----- download (end to end, real archives) ------------------------------------


def test_the_security_notice_can_refuse_a_download(page, window, api, config, tmp_path) -> None:
    api.archive = write_archive(config, tmp_path / "m.zip", seed_exportable(config, "Source"))
    page.confirm = lambda _title, _text: False
    select_row(page, 0)

    page.download_selected()

    assert api.download_requests == []
    assert [m.name for m in ModelRepo(config.db).list()] == ["Default", "Source"]


def test_a_download_installs_the_publishers_model(page, window, api, config, tmp_path) -> None:
    source = seed_exportable(config, "Source")
    api.archive = write_archive(config, tmp_path / "m.zip", source, name="Range brass")
    page.confirm = lambda _title, _text: True

    download(page, window)

    assert api.download_requests == ["uid-1"]
    installed = next(m for m in ModelRepo(config.db).list() if m.name == "Range brass")
    # Ownership is decided by how it got here (CLAUDE.md §5): the checkpoint is
    # the publisher's, so the Train activity must stay off it.
    assert installed.model_type == "CommunityManaged"
    assert not is_trainable(installed)
    assert installed.model_path is not None and Path(installed.model_path).exists()
    assert [h.name for h in HeadstampRepo(config.db).list_for_model(installed.id)] == ["9mm FC"]
    assert window.notify.titles == ["Download complete"]


def test_a_download_of_a_feedback_model_explains_the_loop(page, window, api, config, tmp_path) -> None:
    source = seed_exportable(config, "Source")
    api.archive = write_archive(
        config,
        tmp_path / "m.zip",
        source,
        name="Loop model",
        community_model_uid="uid-1",
        feedback_loop_enabled=True,
        feedback_loop_confidence_floor=80,
    )
    page.confirm = lambda _title, _text: True

    download(page, window)

    assert window.notify.titles == ["Feedback loop enabled"]
    assert "80%" in window.notify.calls[0][1]


def test_the_catalogue_version_is_recorded_over_the_archives(page, window, api, config, tmp_path) -> None:
    # The manifest can lag what the catalogue advertises; without recording the
    # catalogue's version a just-installed model still reads "Update available".
    source = seed_exportable(config, "Source")
    api.models = [info("uid-7", name="Range brass", version=5)]
    api.archive = write_archive(config, tmp_path / "m.zip", source, name="Range brass", community_model_uid="uid-7")
    page.refresh()
    assert drain_until(window, lambda: page.tree.topLevelItemCount() == 1)
    page.confirm = lambda _title, _text: True

    download(page, window)

    installed = next(m for m in ModelRepo(config.db).list() if m.community_model_uid == "uid-7")
    assert installed.model_version == 5
    assert drain_until(window, lambda: cell(page, 0, "State") == STATE_LABELS[STATE_INSTALLED])


def test_an_update_refreshes_the_installed_row_in_place(page, window, api, config, tmp_path) -> None:
    installed = seed_exportable(config, "My copy", community_model_uid="uid-9", model_version=1)
    api.models = [info("uid-9", name="Range brass", version=2)]
    api.archive = write_archive(config, tmp_path / "m.zip", installed, name="Range brass")
    page.refresh()
    assert drain_until(window, lambda: cell(page, 0, "State") == STATE_LABELS[STATE_UPDATE])
    page.ask_download_choice = lambda _name: STATE_UPDATE

    download(page, window)

    rows = [m for m in ModelRepo(config.db).list() if m.community_model_uid == "uid-9"]
    assert [m.id for m in rows] == [installed.id], "the update added a row instead of refreshing one"
    assert rows[0].name == "My copy", "the update overwrote the name the user gave it"
    assert rows[0].model_version == 2
    assert window.notify.titles == ["Update complete"]


def test_an_update_can_install_a_separate_copy(page, window, api, config, tmp_path) -> None:
    installed = seed_exportable(config, "My copy", community_model_uid="uid-9", model_version=1)
    api.models = [info("uid-9", name="Range brass", version=2)]
    api.archive = write_archive(config, tmp_path / "m.zip", installed, name="Range brass")
    page.refresh()
    assert drain_until(window, lambda: cell(page, 0, "State") == STATE_LABELS[STATE_UPDATE])
    page.ask_download_choice = lambda _name: "copy"

    download(page, window)

    rows = sorted(
        (m for m in ModelRepo(config.db).list() if m.community_model_uid == "uid-9"),
        key=lambda m: m.id,
    )
    assert [m.name for m in rows] == ["My copy", "Range brass"]
    assert rows[0].model_type == "Standard", "the local copy lost its ownership"
    assert rows[1].model_type == "CommunityManaged"


def test_declining_the_update_prompt_downloads_nothing(page, window, api, config, tmp_path) -> None:
    installed = seed_exportable(config, "My copy", community_model_uid="uid-9", model_version=1)
    api.models = [info("uid-9", name="Range brass", version=2)]
    api.archive = write_archive(config, tmp_path / "m.zip", installed, name="Range brass")
    page.refresh()
    assert drain_until(window, lambda: cell(page, 0, "State") == STATE_LABELS[STATE_UPDATE])
    page.ask_download_choice = lambda _name: "cancel"

    page.download_selected()

    assert api.download_requests == []


def test_a_failed_download_reports_instead_of_raising(page, window, api, config) -> None:
    api.archive = None  # download_to asserts, standing in for a transport error
    page.confirm = lambda _title, _text: True
    select_row(page, 0)

    page.download_selected()

    assert drain_until(window, lambda: window.notify.titles == ["Download failed"])
    assert page.download_button.isEnabled(), "the page stayed busy after a failure"


# ----- sign-in dialog ----------------------------------------------------------


@pytest.fixture
def login(window):
    def _make(auth: FakeAuth) -> tuple[Any, _Recorder]:
        dialog = LoginDialog(window, auth=auth)
        recorder = _Recorder()
        dialog.notify = recorder
        return dialog, recorder

    return _make


def test_sign_in_runs_on_a_worker_and_accepts(qapp, login) -> None:
    auth = FakeAuth(signed_in=False)
    dialog, notified = login(auth)

    dialog.sign_in()

    assert pump(qapp, lambda: dialog.auth_result is not None), "the sign-in worker never finished"
    assert auth.logins == 1
    assert auth.is_authenticated()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert notified.calls == [], "success must not open a modal on top of the visible state change"


def test_a_bound_redirect_port_is_reported_specifically(qapp, login) -> None:
    dialog, notified = login(
        FakeAuth(signed_in=False, login_error=PortInUseError("Local redirect port 44300 is in use."))
    )

    dialog.sign_in()

    assert pump(qapp, lambda: notified.calls != []), "the sign-in worker never finished"
    assert notified.titles == ["Port in use"]
    assert "Close any other instance" in notified.calls[0][1]
    assert dialog.result() != QDialog.DialogCode.Accepted
    # Retryable: the button comes back rather than leaving a dead dialog.
    assert dialog.sign_in_button.isEnabled()


def test_an_auth_error_shows_its_message_and_anything_else_its_repr(qapp, login) -> None:
    dialog, notified = login(FakeAuth(signed_in=False, login_error=AuthError("Login failed: no access_token")))
    dialog.sign_in()
    assert pump(qapp, lambda: notified.calls != [])

    assert notified.calls[0] == ("Sign-in failed", "Login failed: no access_token")

    other, other_notified = login(FakeAuth(signed_in=False, login_error=ValueError("boom")))
    other.sign_in()
    assert pump(qapp, lambda: other_notified.calls != [])

    assert other_notified.calls[0] == ("Sign-in failed", "ValueError('boom')")


def test_the_auth_manager_can_be_built_lazily_by_the_dialog(qapp, window) -> None:
    auth = FakeAuth(signed_in=False)
    dialog = LoginDialog(window, auth_factory=lambda: auth)
    dialog.notify = _Recorder()

    dialog.sign_in()

    assert pump(qapp, lambda: dialog.auth_result is not None)
    assert auth.logins == 1


# ----- share dialog ------------------------------------------------------------


@pytest.fixture
def share(window, config, api, qapp):
    dialogs: list[Any] = []

    def _make(**kwargs: Any) -> tuple[Any, _Recorder]:
        dialog = ShareModelDialog(window, db=config.db, api_factory=lambda: api, username="ada", **kwargs)
        recorder = _Recorder()
        dialog.notify = recorder
        dialog.confirm = lambda _title, _text: True
        dialogs.append(dialog)
        # The cartridge list loads on a worker; let it land before the form is driven.
        assert pump(qapp, lambda: dialog.cartridge_combo.count() > 0)
        return dialog, recorder

    yield _make
    for dialog in dialogs:
        dialog.close()


def select_model(dialog: Any, name: str) -> None:
    dialog.model_combo.setCurrentIndex([m.name for m in dialog._models].index(name))


def test_the_form_defaults_to_the_models_own_name_and_cartridge(share, config) -> None:
    seed_exportable(config, "Range brass")
    dialog, _notified = share()
    select_model(dialog, "Range brass")

    assert dialog.name_edit.text() == "Range brass"
    assert dialog.cartridge_combo.currentText() == "9mm"


def test_share_refuses_an_empty_name(share, config, api) -> None:
    seed_exportable(config, "Range brass")
    dialog, notified = share()
    select_model(dialog, "Range brass")
    dialog.name_edit.setText("   ")

    dialog.share()

    assert notified.titles == [MISSING_NAME_TITLE]
    assert api.shares == []


def test_share_refuses_to_publish_a_model_with_no_checkpoint(share, config, api) -> None:
    make_model(config, "Untrained")
    dialog, notified = share()
    select_model(dialog, "Untrained")

    dialog.share()

    assert notified.titles == [NO_CHECKPOINT_TITLE]
    assert api.shares == []


def test_images_only_can_be_shared_without_a_checkpoint(share, config, api, qapp) -> None:
    model = make_model(config, "Untrained")
    images = paths.model_images_dir(model.id)
    images.mkdir(parents=True, exist_ok=True)
    (images / "9mm FC__1.jpg").write_bytes(b"jpeg-ish")
    dialog, notified = share()
    select_model(dialog, "Untrained")
    dialog.export_combo.setCurrentText("Images only")

    dialog.share()

    # Wait on the notify, not api.shares: the fake api records from the worker
    # thread, but the DB write and the notify happen in on_done on the next
    # drain — a pump that exits on api.shares races them (seen on the slow
    # Windows CI runner).
    assert pump(qapp, lambda: notified.titles != []), "the share worker never finished"
    assert notified.titles == ["Model shared"]
    assert api.shares[0]["model_info"]["ModelExportMode"] == 2
    assert "images/9mm FC__1.jpg" in api.shares[0]["entries"]


def test_sharing_uploads_a_real_archive_and_stamps_the_uid(share, config, api, qapp) -> None:
    model = seed_exportable(config, "Range brass")
    dialog, notified = share()
    select_model(dialog, "Range brass")
    dialog.description_edit.setPlainText("Mixed range pickup")
    dialog.fb_enabled_check.setChecked(True)
    dialog.fb_floor_spin.setValue(90)

    dialog.share()

    assert pump(qapp, lambda: notified.titles != []), "the share worker never finished"
    payload = api.shares[0]
    assert payload["manifest_exists"], "the standalone manifest was not uploaded alongside the ZIP"
    assert "manifest.json" in payload["entries"]
    assert f"model/{model.id}.pth" in payload["entries"]
    sent = payload["model_info"]
    assert sent["ModelName"] == "Range brass"
    assert sent["ModelDescription"] == "Mixed range pickup"
    assert sent["Author"] == "ada"
    assert sent["CartridgeId"] == 1
    assert (sent["FeedbackLoopEnabled"], sent["FeedbackLoopConfidenceFloor"]) == (True, 90)
    assert sent["HeadstampCount"] == 1
    assert sent["ModelVersion"] == model.model_version

    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None
    assert stored.community_model_uid == sent["CommunityModelUID"]
    assert notified.titles == ["Model shared"]


def test_sharing_your_own_model_does_not_make_it_foreign(share, config, api, qapp) -> None:
    # A community UID means "exists in the community", not "isn't yours"
    # (CLAUDE.md §5) — the publisher's own copy stays trainable.
    model = seed_exportable(config, "Range brass")
    dialog, notified = share()
    select_model(dialog, "Range brass")

    dialog.share()
    assert pump(qapp, lambda: notified.titles != [])

    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None
    assert stored.community_model_uid
    assert stored.model_type == "Standard"
    assert is_trainable(stored)


def test_resharing_keeps_the_uid_and_bumps_the_version(share, config, api, qapp) -> None:
    model = seed_exportable(config, "Range brass", community_model_uid="uid-mine", model_version=4)
    dialog, notified = share()
    select_model(dialog, "Range brass")

    dialog.share()
    assert pump(qapp, lambda: notified.titles != [])

    assert api.shares[0]["model_info"]["CommunityModelUID"] == "uid-mine"
    assert api.shares[0]["model_info"]["ModelVersion"] == 5
    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None and stored.model_version == 5


def test_the_server_can_rename_the_uid(share, config, api, qapp) -> None:
    model = seed_exportable(config, "Range brass")
    api.server_uid = "uid-from-server"
    dialog, notified = share()
    select_model(dialog, "Range brass")

    dialog.share()
    assert pump(qapp, lambda: notified.titles != [])

    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None and stored.community_model_uid == "uid-from-server"


def test_a_failed_share_reports_and_leaves_the_model_untouched(share, config, api, qapp) -> None:
    model = seed_exportable(config, "Range brass")
    api.share_error = RuntimeError("FileUploadRequest → HTTP 403")
    dialog, notified = share()
    select_model(dialog, "Range brass")

    dialog.share()

    assert pump(qapp, lambda: notified.calls != []), "the share worker never finished"
    assert notified.titles == ["Share failed"]
    assert dialog.status_label.text().startswith("Share failed")
    assert dialog.share_button.isEnabled(), "the dialog stayed busy after a failure"
    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None and not stored.community_model_uid
