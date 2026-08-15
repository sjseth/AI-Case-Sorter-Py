"""Chrome parity (increment 14): About/License, empty states, window/session
persistence, and the dock float/re-dock recovery hooks.

Everything here runs offscreen, no real modals (dialogs are constructed and
inspected directly — never ``.exec()``'d) and against a real SQLite-backed
``Config``, same conventions as test_app.py / test_sort.py.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from sorter.hardware.serial_emulator import EMULATED_PORT
from sorter.ui.app import SIDEBAR_ICON_SIZE, default_qpa_platform

from .conftest import drain_until, seed_model


def _icon_bytes(button) -> bytes:
    """The button's icon as pixels — the only way to see what color it was inked."""
    image = button.icon().pixmap(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE).toImage()
    return bytes(image.constBits())


# ----- QT_QPA_PLATFORM default (no QApplication needed) -----------------------


def test_linux_defaults_to_xcb_with_a_wayland_fallback(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)

    default_qpa_platform()

    assert os.environ.get("QT_QPA_PLATFORM") == "xcb;wayland"


def test_an_explicit_choice_always_wins(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")

    default_qpa_platform()

    assert os.environ.get("QT_QPA_PLATFORM") == "wayland"


def test_non_linux_platforms_are_left_alone(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)

    default_qpa_platform()

    assert os.environ.get("QT_QPA_PLATFORM") is None


# ----- About ------------------------------------------------------------------


def test_about_dialog_shows_identity_and_gpl_notice(window) -> None:
    from sorter import __version__
    from sorter.ui.dialog_about import GPL_NOTICE, AboutDialog

    dialog = AboutDialog(window)

    labels_text = " ".join(child.text() for child in dialog.findChildren(type(dialog.version_label)) if child.text())
    assert "AI Case Sorter OSS" in labels_text
    assert __version__ in dialog.version_label.text()
    assert GPL_NOTICE.split("\n\n")[0] in labels_text


def test_about_dialog_has_no_firmware_line_without_a_broker(window) -> None:
    from sorter.ui.dialog_about import AboutDialog

    dialog = AboutDialog(window)

    assert dialog.firmware_label is None


def test_about_dialog_shows_firmware_version_when_a_broker_is_live(window) -> None:
    from sorter.ui.dialog_about import AboutDialog

    window.broker = types.SimpleNamespace(firmware_version="CS7.2-1.7")

    dialog = AboutDialog(window)

    assert dialog.firmware_label is not None
    assert "CS7.2-1.7" in dialog.firmware_label.text()


def test_about_dialog_links_cover_repo_firmware_issues_and_shop(window) -> None:
    from sorter.ui.dialog_about import (
        FIRMWARE_REPO_URL,
        ISSUES_URL,
        REPO_URL,
        SHOP_URL,
        AboutDialog,
    )

    dialog = AboutDialog(window)

    html = " ".join(
        child.text() for child in dialog.findChildren(type(dialog.version_label)) if "href=" in child.text()
    )
    for url in (REPO_URL, FIRMWARE_REPO_URL, ISSUES_URL, SHOP_URL):
        assert url in html


def test_help_menu_opens_about_and_license(window) -> None:
    calls = []
    window._show_about = lambda: calls.append("about")
    window._show_license = lambda: calls.append("license")

    actions = {a.text(): a for a in window.menus["Help"].actions() if a.text()}
    actions["About"].trigger()
    actions["License"].trigger()

    assert calls == ["about", "license"]


# ----- License ------------------------------------------------------------


def test_license_dialog_loads_the_real_license(window) -> None:
    from sorter.ui.dialog_about import LicenseDialog

    dialog = LicenseDialog(window)

    assert "GNU GENERAL PUBLIC LICENSE" in dialog.text_view.toPlainText()


def test_license_dialog_handles_a_missing_license_file(window, monkeypatch, tmp_path) -> None:
    import sorter.ui.dialog_about as dialog_about

    monkeypatch.setattr(dialog_about, "app_root", lambda: tmp_path)

    dialog = dialog_about.LicenseDialog(window)

    assert "not found" in dialog.text_view.toPlainText()
    assert "GPL" in dialog.text_view.toPlainText() or "gpl" in dialog.text_view.toPlainText().lower()


# ----- Empty state ----------------------------------------------------------


def test_fresh_window_shows_the_empty_state(window) -> None:
    # No board, no camera, and the seeded default model has no headstamps.
    assert window.sort_stack.currentIndex() == 1


def test_empty_state_clears_once_serial_connects(window) -> None:
    window.connect_serial(EMULATED_PORT)

    assert window.sort_stack.currentIndex() == 0


def test_empty_state_clears_once_the_camera_connects(window) -> None:
    window._set_camera_indicator("Camera: connected (640x480)", connected=True)

    assert window.sort_stack.currentIndex() == 0


def test_empty_state_clears_once_something_is_assigned_even_disconnected(window, config) -> None:
    assert window.sort_stack.currentIndex() == 1

    seed_model(config, {"9mm FC": 1})
    window.bus.post("mode/changed", None)
    window.bus.drain()

    assert window.sort_stack.currentIndex() == 0


def test_empty_state_panel_links_to_settings_serial_and_camera(window) -> None:
    window.empty_state_board_button.click()
    assert window.settings_list.currentItem().text() == "Serial"
    assert window.pages.currentWidget() is window._pages_by_name["Settings"]

    window.sidebar_buttons["Sort"].click()
    window.empty_state_camera_button.click()
    assert window.settings_list.currentItem().text() == "Camera"


# ----- sidebar branding -------------------------------------------------------


def test_every_activity_carries_a_vector_icon_at_the_configured_size(window) -> None:
    for name, button in window.sidebar_buttons.items():
        assert not button.icon().isNull(), name
        assert button.iconSize().width() == SIDEBAR_ICON_SIZE
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon


def test_sidebar_labels_are_the_page_name_alone(window) -> None:
    # The glyphs used to live in the label text; they are icons now, so a
    # label is exactly what the width rule measures.
    for name, button in window.sidebar_buttons.items():
        assert button.text() == name


def test_the_checked_activity_is_inked_differently(window) -> None:
    checked = window.sidebar_buttons["Sort"]
    unchecked = window.sidebar_buttons["Models"]
    assert checked.isChecked() and not unchecked.isChecked()

    assert _icon_bytes(checked) != _icon_bytes(unchecked)

    window.sidebar_buttons["Models"].click()

    # Both ends of the swap repaint: the new selection takes the highlight ink
    # and the old one goes back to muted.
    assert _icon_bytes(window.sidebar_buttons["Models"]) != _icon_bytes(checked)


def test_a_theme_switch_re_renders_the_sidebar_icons(window) -> None:
    before = {name: _icon_bytes(b) for name, b in window.sidebar_buttons.items()}

    window.set_theme("Light")

    assert window.palette_colors["text_muted"] != "#9a9a9a"
    for name, button in window.sidebar_buttons.items():
        assert _icon_bytes(button) != before[name], name


def test_the_window_icon_is_the_fixed_neutral_mark(window) -> None:
    assert not window.windowIcon().isNull()
    # It must NOT follow the palette — a taskbar has its own background.
    mark = window.windowIcon().pixmap(32, 32).toImage()
    window.set_theme("Light")
    assert window.windowIcon().pixmap(32, 32).toImage() == mark


# ----- window/session polish --------------------------------------------------


def test_minimum_size_matches_the_tk_reference(window) -> None:
    assert (window.minimumWidth(), window.minimumHeight()) == (960, 660)


def test_close_does_not_prompt_during_a_run(window) -> None:
    # No confirmation, ever — see the judgment-call register. A stand-in
    # controller is enough; nothing here needs a real board.
    window.run_controller = types.SimpleNamespace(stop=lambda: None)
    window.bus.post("run/started", None)
    window.bus.drain()

    calls = []
    window.notify = lambda *a, **k: calls.append(a)

    window.close()

    assert calls == []


# ----- window/session persistence ---------------------------------------------


def test_window_state_and_model_columns_persist_across_a_rebuild(window_factory, config, qapp) -> None:
    # isVisible() only reflects reality once the top-level is actually shown.
    first = window_factory(config)
    first.show()
    qapp.processEvents()
    first.models_page.tree.header().resizeSection(0, 321)
    first.history_dock.toggleView(True)  # closed at startup; opened here
    qapp.processEvents()

    first.close()  # closeEvent saves ui.window_state + ui.models_columns
    qapp.processEvents()

    second = window_factory(config)
    second.show()
    qapp.processEvents()

    assert second.models_page.tree.header().sectionSize(0) == 321
    # The dock half is now CDockManager's XML state, not QMainWindow's.
    assert not second.history_dock.isClosed()


def test_restore_ignores_a_malformed_setting(window_factory, config) -> None:
    from sorter.data.repository import SettingsRepo
    from sorter.ui.app import SETTING_MODELS_COLUMNS, SETTING_WINDOW_STATE

    SettingsRepo(config.db).set(SETTING_WINDOW_STATE, "not valid base64!!")
    SettingsRepo(config.db).set(SETTING_MODELS_COLUMNS, 12345)  # wrong type entirely

    # Must not raise.
    window_factory(config)


# ----- dock float / re-dock ----------------------------------------------------
#
# The stock-Qt suite here used to pin a stack of QMainWindowLayout workarounds
# (a repaint-on-transition handler, a collapsed-dock size floor, a 1px resize
# nudge). QtAds lays panels out in its own splitters, so none of those failure
# modes exist to work around and the code they covered is gone. What remains
# worth pinning is the behaviour users actually hit.


def test_a_floated_panel_returns_home_and_stays_open(window, qapp) -> None:
    window.show()
    qapp.processEvents()
    window.serial_dock.setFloating()
    qapp.processEvents()
    assert window.serial_dock.isFloating()

    window._redock_panels()
    qapp.processEvents()

    assert not window.serial_dock.isFloating()
    assert not window.serial_dock.isClosed()
    assert window.serial_dock.dockContainer() is window.dock_manager
    assert window.dock_manager.floatingWidgets() == []


def test_panels_stay_closable_movable_and_floatable(window) -> None:
    import PySide6QtAds as ads

    feature = ads.CDockWidget.DockWidgetFeature
    for dock in (window.serial_dock, window.history_dock, window.help_dock):
        features = dock.features()
        assert features & feature.DockWidgetClosable
        assert features & feature.DockWidgetMovable
        assert features & feature.DockWidgetFloatable


def test_the_workspace_is_a_fixed_central_area(window) -> None:
    # The sidebar+pages must never be draggable or closable: it is the app,
    # not a panel. QtAds's central widget is exactly that contract.
    feature = window.central_dock.features()
    import PySide6QtAds as ads

    assert window.central_dock.isCentralWidget()
    assert not feature & ads.CDockWidget.DockWidgetFeature.DockWidgetClosable
    assert not feature & ads.CDockWidget.DockWidgetFeature.DockWidgetFloatable


def test_qtads_own_stylesheet_is_disabled_so_the_theme_owns_the_panels(window) -> None:
    # QtAds installs a ~10 KB sheet on the manager, which sits nearer the
    # panels than the window's and would win — freezing them at one palette.
    assert window.dock_manager.styleSheet() == ""
    assert "ads--CDockWidgetTab" in window.styleSheet()


# ----- the inference-device status-bar indicator (#36 follow-up) ----------------


def test_device_indicator_is_hidden_at_startup(window) -> None:
    assert not window.device_label.isVisible()


def test_device_indicator_appears_once_a_local_model_classifies(window, config, monkeypatch) -> None:
    from sorter.ml import local_inference

    seed_model(config, {"FC": 1})
    monkeypatch.setattr(local_inference, "device_description", lambda: "MPS · Apple M4 Pro")
    window.show()

    window.bus.post("run/classified", {"label": "FC", "confidence": 90.0, "slot": 1})
    assert drain_until(window, lambda: window.device_label.isVisible())
    assert window.device_label.text() == "Inference: MPS · Apple M4 Pro"


def test_device_indicator_stays_hidden_in_ai_config_mode(window, monkeypatch) -> None:
    """AI Config classifies over HTTP — a stale device claim would be a lie."""
    from sorter.ml import local_inference

    monkeypatch.setattr(local_inference, "device_description", lambda: "CPU")
    window.show()

    window.bus.post("test/classified", {"label": "FC", "confidence": 90.0})
    window.bus.drain()
    assert not window.device_label.isVisible()


def test_device_indicator_hides_when_the_mode_leaves_local(window, config, monkeypatch) -> None:
    from sorter.data.repository import SettingsRepo
    from sorter.ml import local_inference

    seed_model(config, {"FC": 1})
    monkeypatch.setattr(local_inference, "device_description", lambda: "CPU")
    window.show()
    window.bus.post("run/classified", {"label": "FC", "confidence": 90.0, "slot": 1})
    assert drain_until(window, lambda: window.device_label.isVisible())

    SettingsRepo(config.db).clear_active_model()
    window.bus.post("mode/changed", None)
    assert drain_until(window, lambda: not window.device_label.isVisible())


# ----- auto-connect skips macOS pseudo-ports (#36 follow-up) --------------------


class _DeafBroker:
    """A SerialBroker stand-in whose handshake always fails, instantly."""

    def __init__(self, **_kwargs) -> None:
        self.on_received: list = []
        self.on_sent: list = []

    def try_open(self) -> bool:
        return False


def test_auto_connect_skips_bluetooth_ports_on_darwin(window, monkeypatch) -> None:
    from sorter.hardware import serial_broker

    monkeypatch.setattr(serial_broker.sys, "platform", "darwin")
    monkeypatch.setattr(
        serial_broker,
        "list_serial_ports",
        lambda: ["/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.JabraEvolve275", "/dev/cu.usbmodem14201"],
    )
    monkeypatch.setattr(serial_broker, "SerialBroker", _DeafBroker)
    notes: list[str] = []
    window.bus.subscribe("serial/note", notes.append)

    window._auto_connect_serial()
    assert drain_until(window, lambda: any("did not handshake" in n for n in notes))

    probed = [n for n in notes if n.startswith("probing ")]
    assert probed == ["probing /dev/cu.usbmodem14201 @ 9600…"]
    skip_notes = [n for n in notes if n.startswith("skipping ")]
    assert len(skip_notes) == 1, "the skipped ports are named, not silently dropped"
    assert "/dev/cu.Bluetooth-Incoming-Port" in skip_notes[0]
    assert "/dev/cu.JabraEvolve275" in skip_notes[0]


def test_auto_connect_always_probes_the_saved_port(window, monkeypatch) -> None:
    """A port the user chose once is not a guess — the filter never vetoes it."""
    from sorter.hardware import serial_broker

    monkeypatch.setattr(serial_broker.sys, "platform", "darwin")
    monkeypatch.setattr(
        serial_broker,
        "list_serial_ports",
        lambda: ["/dev/cu.OddballAdapter", "/dev/cu.usbmodem14201"],
    )
    monkeypatch.setattr(serial_broker, "SerialBroker", _DeafBroker)
    window.config.serial["port"] = "/dev/cu.OddballAdapter"
    notes: list[str] = []
    window.bus.subscribe("serial/note", notes.append)

    window._auto_connect_serial()
    assert drain_until(window, lambda: sum("did not handshake" in n for n in notes) >= 2)

    probed = [n for n in notes if n.startswith("probing ")]
    assert probed[0].startswith("probing /dev/cu.OddballAdapter")
    assert any("cu.usbmodem14201" in n for n in probed)
