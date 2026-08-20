"""The Qt shell, built and driven headless — no display, no Xvfb, no hardware.

This is the requirement-4 proof from docs/ui-modernization.md: real widgets on
the offscreen platform plugin, with the event bus doing the cross-thread work.
Fixtures live in conftest.py; the Sort dashboard's own behavior is in
test_sort.py.
"""

from __future__ import annotations

import types

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QPixmap


def test_sidebar_activities(window) -> None:
    # Two groups: the always-live surfaces, then the mode pair (Train first —
    # the flagship local-model path, AI Config the alternative backend).
    assert list(window.sidebar_buttons) == [
        "Sort",
        "Models",
        "Community",
        "Train",
        "AI Config",
        "Settings",
    ]
    assert window.sidebar_buttons["Sort"].isChecked()
    assert window.pages.currentWidget() is window._pages_by_name["Sort"]


def test_separators_split_the_sidebar_into_three_groups(window) -> None:
    """The lines are what make the sidebar grouped rather than one list:
    live surfaces / the mode pair / Settings."""
    # Community is auth-gated, and a hidden widget drops out of the layout —
    # sign in so the group above the first line is actually laid out.
    window.community_page.is_signed_in = lambda: True
    window._apply_auth_visibility()
    sidebar = window.sidebar_buttons["Sort"].parentWidget()
    sidebar.layout().activate()

    mode_line = window.sidebar_separator
    settings_line = window.sidebar_settings_separator

    # Both palette-driven through the same objectName, via theme.py.
    assert mode_line.objectName() == "sidebarSeparator"
    assert settings_line.objectName() == "sidebarSeparator"

    assert window.sidebar_buttons["Community"].geometry().bottom() <= mode_line.y()
    assert mode_line.geometry().bottom() <= window.sidebar_buttons["Train"].y()
    assert window.sidebar_buttons["AI Config"].geometry().bottom() <= settings_line.y()
    assert settings_line.geometry().bottom() <= window.sidebar_buttons["Settings"].y()


def test_settings_stays_reachable_when_the_window_is_short(window, qapp) -> None:
    """Pinned under a stretch it fell off-screen on a short window (issue #105).
    In the flow it follows AI Config, so it is on-screen whenever the entries
    above it are."""
    sidebar = window.sidebar_buttons["Sort"].parentWidget()
    settings = window.sidebar_buttons["Settings"]
    ai_config = window.sidebar_buttons["AI Config"]

    # Short enough that a bottom-pinned entry would be pushed past the edge:
    # the entries plus both hairlines, with nothing to spare.
    window.resize(window.width(), settings.sizeHint().height() * len(window.sidebar_buttons) + 40)
    qapp.processEvents()
    sidebar.layout().activate()

    assert settings.geometry().bottom() - ai_config.geometry().bottom() <= settings.height() * 2
    assert settings.geometry().bottom() <= sidebar.height()


@pytest.mark.parametrize("name", ["Train", "AI Config", "Models", "Community", "Settings", "Sort"])
def test_sidebar_switches_pages(window, name: str) -> None:
    window.sidebar_buttons[name].click()

    assert window.pages.currentWidget() is window._pages_by_name[name]
    assert window.sidebar_buttons[name].isChecked()


def test_a_muted_activity_is_inked_apart_from_its_neighbours(window, config) -> None:
    """The stylesheet can't reach a QIcon, so the property and the icon ink
    are set together (``_set_activity_unavailable``) and must not drift.

    Compared as rendered pixels, not by identity: a QIcon built from the same
    SVG is a different object every time.
    """
    from sorter.ui.app import SIDEBAR_ICON_SIZE
    from sorter.ui.icons import TRAIN
    from sorter.ui.icons import icon as vector_icon
    from sorter.ui.theme import unavailable_ink

    def rendered(color: str):
        return vector_icon(TRAIN, color, SIDEBAR_ICON_SIZE).pixmap(SIDEBAR_ICON_SIZE).toImage()

    train = window.sidebar_buttons["Train"]
    inked = train.icon().pixmap(SIDEBAR_ICON_SIZE).toImage()

    assert train.property("unavailable") is True
    # Blended toward the window, not merely text_subtle: JL read a
    # text_subtle AI Config as still available.
    assert inked == rendered(unavailable_ink(window.palette_colors))
    assert inked != rendered(window.palette_colors["text_muted"])

    # And the muting is undone, not merely applied, when the mode allows it.
    from .conftest import seed_model

    seed_model(config, {"9mm FC": 1})
    window.bus.post("mode/changed", None)
    window.bus.drain()

    assert train.property("unavailable") is False
    assert train.icon().pixmap(SIDEBAR_ICON_SIZE).toImage() == rendered(window.palette_colors["text_muted"])


def test_sidebar_fits_the_widest_label(window) -> None:
    # Regression: a fixed 84 px sidebar clipped "Community" on wider fonts.
    sidebar = window.sidebar_buttons["Sort"].parentWidget()
    widest = max(sidebar.fontMetrics().horizontalAdvance(n) for n in window.sidebar_buttons)

    assert sidebar.minimumWidth() >= widest + 24


def test_sort_actions_are_disabled_without_a_board(window) -> None:
    # Dict order follows the strip: Start/Stop last = the green primary at
    # the far right (JL), matching the Training strip.
    assert list(window.action_buttons) == ["Manual feed", "Start/Stop"]
    assert not any(button.isEnabled() for button in window.action_buttons.values())
    # The idle face, so the row reads "Start" even when nothing can start.
    assert window.run_button.text() == "Start"
    assert window.run_button.objectName() == "action"


def test_serial_dock_logs_traffic(window) -> None:
    assert not window.serial_dock.isClosed()

    window.bus.post("serial/rx", "ok")
    window.bus.drain()

    assert "<- ok" in window.serial_monitor.output.toPlainText()


def test_menus(window) -> None:
    assert [a.text().replace("&", "") for a in window.menuBar().actions()] == [
        "File",
        "View",
        "Help",
    ]
    # window.menus, not action.menu(): the latter hands back a Python-owned
    # wrapper and destroys the C++ menu when the temporary is collected.
    assert [a.text() for a in window.menus["View"].actions() if a.text()] == [
        "Serial Monitor",
        "Classification History",
        "User Guide panel",
        "Themes",
        "Re-dock panels",
    ]
    assert window.serial_dock.toggleViewAction() in window.menus["View"].actions()
    assert window.history_dock.toggleViewAction() in window.menus["View"].actions()
    assert window.help_dock.toggleViewAction() in window.menus["View"].actions()
    assert window.themes_dock.toggleViewAction() in window.menus["View"].actions()
    help_texts = [a.text() for a in window.menus["Help"].actions() if a.text()]
    assert help_texts == [
        "User Guide",
        "Check for updates…",
        "Export support package…",
        "About",
        "License",
    ]


def test_theme_section_hosts_the_theme_combo(window) -> None:
    # By name, not by row: Theme stopped being the last section when "Import
    # from Windows" was added, and this test is about the section's content.
    window._open_settings_section("Theme")

    assert window.settings_list.currentItem().text() == "Theme"
    page = window.settings_pages.currentWidget()
    assert window.theme_combo.parentWidget() is page


def test_the_edit_theme_button_opens_the_editor(window, monkeypatch) -> None:
    import sorter.ui.dialog_theme_editor as dialog_theme_editor

    opened = []

    class FakeDialog:
        def __init__(self, parent, win) -> None:
            opened.append(win)

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(dialog_theme_editor, "ThemeEditorDialog", FakeDialog)
    assert window.theme_edit_button.parentWidget() is window.theme_combo.parentWidget()

    window.theme_edit_button.click()

    assert opened == [window]


def test_run_worker_replies_are_one_shot_and_unsubscribed(window) -> None:
    """Regression (this suite's first CI run): reply topics were keyed on id(fn) and
    never unsubscribed, so a later worker whose fn recycled the address
    delivered its result to the earlier worker's callback too — a cartridge
    list arriving in a username handler.
    """
    import time

    results: list[object] = []
    window.run_worker(lambda: "first", on_done=results.append)
    deadline = time.monotonic() + 5
    while results != ["first"] and time.monotonic() < deadline:
        window.bus.drain()
    assert results == ["first"]

    worker_topics = [t for t, handlers in window.bus._subs.items() if t.startswith("worker/") and handlers]
    assert worker_topics == []  # delivery must tear the subscription down


def test_theme_switch_restyles_the_window(window) -> None:
    window.theme_combo.setCurrentText("Dark")
    dark = window.styleSheet()

    window.theme_combo.setCurrentText("Light")

    assert window.styleSheet() != dark
    assert window.theme_name == "Light"


def test_the_history_dock_carries_its_full_name(window) -> None:
    # Seth: "it just says History" — the dock and its View toggle must agree.
    assert window.history_dock.windowTitle() == "Classification History"


def test_panels_are_managed_by_qtads_and_carry_the_drag_hint(window) -> None:
    """JL couldn't drop History next to the Serial Monitor with stock Qt docks.
    QtAds owns the docking now: every panel is registered with the one dock
    manager, and the hint rides the tab — which is the drag handle here."""
    import PySide6QtAds as ads

    assert isinstance(window.dock_manager, ads.CDockManager)
    # The manager is the window's central widget; the pages are ITS central area.
    assert window.centralWidget() is window.dock_manager
    assert window.central_dock.isCentralWidget()

    registered = window.dock_manager.dockWidgetsMap()
    for dock in (window.serial_dock, window.history_dock, window.help_dock, window.themes_dock):
        assert registered[dock.objectName()] is dock
        assert "Re-dock panels" in dock.tabWidget().toolTip()


def test_only_the_serial_panel_is_open_at_startup(window) -> None:
    # QtAds tracks closed-ness itself: isHidden() is False even for a closed
    # panel, so isClosed() is the only honest question to ask.
    assert not window.serial_dock.isClosed()
    assert window.history_dock.isClosed()
    assert window.help_dock.isClosed()
    assert window.themes_dock.isClosed()


def test_redock_panels_brings_a_floating_dock_home(window) -> None:
    """Seth floated the history panel and couldn't drag it back — the View
    menu's Re-dock action must always work, no dexterity required."""
    window.history_dock.toggleView(True)
    # QtAds's setFloating() takes no argument — it floats, and re-docking is
    # addDockWidget's job, which is exactly what _redock_panels re-issues.
    window.history_dock.setFloating()
    assert window.history_dock.isFloating()

    window._redock_panels()

    assert not window.history_dock.isFloating()
    assert window.history_dock.dockContainer() is window.dock_manager


def test_redock_leaves_a_closed_panel_closed(window) -> None:
    # addDockWidget re-opens a closed dock, so _redock_panels has to skip
    # them — otherwise Re-dock silently undoes every View-menu toggle.
    assert window.help_dock.isClosed()

    window._redock_panels()

    assert window.help_dock.isClosed()


def test_closing_a_window_stops_its_timers(window_factory, config) -> None:
    """Regression (Windows CI access violation): a closed window must go
    inert — its drain and preview timers left running turn every closed test
    window into a zombie that ticks in later tests' event pumps."""
    window = window_factory(config)
    assert window._bus_timer.isActive() and window._preview_timer.isActive()

    window.close()

    assert not window._bus_timer.isActive()
    assert not window._preview_timer.isActive()


def test_indicators_start_disconnected(window) -> None:
    assert "disconnected" in window.camera_label.text()
    assert "disconnected" in window.serial_label.text()


def test_serial_indicator_updates_and_announces(window) -> None:
    seen = []
    window.bus.subscribe("serial/state", seen.append)

    window._set_serial_indicator("Serial: connected (COM3)", connected=True)
    window.bus.drain()

    assert "connected (COM3)" in window.serial_label.text()
    assert seen == [{"connected": True, "message": "Serial: connected (COM3)"}]


def test_status_topic_reaches_the_status_bar(window) -> None:
    window.bus.post("status", "Auto-connect: probing COM3…")
    window.bus.drain()

    assert window.statusBar().currentMessage() == "Auto-connect: probing COM3…"


def test_bgr_frame_renders(window) -> None:
    frame = np.zeros((480, 640, 3), np.uint8)

    pixmap = QPixmap.fromImage(window.frame_to_image(frame))

    assert not pixmap.isNull()
    assert (pixmap.width(), pixmap.height()) == (640, 480)


def test_scaled_frames_never_grow_the_window(qapp, window) -> None:
    # Regression: a scaled pixmap must not feed back into the layout's minimum,
    # or the window ratchets larger on every repaint. Both panels scale — the
    # crop on every classification, the live preview on every frame.
    window.show_camera_check.setChecked(True)
    window.resize(1024, 768)
    window.show()
    qapp.processEvents()
    minimum_before = window.minimumSizeHint()
    size_before = window.size()

    frame = np.zeros((1080, 1920, 3), np.uint8)
    window.camera = types.SimpleNamespace(latest_frame=lambda: frame)
    for _ in range(3):
        window._refresh_preview()
        window._show_crop(frame)
        qapp.processEvents()

    assert window.minimumSizeHint() == minimum_before
    assert window.size() == size_before
