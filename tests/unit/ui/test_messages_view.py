"""The Messages panel (issue #112, B4): every status-bar line, kept and readable.

Driven through the real window and bus, offscreen, like test_serial_monitor.py.
"""

from __future__ import annotations

import base64
import logging
import zlib

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QTextCursor
from PySide6.QtTest import QTest

from sorter.ui.message_log import MAX_ENTRIES

from .conftest import drain_until

LONG_ERROR = (
    "HTTPConnectionPool(host='localhost', port=8000): Max retries exceeded with url: "
    "/v1/chat/completions (Caused by NewConnectionError('<urllib3.connection.HTTPConnection "
    "object at 0x7f3a2c1b9d50>: Failed to establish a new connection: [Errno 111] Connection refused'))"
)


def _last_line_format(window):
    """The character format at the end of the panel's last line — its text's, not its stamp's."""
    cursor = QTextCursor(window.messages_view.output.document().lastBlock())
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
    return cursor.charFormat()


def _lines(window) -> list[str]:
    return window.messages_view.text().splitlines()


def test_posting_more_than_the_cap_keeps_the_newest(window) -> None:
    total = MAX_ENTRIES + 5
    for i in range(total):
        window.bus.post("status", f"Message {i}")

    assert drain_until(window, lambda: window.statusBar().currentMessage() == f"Message {total - 1}")
    texts = [entry.text for entry in window.status_log.entries()]
    assert texts == [f"Message {i}" for i in range(5, total)]
    lines = _lines(window)
    assert len(lines) == MAX_ENTRIES
    assert lines[0].endswith("Message 5")
    assert lines[-1].endswith(f"Message {total - 1}")


def test_a_run_error_is_kept_in_full(window) -> None:
    window.bus.post("run/error", LONG_ERROR)
    window.bus.drain()

    entry = window.status_log.entries()[-1]
    assert entry.level == "error"
    assert entry.text == f"Run error: {LONG_ERROR}"
    assert f"[error] Run error: {LONG_ERROR}" in window.messages_view.text()


def test_an_error_renders_distinct_from_info(window) -> None:
    window.bus.post("status", "Connected to COM3.")
    window.bus.drain()
    info = _last_line_format(window)

    window.bus.post("run/error", "Sort timeout")
    window.bus.drain()
    error = _last_line_format(window)

    assert info.foreground().color() == QColor(window.palette_colors["text"])
    assert error.foreground().color() == QColor(window.palette_colors["error"])
    assert error.fontWeight() == QFont.Weight.Bold
    assert info.fontWeight() != QFont.Weight.Bold


def test_a_theme_switch_recolours_the_log(window) -> None:
    window.bus.post("run/error", "Sort timeout")
    window.bus.drain()

    window.set_theme("Light")

    assert _last_line_format(window).foreground().color() == QColor(window.palette_colors["error"])


@pytest.mark.parametrize(
    ("topic", "level"),
    [("status/error", "error"), ("test/error", "error"), ("status", "info"), ("run/status", "info")],
)
def test_each_status_topic_records_its_level(window, topic, level) -> None:
    window.bus.post(topic, "something happened")
    window.bus.drain()

    assert window.status_log.entries()[-1].level == level


def test_run_steps_collapse_into_one_line(window) -> None:
    for _case in range(3):
        for step in ("Feeding (slot 1)…", "Capturing & cropping…", "Classifying…"):
            window.bus.post("run/status", step)
    window.bus.post("run/error", "Sort timeout")
    window.bus.drain()

    assert [entry.text for entry in window.status_log.entries()][-2:] == ["Classifying…", "Run error: Sort timeout"]
    assert _lines(window)[-2].endswith("Classifying…")


def test_download_percentages_are_superseded(window) -> None:
    window.bus.post("status/progress", "Downloading m: 10%")
    window.bus.post("status/progress", "Downloading m: 20%")
    window.bus.post("status", "Imported m.")
    window.bus.drain()

    assert [entry.text for entry in window.status_log.entries()][-1] == "Imported m."
    assert not any("Downloading" in line for line in _lines(window))


def test_the_panel_starts_closed_and_view_toggles_it(window) -> None:
    assert window.messages_dock.isClosed()
    actions = {action.text(): action for action in window.menus["View"].actions()}

    actions["Messages"].trigger()

    assert not window.messages_dock.isClosed()


def test_clicking_the_status_message_opens_the_panel(window, qapp) -> None:
    window.show()
    qapp.processEvents()
    bar = window.statusBar()

    QTest.mouseClick(window.serial_label, Qt.MouseButton.LeftButton)
    assert window.messages_dock.isClosed()  # a permanent widget is not the message

    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(4, bar.height() // 2))
    assert not window.messages_dock.isClosed()


def test_clear_empties_the_panel(window) -> None:
    window.set_status("one")
    window.set_status("two")

    window.messages_view.clear_button.click()

    assert len(window.status_log) == 0
    assert window.messages_view.text() == ""
    assert window.messages_view.count_label.text().startswith("0 messages")
    window.set_status("three")
    assert _lines(window) == [_lines(window)[0]]
    assert _lines(window)[0].endswith("three")


def test_copy_all_copies_every_line(window) -> None:
    window.set_status("Connected to COM3.")
    window.bus.post("run/error", LONG_ERROR)
    window.bus.drain()

    window.messages_view.copy_button.click()

    copied = QGuiApplication.clipboard().text()
    assert copied == window.status_log.dump()
    assert "Connected to COM3." in copied
    assert f"[error] Run error: {LONG_ERROR}" in copied


def test_a_layout_saved_before_the_panel_existed_still_restores(window_factory, config) -> None:
    """A dock state written by a build without the Messages panel keeps the rest of its layout."""
    from sorter.data.repository import SettingsRepo
    from sorter.ui.app import SETTING_WINDOW_STATE

    first = window_factory(config)
    first.history_dock.toggleView(True)
    raw = bytes(first.dock_manager.saveState().data())
    first.close()
    # QtAds writes qCompress'd XML: a 4-byte length, then zlib. Remove the
    # panel's area from its splitter, as the pre-B4 build wrote the state.
    xml = zlib.decompress(raw[4:]).decode()
    area = '<Area Tabs="1" Current="Messages"><Widget Name="Messages" Closed="1"/></Area>'
    for old, new in (
        (area, ""),
        ('Orientation="|" Count="6"', 'Orientation="|" Count="5"'),
        ("0 0 0 0 0 0 ", "0 0 0 0 0 "),
    ):
        assert old in xml
        xml = xml.replace(old, new, 1)
    data = xml.encode()
    blob = len(data).to_bytes(4, "big") + zlib.compress(data)
    SettingsRepo(config.db).set(SETTING_WINDOW_STATE, base64.b64encode(blob).decode("ascii"))

    second = window_factory(config)

    assert not second.history_dock.isClosed()
    assert second.messages_dock.isClosed()
    second.open_messages()
    assert not second.messages_dock.isClosed()
    assert second.messages_dock.dockAreaWidget() is not None


def test_settled_lines_leave_a_debug_trail(window, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="sorter.ui.app")

    window.set_status("Classifying…")
    window.set_status("Run error: Sort timeout", level="error")

    messages = [record.getMessage() for record in caplog.records if record.name == "sorter.ui.app"]
    assert "status [error] Run error: Sort timeout" in messages
    assert not any("Classifying" in message for message in messages)
