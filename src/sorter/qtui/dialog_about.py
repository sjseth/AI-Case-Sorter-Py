"""Help -> About / Help -> License.

URLs mirror ``pyproject.toml``'s ``[project.urls]`` plus two more JL asked
for: the CS7.2 hardware/firmware repo and Seth's shop, the latter found by
grepping ``README.md``'s Acknowledgements section — there is no dedicated
"shop" link anywhere else in the repo, so this is the only source; re-grep
if it ever goes stale.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from .. import __version__
from ..paths import app_root

# Keep in lock-step with pyproject.toml's [project.urls].
REPO_URL = "https://github.com/sjseth/AI-Case-Sorter-Py"
ISSUES_URL = "https://github.com/sjseth/AI-Case-Sorter-Py/issues"
# Not in pyproject.toml (a separate project) — JL asked for it by name.
FIRMWARE_REPO_URL = "https://github.com/sjseth/AI-Case-Sorter-CS7.2"
# From README.md's Acknowledgements section; grep README.md again if this
# ever needs a home other than the shop.
SHOP_URL = "https://shop.sjseth.com"

GPL_NOTICE = (
    "This program is free software: you can redistribute it and/or modify it "
    "under the terms of the GNU General Public License as published by the "
    "Free Software Foundation, either version 3 of the License, or (at your "
    "option) any later version.\n\n"
    "This program is distributed WITHOUT ANY WARRANTY; without even the "
    "implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE."
)

LICENSE_MISSING_TEXT = (
    "LICENSE file not found next to the running app.\n\n"
    "This program is distributed under the GNU General Public License v3.0 "
    "or later — see https://www.gnu.org/licenses/gpl-3.0.html."
)


def firmware_line(win: Any) -> str | None:
    """``None`` unless a broker is actually connected — no line, not a blank one."""
    broker = getattr(win, "broker", None)
    if broker is None:
        return None
    version = getattr(broker, "firmware_version", None)
    return f"Connected firmware: {version}" if version else None


class AboutDialog(QDialog):
    """App identity, GPL notice, and the links JL asked for.

    Identity, links and the license only — the update flow is its own Help
    entry ("Check for updates…", ``dialog_update.py``) and never lands here.
    """

    def __init__(self, win: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About AI Case Sorter OSS")
        self.setMinimumWidth(420)

        column = QVBoxLayout(self)
        title = QLabel("AI Case Sorter OSS", self)
        title.setObjectName("aboutTitle")
        column.addWidget(title)
        self.version_label = QLabel(f"Version {__version__}", self)
        column.addWidget(self.version_label)

        self.firmware_label: QLabel | None = None
        line = firmware_line(win)
        if line:
            self.firmware_label = QLabel(line, self)
            column.addWidget(self.firmware_label)

        notice = QLabel(GPL_NOTICE, self)
        notice.setWordWrap(True)
        notice.setObjectName("mutedLabel")
        column.addWidget(notice)

        # TODO(#11): "Check for updates..." button/link goes here once the
        # update dialog lands (Help menu, per docs/ui-modernization.md).

        for text, url in (
            ("Project on GitHub", REPO_URL),
            ("Hardware & firmware (CS7.2)", FIRMWARE_REPO_URL),
            ("Report an issue", ISSUES_URL),
            ("SJSeth's Shop", SHOP_URL),
        ):
            link = QLabel(f'<a href="{url}">{text}</a>', self)
            link.setOpenExternalLinks(True)
            link.setObjectName(f"aboutLink_{url}")
            column.addWidget(link)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)


class LicenseDialog(QDialog):
    """The repo's LICENSE, read off disk — absence is handled, not fatal."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("License")
        self.resize(640, 520)

        column = QVBoxLayout(self)
        self.text_view = QPlainTextEdit(self)
        self.text_view.setReadOnly(True)
        self.text_view.setPlainText(load_license_text())
        column.addWidget(self.text_view, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)


def load_license_text() -> str:
    path = app_root() / "LICENSE"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return LICENSE_MISSING_TEXT


def build_about_dialog(win: Any) -> AboutDialog:
    return AboutDialog(win, win)


def build_license_dialog(win: Any) -> LicenseDialog:
    return LicenseDialog(win)
