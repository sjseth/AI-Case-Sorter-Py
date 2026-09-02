"""The PySide6 shell: activity sidebar, stacked pages, docked panels, status bar.

This is the app's only UI; ``python -m sorter`` lands here. Every colour comes
from ``ui/palettes.py``, which ``ui/theme.py`` renders as QSS.

This module owns the shell and the Sort dashboard itself; every other surface
is its own module with a ``build_*(win)`` factory that this only wires up.

The panels are Qt Advanced Docking System dock widgets (see ``_build_dock``
and ``DOCK_HOMES``): serial monitor at the bottom, classification history,
user guide and themes on the right, the last three closed until asked for.
The sidebar+pages are the manager's *central* widget, which is what makes
them a fixed anchor the panels arrange around rather than a panel themselves.

The non-UI layers are used as-is: ``EventBus`` (drained by a 50 ms ``QTimer``
— workers post, the main thread dispatches), ``Camera``, ``SerialBroker`` and
``RunController``.

Scope and rationale: docs/ui-modernization.md.
"""

from __future__ import annotations

import base64
import html
import itertools
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

import numpy as np
import PySide6QtAds as ads
from PySide6.QtCore import (
    QByteArray,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .. import __version__
from ..control.events import EventBus
from ..control.run_controller import RunController
from ..hardware import serial_broker
from ..hardware.camera import Camera
from ..hardware.serial_emulator import EMULATED_PORT, EmulatorBroker
from ..ml import classifier, local_inference
from ..paths import app_data_dir
from . import desktop_integration
from .ai_page import build_ai_page
from .community_page import build_community_page
from .dialog_slot_assign import CATCH_ALL_HINT, SlotAssignDialog
from .dialog_template import EditTemplateDialog, NewTemplateDialog
from .dialog_winforms_import import (
    SECTION_NAME as WINFORMS_IMPORT_SECTION,
)
from .dialog_winforms_import import (
    build_winforms_import_section,
    maybe_offer_first_run,
)
from .help_viewer import build_help_window, topic_for
from .history_view import build_history_view
from .icons import AI_CONFIG, COMMUNITY, MODELS, SETTINGS, SORT, TRAIN, app_icon
from .icons import icon as vector_icon
from .models_page import build_models_page
from .palettes import (
    SETTING_CUSTOM_THEMES,
    SETTING_THEME,
    THEMES,
    load_custom_themes,
    resolve_theme,
    theme_names,
)
from .serial_monitor import build_serial_monitor
from .settings_camera import build_camera_section
from .settings_imageproc import build_imageproc_section
from .settings_serial import build_serial_section
from .slot_grid import SlotGrid
from .theme import build_stylesheet, unavailable_ink
from .torch_gate import TorchGate
from .train_page import build_train_page

log = logging.getLogger(__name__)

PREVIEW_FPS = 20
SIDEBAR_WIDTH = 84
# Defensive: every SETTINGS_SECTIONS entry has a builder, so nothing renders
# this unless a section is added without one.
PLACEHOLDER_TEXT = "This section has no settings yet."

# Sidebar: (icon name, page name), in three groups separated by hairlines. The
# icons are drawn from ui/icons.py and inked by the live palette
# (_paint_sidebar_icons) — emoji read as artwork and themed badly.
AI_CONFIG_ACTIVITY = "AI Config"
# The always-live surfaces.
ACTIVITIES = (
    (SORT, "Sort"),
    (MODELS, "Models"),
    (COMMUNITY, "Community"),
)
# Below a separator: the two ways a classifier is taught, of which exactly one
# is live at a time (Seth on AI Config: "it takes the place of the training
# screen; it is analogous to training for an LLM"). Both are always visible —
# the muted one explains itself when clicked (JL).
MODE_ACTIVITIES = (
    (TRAIN, "Train"),
    (AI_CONFIG, AI_CONFIG_ACTIVITY),
)
# Below a second separator, and in the flow rather than pinned under a stretch:
# pinned to the bottom it fell off-screen at a modest window height (JL, on a
# Mac), which is the one entry that must be reachable from anywhere.
SETTINGS_ACTIVITY = (SETTINGS, "Settings")
# One line each, always set, saying only whether this entry is the live one.
ACTIVITY_TOOLTIP_LIVE = "Classification uses this now"
TRAIN_TOOLTIP_MUTED = "Activates when a local model is active — see Models"
AI_CONFIG_TOOLTIP_MUTED = "Activates when 'Use AI Config' or an OpenAI model is selected on Models"
SIDEBAR_ICON_SIZE = 26
# "Import from Windows" is last: it is a one-off errand, not a knob, and its
# name is also a GUIDE.md heading (help_viewer slugifies section names
# straight to an anchor, which tests/unit/ui/test_help.py pins).
SETTINGS_SECTIONS = ("Camera", "Serial", "Image Processing", "Theme", WINFORMS_IMPORT_SECTION)
BAUD_CHOICES = (9600, 19200, 38400, 57600, 115200)
# On every dock's tab: QtAds's drop overlays show where a panel *can* go once
# a drag starts, but nothing hints that it can be dragged at all (JL).
DOCK_DRAG_HINT = "Drag this tab to move the panel; View \u2192 Re-dock panels brings it home."

# The Sort column's primary panel: the crop the classifier actually saw, plus
# the one result it produced (Seth, 2026-08-13 — the Windows app's layout).
# History belongs to the Monitor dock, not to a strip under the dashboard.
CROP_EMPTY_TEXT = "No case captured yet"
RESULT_EMPTY_TEXT = "—"
RESULT_EMPTY_CONFIDENCE = "—"
CAPTURE_CAPTION = "Last capture"
HEADSTAMP_CAPTION = "Headstamp"
CONFIDENCE_CAPTION = "Confidence"

# The live feed is a monitor, not the working surface: off unless asked for,
# and the preview timer does no camera read while it is (see _refresh_preview).
SHOW_CAMERA_TEXT = "Show live camera"
SETTING_SHOW_CAMERA = "ui.sort_show_camera"

# The Start/Stop toggle: one button, two faces. The key is what
# `action_buttons` exposes it under.
RUN_TOGGLE_KEY = "Start/Stop"
RUN_START_TEXT = "Start"
RUN_STOP_TEXT = "Stop"
TEMPLATE_BUTTON_WIDTH = 36

# The camera preview when there is nothing to show. The Sort page's version
# links to where the device is chosen; `_camera_placeholder_html` colors the
# link from the live palette.
PREVIEW_INITIAL_TEXT = "No frame"
CAMERA_DEAD_TEXT = "No camera feed"


class _PreviewLabel(QLabel):
    """The camera preview surface — plain text only, whole-widget click.

    This was a rich-text label with an ``<a>`` link; PySide6 6.11's
    QTextDocument path crashed the Windows CI runner (offscreen) with a
    deterministic access violation the first time an event pump touched it.
    Plain text plus a click signal gives the same affordance without ever
    instantiating a text document.
    """

    clicked = Signal()

    def mousePressEvent(self, event: Any) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


class _CropPanel(QLabel):
    """The last cropped headstamp, filling whatever space the column gives it.

    Keeps the source pixmap aside and re-scales on resize: the scaled copy must
    never become the label's size hint, or each repaint grows the layout the
    next one is scaled to (same discipline as ``_PreviewLabel``'s host).
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._source: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(1, 1)

    def set_source(self, pixmap: QPixmap) -> None:
        self._source = pixmap
        self._rescale()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._source is None or self._source.isNull():
            return
        self.setPixmap(
            self._source.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


CAMERA_DEAD_LINK = "open Camera settings"
CAMERA_FAILED_STATUS = "Camera failed to start — pick a device in Settings → Camera."

# Run options, grouped into one popover rather than spread over the page.
STORE_IMAGES_LABELS = {
    "none": "None",
    "above": "Above confidence floor",
    "below": "Below confidence floor",
    "all": "All images",
}
STORE_IMAGES_BY_LABEL = {label: mode for mode, label in STORE_IMAGES_LABELS.items()}
STORE_IMAGES_WARNING_TITLE = "Store images enabled"
STORE_IMAGES_WARNING_TEXT = (
    "Classified run images will be saved under the active model's run_images "
    "folder. This can use significant disk space over time."
)

# Community model settings (issue #29, A24/A25). The fetch fires on entering
# Sort with a community model active and is fail-open throughout: anything that
# goes wrong leaves the local floor, the local opt-in and no prompts.
MODEL_UPDATE_BUTTON = "Model update: v{version}"
NOTES_BUTTON = "Moderator notes ({count})"
FEEDBACK_BLOCKED_STATUS = "Feedback paused by the model's moderator."
NOTES_GATE_TITLE = "Moderator note"
NOTES_GATE_TEXT = (
    "A moderator has left a note about this model's feedback images. "
    "Read and acknowledge it before starting a run — open it with the "
    "“Moderator notes” button."
)

EMPTY_STATE_TITLE = "Nothing connected yet"
EMPTY_STATE_HINT = "Connect a board and a camera to start sorting."

# A run is stopped, not paused, when the link drops (issue #35): a reconnect
# mid-cycle leaves the wheel's position and the drop pipeline unknown, and
# resuming from there is how a case lands in the wrong bin.
SERIAL_LOST_TITLE = "Serial disconnected"
SERIAL_LOST_TEXT = (
    "The board stopped responding, so the run was stopped.\n\n"
    "Check the cable and the board's power, then reconnect from "
    "Settings → Serial."
)

# Persisted window/session state (JL, increment 14): dock layout + the model
# table's column widths, the same _load_setting/_save_setting pattern used
# for the theme choice.
SETTING_WINDOW_STATE = "ui.window_state"
SETTING_MODELS_COLUMNS = "ui.models_columns"

MIN_WINDOW_SIZE = (960, 660)

# Each panel's home: where it is built, and where View → Re-dock returns it.
DOCK_HOMES = (
    ("serial_dock", ads.BottomDockWidgetArea),
    ("history_dock", ads.RightDockWidgetArea),
    ("help_dock", ads.RightDockWidgetArea),
    ("themes_dock", ads.RightDockWidgetArea),
)


def _configure_dock_manager() -> None:
    """QtAds config flags. Static/global, so they must precede the first manager.

    ``DisableStylesheet`` is the load-bearing one: QtAds otherwise installs its
    own ~10 KB sheet on the manager, which — being nearer the dock widgets than
    the window's — wins over ours and freezes the panels at one hard-coded
    palette. With it off, theme.py's ``ads--*`` rules are the only thing
    painting them.
    """
    flags = ads.CDockManager.eConfigFlag
    for flag in (
        flags.DisableStylesheet,
        flags.HideSingleCentralWidgetTitleBar,  # the pages area is chrome, not a panel
        flags.OpaqueSplitterResize,
        flags.FocusHighlighting,
        flags.DockAreaHasUndockButton,
        flags.DockAreaHasCloseButton,
    ):
        ads.CDockManager.setConfigFlag(flag, True)


class QtMainWindow(QMainWindow):
    def __init__(self, config: Any, *, auto_connect: bool = True) -> None:
        super().__init__()
        self.config = config
        self.db = getattr(config, "db", None)
        self.bus = EventBus()
        self._worker_tokens = itertools.count()
        self._muted_labels: list[QLabel] = []
        # Set before the UI is built: the action row's enabled state reads them.
        self.broker: Any | None = None
        self.run_controller: RunController | None = None
        self._is_running = False
        self._master_count = 0
        self._templates: list[Any] = []
        # The current case only — (display label, confidence, above the floor).
        self._current_result: tuple[str, float, bool] | None = None
        # The store-images disk-usage notice shows once per session, not per run.
        self._store_warning_shown = False
        # Community model settings, from the last Sort-page fetch (A24/A25).
        # `_community_settings` is kept so a serial reconnect — which builds a
        # fresh RunController, and with it a fresh FeedbackService — doesn't
        # silently drop the server's policy.
        self._settings_fetch_busy = False
        self._community_settings: tuple[int, Any] | None = None
        self._model_update: tuple[str, int, int, Any] | None = None
        # Modal seams (CLAUDE.md §5): instance attributes, so a test replaces
        # them and nothing blocks offscreen.
        self.open_notes_dialog: Callable[[], None] = self._open_notes_dialog
        self.open_model_update_dialog: Callable[[], None] = self._open_model_update_dialog

        self.setMinimumSize(*MIN_WINDOW_SIZE)

        load_custom_themes(self._load_setting(SETTING_CUSTOM_THEMES))
        self.theme_name = resolve_theme(self._load_setting(SETTING_THEME))
        self.palette_colors = THEMES[self.theme_name]

        # Before _build_ui: the Camera settings page reads it at construction.
        # Constructing a Camera does not open the device.
        self.camera = Camera(
            device_index=int(config.camera.get("device_index", 0)),
            width=int(config.camera.get("width", 640)),
            height=int(config.camera.get("height", 480)),
        )

        # The one sanctioned front door for anything needing local inference.
        self.ensure_torch = TorchGate(self)

        self.setWindowTitle(f"AI Case Sorter OSS - v{__version__} · GPL-3.0")
        # The filled launcher mark, deliberately not palette-coloured: a taskbar
        # owns its own background (see icons' launcher block). Set
        # application-wide as well, so dialogs inherit it.
        window_icon = app_icon()
        self.setWindowIcon(window_icon)
        QGuiApplication.setWindowIcon(window_icon)
        self._build_ui()
        self._apply_theme(self.theme_name)
        self._restore_window_state()

        self.bus.subscribe("status", self.set_status)
        # Run state comes from the controller's own events, never from the
        # button handlers — a run can also end on its own (error, package halt).
        self.bus.subscribe("run/started", lambda _p: self._on_run_started())
        self.bus.subscribe("run/stopped", lambda _p: self._on_run_stopped())
        self.bus.subscribe("run/status", self.set_status)
        # Manual feed / test cycles report on their own topic; without this
        # their progress is invisible and the previous status looks stuck.
        self.bus.subscribe("test/status", self.set_status)
        self.bus.subscribe("run/error", lambda msg: self.set_status(f"Run error: {msg}"))
        self.bus.subscribe("run/result", self._on_run_result)
        # Both fire right after classify_active — the moment the inference
        # device is guaranteed to have been picked.
        self.bus.subscribe("run/classified", lambda _p: self.refresh_device_indicator())
        self.bus.subscribe("test/classified", lambda _p: self.refresh_device_indicator())
        self.bus.subscribe("run/history", self._on_run_history)
        self.bus.subscribe("run/assignment_changed", lambda _p: self._refresh_sort_grid())
        self.bus.subscribe("run/package_full", self._on_package_full)
        self.bus.subscribe("run/package_halt", self._on_package_halt)
        self.bus.subscribe("serial/disconnected", self._on_serial_disconnected)
        # Headstamps, templates and the Train activity are all scoped to the
        # active model, so a mode switch re-reads every one of them.
        self.bus.subscribe("mode/changed", lambda _p: self._on_mode_changed())
        self._bus_timer = QTimer(self)
        self._bus_timer.timeout.connect(lambda: self.bus.drain(max_items=128))
        self._bus_timer.start(50)

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._preview_timer.start(int(1000 / PREVIEW_FPS))

        self.auth: Any | None = None
        self._update_info: Any | None = None
        self._pending_update: Any | None = None
        if auto_connect:
            # A returning signed-in user finds Community present at launch.
            # Reads the token cache only; never constructed in tests.
            try:
                from ..community.auth import AuthManager

                self.auth = AuthManager()
            except Exception:
                self.auth = None
            self.community_page.refresh_auth_state()
            # The shell opens on Sort, so nothing would otherwise "enter" it.
            self._fetch_community_settings()
            self.start_camera()
            self._auto_connect_serial()
            self._warm_device_indicator()
            QTimer.singleShot(2500, self, self._startup_update_check)
            # After the shell is up, so the dialog has a parent to centre on.
            # Silent unless a Windows install is actually there (issue #98).
            QTimer.singleShot(0, self, self._offer_winforms_import)
        self._apply_auth_visibility()

    # ----- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        # Set before any page is built: show_page("Sort") (from the sidebar's
        # own construction, below) already reaches _update_sort_empty_state.
        self._camera_state = ("Camera: disconnected", False)
        self._serial_state = ("Serial: disconnected", False)
        # Built here rather than with the rest of the status bar (below): the
        # sidebar's own construction enters the Sort page, which paints it.
        # Same quiet role as the app-update button — it appears only when the
        # Sort-page fetch found a newer published version.
        self.model_update_button = QPushButton(self)
        self.model_update_button.setObjectName("update")
        self.model_update_button.clicked.connect(lambda: self.open_model_update_dialog())
        self.model_update_button.hide()

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.pages = QStackedWidget(central)
        self._pages_by_name: dict[str, QWidget] = {}
        self._add_page("Sort", self._build_sort_page())
        self._add_page("Train", self._build_train_page())
        self._add_page(AI_CONFIG_ACTIVITY, self._build_ai_page())
        self._add_page("Models", self._build_models_page())
        self._add_page("Community", self._build_community_page())
        self._add_page("Settings", self._build_settings_page())
        body.addWidget(self._build_sidebar())
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)

        # QtAds owns the docking; the manager installs itself as the window's
        # central widget, and the sidebar+pages become *its* central area — a
        # fixed, unclosable, undraggable anchor the panels arrange around.
        _configure_dock_manager()
        self.dock_manager = ads.CDockManager(self)
        self.central_dock = ads.CDockWidget(self.dock_manager, "Workspace")
        self.central_dock.setWidget(central)
        self.dock_manager.setCentralWidget(self.central_dock)

        # Docks are built before the menus: View hosts their toggle actions.
        self._build_serial_dock()
        self._build_history_dock()
        self._build_help_dock()
        self._build_themes_dock()
        self._build_menus()

        # Where local classification runs (e.g. "Inference: MPS · Apple M4").
        # Hidden until the first classify() has picked a device; absent in AI
        # Config mode, where classification is an HTTP call.
        self.device_label = self._muted_label("", self)
        self.device_label.hide()
        self.statusBar().addPermanentWidget(self.device_label)
        self.camera_label = QLabel(self)
        self.serial_label = QLabel(self)
        # Added left to right, so serial ends up rightmost.
        self.statusBar().addPermanentWidget(self.camera_label)
        self.statusBar().addPermanentWidget(self.serial_label)
        # Rightmost pair: update affordance (hidden until there is something to
        # do — the Help menu is the always-reachable route), then sign-in.
        self.update_button = QPushButton(self)
        self.update_button.setObjectName("update")
        self.update_button.clicked.connect(lambda: self.open_update_dialog())
        self.update_button.hide()
        self.statusBar().addPermanentWidget(self.update_button)
        self.statusBar().addPermanentWidget(self.model_update_button)
        # Community identity — the only surface for it now (JL): the
        # Community page used to carry its own "Signed in as ... [Sign out]"
        # row, which duplicated this button and wasted a row for nothing else
        # the page needed. Hidden until signed in; text/tooltip filled by
        # _apply_auth_visibility.
        self.identity_label = self._muted_label("", self)
        self.identity_label.hide()
        self.statusBar().addPermanentWidget(self.identity_label)
        self.signin_button = QPushButton("Sign in", self)
        self.signin_button.clicked.connect(self._on_signin_clicked)
        self.statusBar().addPermanentWidget(self.signin_button)
        self._paint_indicators()
        self._apply_mode_visibility()
        self.set_status("Idle.")

    def _muted_label(self, text: str, parent: QWidget | None = None) -> QLabel:
        """A label in the muted role — registered so a theme switch recolors it."""
        label = QLabel(text, parent)
        label.setStyleSheet(f"color: {self.palette_colors['text_muted']};")
        self._muted_labels.append(label)
        return label

    def _placeholder_page(self, text: str = PLACEHOLDER_TEXT) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = self._muted_label(text, page)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        return page

    def _add_page(self, name: str, page: QWidget) -> None:
        self._pages_by_name[name] = page
        self.pages.addWidget(page)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget(self)
        sidebar.setObjectName("sidebar")
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(6, 8, 6, 8)
        column.setSpacing(4)

        self.sidebar_buttons: dict[str, QToolButton] = {}
        # Which motif each button carries, so a theme switch can re-ink them.
        self._sidebar_icon_names: dict[str, str] = {}
        # The group owns exclusivity; keep the reference or it is collected.
        self._sidebar_group = QButtonGroup(self)
        self._sidebar_group.setExclusive(True)
        for icon_name, name in ACTIVITIES:
            column.addWidget(self._activity_button(sidebar, icon_name, name))
        self.sidebar_separator = self._sidebar_separator(sidebar)
        column.addWidget(self.sidebar_separator)
        for icon_name, name in MODE_ACTIVITIES:
            column.addWidget(self._activity_button(sidebar, icon_name, name))
        self.sidebar_settings_separator = self._sidebar_separator(sidebar)
        column.addWidget(self.sidebar_settings_separator)
        column.addWidget(self._activity_button(sidebar, *SETTINGS_ACTIVITY))
        column.addStretch(1)

        # Width follows the widest label's font metrics, not a constant — a
        # fixed pixel width clips "Community" on fonts wider than the dev box.
        metrics = sidebar.fontMetrics()
        widest = max(metrics.horizontalAdvance(name) for name in self.sidebar_buttons)
        sidebar.setFixedWidth(max(SIDEBAR_WIDTH, widest + 24))

        self.sidebar_buttons["Sort"].setChecked(True)
        self._paint_sidebar_icons()
        self.show_page("Sort")
        return sidebar

    def _sidebar_separator(self, parent: QWidget) -> QFrame:
        """The hairline splitting the always-live surfaces from the mode pair.

        A QFrame, not a styled QWidget: only the former paints a stylesheet
        background without a paintEvent of its own, which is what keeps the
        colour palette-driven and re-themed by the stylesheet alone.
        """
        line = QFrame(parent)
        line.setObjectName("sidebarSeparator")
        line.setFixedHeight(1)
        return line

    def _activity_button(self, parent: QWidget, icon_name: str, name: str) -> QToolButton:
        button = QToolButton(parent)
        button.setText(name)
        button.setIconSize(QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setCheckable(True)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.clicked.connect(lambda _checked=False, page=name: self.open_activity(page))
        # The checked state is a different ink, and the group flips two buttons
        # per click — so both ends of the swap repaint themselves.
        button.toggled.connect(lambda _on=False, page=name: self._paint_sidebar_icon(page))
        self._sidebar_group.addButton(button)
        self.sidebar_buttons[name] = button
        self._sidebar_icon_names[name] = icon_name
        return button

    def _paint_sidebar_icon(self, name: str) -> None:
        """Ink one sidebar icon for its current state, from the live palette.

        The three colors are the ones theme.py's ``#sidebar QToolButton``
        rules put on the label, so icon and text always agree.
        """
        button = self.sidebar_buttons[name]
        if button.property("unavailable"):
            color = unavailable_ink(self.palette_colors)
        else:
            color = self.palette_colors["text_highlight" if button.isChecked() else "text_muted"]
        button.setIcon(vector_icon(self._sidebar_icon_names[name], color, SIDEBAR_ICON_SIZE))

    def _paint_sidebar_icons(self) -> None:
        for name in self.sidebar_buttons:
            self._paint_sidebar_icon(name)

    def _build_sort_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal, page)
        splitter.addWidget(self._build_preview_column(splitter))
        splitter.addWidget(self._build_grid_column(splitter))
        # JL: the slot cards are the working surface and get the majority; the
        # camera is a monitor, not the centerpiece.
        splitter.setSizes([360, 640])

        # Index 0 is the working dashboard, 1 the first-run guided panel — see
        # _update_sort_empty_state. Nothing computes the initial state here:
        # _paint_indicators() (called once _build_ui finishes) does that.
        self.sort_stack = QStackedWidget(page)
        self.sort_stack.addWidget(splitter)
        self.sort_stack.addWidget(self._build_empty_state_panel(page))
        column.addWidget(self.sort_stack, 1)
        # At the foot, mirroring the Train page's Training strip (JL): the
        # working surface first, the launchers under it.
        column.addLayout(self._build_action_row(page))
        return page

    def _build_grid_column(self, parent: QWidget) -> QWidget:
        """The slot grid, with the run counter/reset and the template picker above it.

        JL (follow-up to the run-options move): the counter and reset button
        felt orphaned floating on the action row — they belong with what
        they count, not with Start/Stop/Manual feed. Same argument for the
        template picker: it names the layout these cards *are*.
        """
        holder = QWidget(parent)
        column = QVBoxLayout(holder)
        # Left margin clears the splitter handle — "Slots" was pressed right
        # up against the divider line (JL live-testing).
        column.setContentsMargins(10, 0, 0, 0)
        column.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(self._muted_label("Slots", holder))
        header.addStretch(1)
        header.addWidget(self._muted_label("Sorted this run", holder))
        self.master_count_label = QLabel("0", holder)
        self.master_count_label.setObjectName("masterCount")
        header.addWidget(self.master_count_label)
        reset = QPushButton("Reset counts", holder)
        reset.clicked.connect(self.reset_counts)
        header.addWidget(reset)
        self._add_template_group(holder, header)
        column.addLayout(header)

        self.slot_grid = SlotGrid(self.config, holder)
        self.slot_grid.slot_clicked.connect(lambda slot: self.open_slot_editor(slot))
        self.slot_grid.slot_reset.connect(lambda slot: self.reset_slot_count(slot))
        column.addWidget(self.slot_grid, 1)
        return holder

    def _build_empty_state_panel(self, page: QWidget) -> QWidget:
        """First-run guidance in place of a grid nothing has configured yet."""
        panel = QWidget(page)
        column = QVBoxLayout(panel)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.setSpacing(10)

        title = QLabel(EMPTY_STATE_TITLE, panel)
        title.setObjectName("emptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        hint = self._muted_label(EMPTY_STATE_HINT, panel)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(hint)

        self.empty_state_board_button = QPushButton("Connect a board — Settings → Serial", panel)
        self.empty_state_board_button.clicked.connect(lambda: self._open_settings_section("Serial"))
        column.addWidget(self.empty_state_board_button)

        self.empty_state_camera_button = QPushButton("Connect a camera — Settings → Camera", panel)
        self.empty_state_camera_button.clicked.connect(lambda: self._open_settings_section("Camera"))
        column.addWidget(self.empty_state_camera_button)
        return panel

    def go_to_activity(self, name: str) -> None:
        """Navigate as if the sidebar button had been clicked, checked state included.

        What an in-page "take me there" button wants: ``open_activity`` alone
        switches the page but leaves the sidebar pointing at where the user
        was.
        """
        button = self.sidebar_buttons.get(name)
        if button is not None:
            button.setChecked(True)
        self.open_activity(name)

    def _open_settings_section(self, name: str) -> None:
        self.go_to_activity("Settings")
        items = self.settings_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if items:
            self.settings_list.setCurrentItem(items[0])

    def _refresh_sort_grid(self) -> None:
        """Assignments changed (bus event, template swap, mode switch, edit)."""
        self.slot_grid.refresh_assignments()
        self._update_sort_empty_state()

    def _update_sort_empty_state(self) -> None:
        """No board, no camera, nothing routed anywhere yet -> the guided panel.

        Re-evaluated on every indicator paint (camera/serial connect changes)
        and every assignment change, never cached: any one of the three
        conditions clearing is enough to swap back to the real dashboard.
        """
        connected = self._camera_state[1] or self._serial_state[1]
        fresh_db = not any(int(h.get("slot", 0)) > 0 for h in self.config.headstamps)
        self.sort_stack.setCurrentIndex(1 if (not connected and fresh_db) else 0)

    def _build_action_row(self, page: QWidget) -> QHBoxLayout:
        """The launchers, right-aligned — the Train page's Training strip, mirrored (JL)."""
        actions = QHBoxLayout()
        self.action_buttons: dict[str, QPushButton] = {}
        actions.addStretch(1)

        # Visible whenever the active model has notes at all, acknowledged or
        # not — it is the history view as well as the ack flow.
        self.notes_button = QPushButton(NOTES_BUTTON.format(count=0), page)
        self.notes_button.clicked.connect(lambda: self.open_notes_dialog())
        self.notes_button.hide()
        actions.addWidget(self.notes_button)

        feed_button = QPushButton("Manual feed", page)
        feed_button.clicked.connect(self.manual_feed)
        actions.addWidget(feed_button)
        self.action_buttons["Manual feed"] = feed_button

        # No dedicated row for this (JL) — was its own bar under the
        # template row. The run counter/reset live with the grid instead
        # (see `_build_grid_column`), not here.
        self.run_options_button = self._build_run_options_button(page)
        actions.addWidget(self.run_options_button)

        # One button, two faces (JL): a run is on or it isn't, and the button
        # that ends it is the one that started it. `_update_run_buttons` owns
        # the label/role swap. Last on the strip: the green primary sits at
        # the far right, matching the Training strip (JL).
        self.run_button = QPushButton(RUN_START_TEXT, page)
        self.run_button.setObjectName("action")
        self.run_button.clicked.connect(self.toggle_run)
        actions.addWidget(self.run_button)
        self.action_buttons[RUN_TOGGLE_KEY] = self.run_button

        self._update_run_buttons()
        return actions

    def _add_template_group(self, page: QWidget, bar: QHBoxLayout) -> None:
        """The template picker, right-aligned on the slot grid's header row.

        JL: which layout a run uses belongs over the cards that layout *is*,
        after the counters — not down on the launcher strip. The picker and
        its two edits are a group, so New/Edit shrink to glyphs with tooltips.
        """
        self.template_hint = self._muted_label("", page)
        bar.addWidget(self.template_hint)
        bar.addWidget(self._muted_label("Template", page))
        self.template_combo = QComboBox(page)
        self.template_combo.setMinimumWidth(200)
        self.template_combo.setMaximumWidth(240)
        # `activated` is user-only, so repopulating the combo can't look like a switch.
        self.template_combo.activated.connect(self._on_template_selected)
        bar.addWidget(self.template_combo)
        self.template_new_button = QPushButton("+", page)
        self.template_new_button.setToolTip("New template…")
        self.template_new_button.clicked.connect(self.new_template)
        self.template_edit_button = QPushButton("✎", page)
        self.template_edit_button.setToolTip("Edit template…")
        self.template_edit_button.clicked.connect(self.edit_template)
        for button in (self.template_new_button, self.template_edit_button):
            button.setMaximumWidth(TEMPLATE_BUTTON_WIDTH)
            bar.addWidget(button)
        self._refresh_templates()

    def _build_run_options_button(self, page: QWidget) -> QToolButton:
        """Everything that configures how a run behaves, in one popover.

        Grouped compactly rather than always-visible (JL, increment 14:
        package mode/batch moved here from the template bar, which now
        carries only template things).
        """
        button = QToolButton(page)
        button.setText("⚙ Run options")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        menu = QMenu(button)
        form_holder = QWidget(menu)
        form = QFormLayout(form_holder)
        form.setContentsMargins(10, 8, 10, 8)

        self.store_images_combo = QComboBox(form_holder)
        self.store_images_combo.addItems(list(STORE_IMAGES_LABELS.values()))
        self.store_images_combo.setCurrentText(STORE_IMAGES_LABELS.get(self.config.run_store_images, "None"))
        self.store_images_combo.currentTextChanged.connect(self._on_store_images_changed)
        form.addRow("Store images", self.store_images_combo)

        self.floor_spin = QSpinBox(form_holder)
        self.floor_spin.setRange(0, 100)
        self.floor_spin.setSuffix("%")
        self.floor_spin.setValue(int(self.config.run_confidence_floor))
        self.floor_spin.valueChanged.connect(self._on_floor_changed)
        form.addRow("Confidence floor", self.floor_spin)

        self.auto_select_check = QCheckBox("Automatically select trays", form_holder)
        self.auto_select_check.setChecked(bool(self.config.run_auto_select_trays))
        self.auto_select_check.toggled.connect(self._on_auto_select_toggled)
        form.addRow(self.auto_select_check)

        self.package_check = QCheckBox("Package mode", form_holder)
        self.package_check.setChecked(bool(self.config.run_package_mode))
        self.package_check.toggled.connect(self._on_package_mode_toggled)
        form.addRow(self.package_check)

        self.batch_caption = self._muted_label("Batch size", form_holder)
        self.batch_spin = QSpinBox(form_holder)
        self.batch_spin.setRange(1, 999999)
        self.batch_spin.setValue(int(self.config.run_package_size))
        self.batch_spin.valueChanged.connect(self._on_batch_size_changed)
        form.addRow(self.batch_caption, self.batch_spin)
        self._apply_package_visibility()

        action = QWidgetAction(menu)
        action.setDefaultWidget(form_holder)
        menu.addAction(action)
        button.setMenu(menu)
        return button

    def _on_store_images_changed(self, label: str) -> None:
        mode = STORE_IMAGES_BY_LABEL.get(label, "none")
        self.config.set_run_store_images(mode)
        if mode != "none" and not self._store_warning_shown:
            self._store_warning_shown = True
            self.notify(STORE_IMAGES_WARNING_TITLE, STORE_IMAGES_WARNING_TEXT)

    def _on_floor_changed(self, value: int) -> None:
        # The current-result line reads config.run_confidence_floor live on
        # every run/history event — no separate wiring for the coloring.
        self.config.set_run_confidence_floor(int(value))

    def _on_auto_select_toggled(self, checked: bool) -> None:
        self.config.set_run_auto_select_trays(bool(checked))

    def _build_preview_column(self, parent: QWidget) -> QWidget:
        """The crop the classifier saw, what it made of it, and — on request — the feed.

        Seth (2026-08-13): the operator watches the *cropped* headstamp and the
        call made on it, the way the Windows app shows them. The live camera is
        a setup aid, so it is off by default and secondary when shown.
        """
        holder = QWidget(parent)
        column = QVBoxLayout(holder)
        # Right margin clears the splitter handle, mirroring the grid
        # column's left margin (JL: the divider line sat too tight).
        column.setContentsMargins(0, 0, 10, 0)
        column.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(self._muted_label(CAPTURE_CAPTION, holder))
        header.addStretch(1)
        self.show_camera_check = QCheckBox(SHOW_CAMERA_TEXT, holder)
        # Restored before the preview exists, so the handler is wired below it.
        self.show_camera_check.setChecked(bool(self._load_setting(SETTING_SHOW_CAMERA)))
        header.addWidget(self.show_camera_check)
        column.addLayout(header)

        self.crop_label = _CropPanel(CROP_EMPTY_TEXT, holder)
        self.crop_label.setObjectName("cropPanel")
        column.addWidget(self.crop_label, 3)
        column.addLayout(self._build_result_row(holder))

        self.preview_label = _PreviewLabel(PREVIEW_INITIAL_TEXT, holder)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # A dead camera is the one thing this panel can usefully say, so it
        # says where to fix it rather than sitting black (JL). The whole
        # label is the click target — see _PreviewLabel for why not a link.
        self.preview_label.clicked.connect(self._on_preview_clicked)
        # Ignored + tiny minimum: the label must never report the pixmap as
        # its size hint, or each scaled frame grows the layout that the next
        # frame is scaled to — the window ratchets larger on every repaint.
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview_label.setMinimumSize(1, 1)
        # A video letterbox is black in every theme; not chrome, so not themed.
        self.preview_label.setStyleSheet("background-color: #000000; color: #808080;")
        self.preview_label.setVisible(self.show_camera_check.isChecked())
        # Smaller stretch than the crop: shown, it is the secondary panel.
        column.addWidget(self.preview_label, 2)
        self.show_camera_check.toggled.connect(self._on_show_camera_toggled)
        return holder

    def _build_result_row(self, holder: QWidget) -> QHBoxLayout:
        """The current case, Windows-style: what it is and how sure we are."""
        row = QHBoxLayout()
        row.addWidget(self._muted_label(HEADSTAMP_CAPTION, holder))
        self.result_label = QLabel(RESULT_EMPTY_TEXT, holder)
        self.result_label.setObjectName("currentHeadstamp")
        row.addWidget(self.result_label)
        row.addStretch(1)
        row.addWidget(self._muted_label(CONFIDENCE_CAPTION, holder))
        self.result_confidence_label = QLabel(RESULT_EMPTY_CONFIDENCE, holder)
        self.result_confidence_label.setObjectName("currentConfidence")
        row.addWidget(self.result_confidence_label)
        self._paint_current_result()
        return row

    def _on_show_camera_toggled(self, checked: bool) -> None:
        self.preview_label.setVisible(bool(checked))
        if checked and not self._camera_state[1]:
            self._paint_preview_placeholder()
        self._save_setting(SETTING_SHOW_CAMERA, bool(checked))

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(12)
        self.settings_list = QListWidget(page)
        self.settings_list.setFixedWidth(160)
        self.settings_pages = QStackedWidget(page)
        builders = {
            "Theme": self._build_theme_section,
            "Serial": self._build_serial_page,
            "Camera": lambda: build_camera_section(self),
            "Image Processing": lambda: build_imageproc_section(self),
            WINFORMS_IMPORT_SECTION: lambda: build_winforms_import_section(self),
        }
        for name in SETTINGS_SECTIONS:
            self.settings_list.addItem(name)
            build = builders.get(name)
            self.settings_pages.addWidget(build() if build else self._placeholder_page())
        self.settings_list.currentRowChanged.connect(self.settings_pages.setCurrentIndex)
        self.settings_list.setCurrentRow(0)
        row.addWidget(self.settings_list)
        row.addWidget(self.settings_pages, 1)
        return page

    def _build_train_page(self) -> QWidget:
        # Kept on self: mode/changed and navigating to the page both refresh it.
        self.train_page = build_train_page(self)
        return self.train_page

    def _build_models_page(self) -> QWidget:
        # Kept on self: mode/changed and navigating to the page both refresh it.
        self.models_page = build_models_page(self)
        self.models_page.set_images_hook(self._open_model_images)
        self.models_page.set_headstamps_hook(self._open_headstamps)
        self.models_page.set_evaluate_hook(self._open_evaluator)
        return self.models_page

    def open_help(self) -> None:
        """F1 / Help menu: the guide dock, opened at the current context's topic."""
        page = next(
            (name for name, w in self._pages_by_name.items() if w is self.pages.currentWidget()),
            "Sort",
        )
        section = None
        if page == "Settings":
            item = self.settings_list.currentItem()
            section = item.text() if item is not None else None
        self.help_view.show_topic(topic_for(page, section))
        self.reveal_dock(self.help_dock)

    def reveal_dock(self, dock: Any) -> None:
        """Open a panel and bring it to the front — the QtAds show/raise pair.

        ``toggleView(True)`` re-opens a closed panel (QWidget ``show()`` does
        not: QtAds tracks closed-ness itself, and a closed dock has been taken
        out of its area). ``setAsCurrentTab`` is the raise: a panel sharing a
        tab group with another is otherwise "open" but behind it.
        """
        dock.toggleView(True)
        dock.setAsCurrentTab()
        container = dock.floatingDockContainer()
        if container is not None:
            container.raise_()

    def open_serial_monitor(self) -> None:
        """Settings → Serial's "Open serial monitor" button; View toggles it directly."""
        self.reveal_dock(self.serial_dock)

    def _open_model_images(self, model: Any) -> None:
        from .dialog_model_images import ModelImagesDialog

        ModelImagesDialog(self, self.config, model.id).exec()

    def _open_headstamps(self, model: Any) -> None:
        from .dialog_headstamps import HeadstampManagerDialog

        HeadstampManagerDialog(self, self.config, model.id, bus=self.bus).exec()

    def _open_evaluator(self, model: Any) -> None:
        from .dialog_model_evaluator import ModelEvaluatorDialog

        ModelEvaluatorDialog(self, self, model).exec()

    def _build_ai_page(self) -> QWidget:
        # Kept on self: mode/changed and navigating to the page both refresh it.
        self.ai_page = build_ai_page(self)
        return self.ai_page

    def _build_theme_section(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(QLabel("Theme", page))
        self.theme_combo = QComboBox(page)
        self.theme_combo.addItems(theme_names())
        self.theme_combo.setCurrentText(self.theme_name)
        # Connected last: setCurrentText must not count as the user choosing.
        self.theme_combo.currentTextChanged.connect(self.set_theme)
        self.theme_combo.setMaximumWidth(240)
        column.addWidget(self.theme_combo)
        self.theme_edit_button = QPushButton("Edit theme…", page)
        self.theme_edit_button.setMaximumWidth(240)
        self.theme_edit_button.clicked.connect(self._open_theme_editor)
        column.addWidget(self.theme_edit_button)
        column.addStretch(1)
        return page

    def _open_theme_editor(self) -> None:
        from .dialog_theme_editor import ThemeEditorDialog

        ThemeEditorDialog(self, self).exec()

    def _build_serial_page(self) -> QWidget:
        # Kept on self for tests and the serial/state reactions it subscribes.
        self.serial_section = build_serial_section(self)
        return self.serial_section

    def _build_dock(self, title: str, widget: QWidget, area: Any, *, scroll_area: bool = True) -> Any:
        """One QtAds panel: closable, movable, floatable, hinted on its tab.

        ``scroll_area=False`` for content that lays itself out from the space
        it is given. QtAds wraps a panel's widget in a ``QScrollArea`` by
        default, which hands that widget its *preferred* size and scrolls the
        difference — so the widget is never told the panel got smaller, and
        answers a question nobody asked (issue #101).
        """
        dock = ads.CDockWidget(self.dock_manager, title)
        insert_mode = (
            ads.CDockWidget.eInsertMode.AutoScrollArea if scroll_area else ads.CDockWidget.eInsertMode.ForceNoScrollArea
        )
        dock.setWidget(widget, insert_mode)
        # The tab *is* the drag handle in QtAds, so that is where the hint goes
        # (a QDockWidget put it on the whole panel, which was never the target).
        dock.setTabToolTip(DOCK_DRAG_HINT)
        self.dock_manager.addDockWidget(area, dock)
        return dock

    def _build_serial_dock(self) -> None:
        # The monitor subscribes serial/* itself and keeps the full session
        # history — a dock that exists from startup needs no backlog replay.
        self.serial_monitor = build_serial_monitor(self)
        # Bottom, like Arduino IDE's monitor / VS Code's terminal (JL).
        self.serial_dock = self._build_dock("Serial Monitor", self.serial_monitor, ads.BottomDockWidgetArea)

    def _build_history_dock(self) -> None:
        self.history_view = build_history_view(self)
        # No scroll area: the tile grid sizes itself to the panel and drops
        # what doesn't fit, which only works if it is told the panel's real
        # size (issue #101).
        self.history_dock = self._build_dock(
            "Classification History",
            self.history_view,
            ads.RightDockWidgetArea,
            scroll_area=False,
        )
        # Supplementary, so it starts out of the way; View re-opens it.
        self.history_dock.toggleView(False)

    def _redock_panels(self) -> None:
        """Return every open panel to its home area, un-floated.

        View → "Re-dock panels", the always-works escape hatch. Re-issuing
        each ``addDockWidget`` is the whole implementation: in QtAds that
        pulls the panel out of wherever it ended up (a floating container, a
        tab group, another edge) and rebuilds the startup layout. Closed
        panels are skipped — ``addDockWidget`` re-opens a closed dock, and a
        panel the user switched off in View must stay off.
        """
        for attr, area in DOCK_HOMES:
            dock = getattr(self, attr)
            if dock.isClosed():
                continue
            self.dock_manager.addDockWidget(area, dock)

    def _build_community_page(self) -> QWidget:
        self.community_page = build_community_page(self)
        self.community_page.on_auth_changed = self._on_auth_changed
        return self.community_page

    def _apply_auth_visibility(self) -> None:
        signed_in = self.community_page.is_signed_in()
        self._set_activity_visible("Community", signed_in)
        self.signin_button.setText("Sign out" if signed_in else "Sign in")
        self._update_identity_label(signed_in)

    def _update_identity_label(self, signed_in: bool) -> None:
        """Display name (or email) next to the Sign out button, display-only.

        Read straight off the auth object's decoded claims — same source the
        removed Community-page banner used, not the community server's
        profile metadata, so this never blocks on a network call.
        ``existing_auth_manager()`` never constructs one (CLAUDE.md: building
        must not touch MSAL) — if ``signed_in`` is True one already exists.
        """
        if not signed_in:
            self.identity_label.hide()
            self.identity_label.setText("")
            self.identity_label.setToolTip("")
            return
        auth = self.community_page.existing_auth_manager()
        try:
            name, email = auth.identity() if auth is not None else (None, None)
        except Exception:
            name, email = None, None
        name = (name or "").strip()
        email = (email or "").strip()
        self.identity_label.setText(name or email or "(unknown)")
        self.identity_label.setToolTip(email)
        self.identity_label.show()

    def _on_auth_changed(self) -> None:
        self._apply_auth_visibility()

    def _on_signin_clicked(self) -> None:
        if self.community_page.is_signed_in():
            self.community_page.sign_out()
        else:
            self.community_page.open_login()

    # ----- import from the Windows app ----------------------------------------

    def _offer_winforms_import(self) -> None:
        """First-run offer. Silent, and cheap, when there is nothing to offer."""
        try:
            maybe_offer_first_run(self)
        except Exception:
            # An import that cannot even be offered must not take the launch
            # with it — Settings keeps the same dialog reachable.
            log.exception("first-run Windows-app import offer failed")

    def after_winforms_import(self, result: Any) -> None:
        """Re-read everything the import may have rewritten.

        It can touch the model library, the active model, every headstamp and
        slot, and three settings sections at once, so this re-runs the same
        refresh a mode switch does rather than trying to be surgical.
        """
        self.config.load()
        self._on_mode_changed()
        self.models_page.refresh()
        self.set_status("Imported from the Windows app.")

    # ----- updates ------------------------------------------------------------

    def open_update_dialog(self, *, check: bool = False) -> None:
        from .dialog_update import UpdateDialog

        self._update_dialog = UpdateDialog(
            self, info=self._update_info, app=self, pending=self._pending_update, check_on_open=check
        )
        self._update_dialog.open()

    def note_pending_update(self, pending: Any) -> None:
        self._pending_update = pending
        self.update_button.setText("Restart to update")
        self.update_button.show()

    def note_update_info(self, info: Any) -> None:
        self._update_info = info
        if self._pending_update is not None:
            return  # a staged update outranks a fresh finding
        if info is None:
            self.update_button.hide()
        else:
            self.update_button.setText(f"Update to {info.version}")
            self.update_button.show()

    def _startup_update_check(self) -> None:
        """Silent: staged update first, then an opt-out-able check."""
        from ..update import updater

        pending = updater.pending_update()
        if pending is not None:
            self.note_pending_update(pending)
            return
        if updater.checks_disabled() or self._load_setting(updater.SETTING_CHECK_ON_STARTUP) is False:
            return
        self.run_worker(
            updater.check_for_update,
            on_done=self.note_update_info,
            on_error=lambda _exc: None,  # silent by design; Help menu re-checks loudly
        )

    # ----- community model settings (A24/A25) ---------------------------------

    def _fetch_community_settings(self) -> None:
        """One ``FetchModelSettings`` per Sort-page entry, on a worker.

        Gated on the active model being a community one *and* an account
        already existing — reading the token cache must stay behind a
        user action, and a signed-out user has nothing to fetch with.
        Everything downstream fails open: a ``None`` result leaves the local
        floor and opt-in in charge and raises no prompt.
        """
        if self._settings_fetch_busy or self.db is None:
            return
        from ..community.feedback import is_community_model

        model = self._active_model()
        # `model is None` is folded into the guard so the type checker can
        # narrow it for the rest of this method.
        if model is None or not is_community_model(model) or not self.community_page.is_signed_in():
            self._clear_community_settings()
            self._refresh_notes_button()
            return
        uid = str(model.community_model_uid)
        model_id = int(model.id)
        name = model.name
        # The row's own version; 0/absent means "unknown", which never nags.
        installed = int(model.model_version or 0)
        api_factory = self.community_page.api_factory
        find_entry = self.community_page.find_catalogue_entry
        self._settings_fetch_busy = True

        def work() -> tuple[Any, Any]:
            api = api_factory()
            settings = api.fetch_model_settings(uid)
            info = None
            if settings is not None and installed > 0 and settings.version > installed:
                # Only then, and only to fill the dialog + drive the update:
                # the settings response carries a version number and nothing
                # else about the published model.
                try:
                    info = find_entry(uid, api)
                except Exception:
                    info = None
            return settings, info

        self.run_worker(
            work,
            on_done=lambda payload: self._on_community_settings(model_id, uid, name, installed, payload),
            on_error=lambda _exc: self._on_community_settings_failed(),
        )

    def _on_community_settings(self, model_id: int, uid: str, name: str, installed: int, payload: Any) -> None:
        self._settings_fetch_busy = False
        settings, info = payload if isinstance(payload, tuple) else (None, None)
        if settings is None:
            self._on_community_settings_failed()
            return
        self._community_settings = (model_id, settings)
        self._apply_community_settings()
        if settings.blocked:
            # The contract prefers saying so over going quiet — once per fetch,
            # never per case.
            self.set_status(FEEDBACK_BLOCKED_STATUS)

        from ..community import notes as notes_store

        stored = notes_store.merge(self.db, uid, settings.notes)
        self._refresh_notes_button()

        newer = installed > 0 and settings.version > installed and info is not None
        self._model_update = (name, installed, int(settings.version), info) if newer else None
        self._paint_model_update_button()

        if notes_store.unacknowledged(stored):
            # Queued: this runs inside a bus drain, and a modal here would
            # re-enter it (CLAUDE.md §5).
            QTimer.singleShot(0, self, self.open_notes_dialog)

    def _on_community_settings_failed(self) -> None:
        """Offline / refused / garbage: back to purely local behaviour."""
        self._settings_fetch_busy = False
        self._clear_community_settings()
        self._refresh_notes_button()

    def _clear_community_settings(self) -> None:
        self._community_settings = None
        self._model_update = None
        self._paint_model_update_button()
        if self.run_controller is not None:
            self.run_controller.clear_community_settings()

    def _apply_community_settings(self) -> None:
        """Push the last fetch's policy into the (possibly rebuilt) controller."""
        if self.run_controller is None:
            return
        if self._community_settings is None:
            self.run_controller.clear_community_settings()
            return
        model_id, settings = self._community_settings
        self.run_controller.apply_community_settings(
            model_id,
            confidence_floor=int(settings.confidence_floor),
            feedback_enabled=bool(settings.feedback_enabled),
            blocked=bool(settings.blocked),
            wish_list=settings.wish_list,
        )

    def _paint_model_update_button(self) -> None:
        if self._model_update is None:
            self.model_update_button.hide()
            return
        _name, _installed, available, _info = self._model_update
        self.model_update_button.setText(MODEL_UPDATE_BUTTON.format(version=available))
        self.model_update_button.show()

    def model_update_dialog(self) -> Any | None:
        """The dialog, wired but not shown — tests drive its buttons directly."""
        if self._model_update is None:
            return None
        from .dialog_model_update import build_model_update_dialog

        name, installed, available, info = self._model_update
        dialog = build_model_update_dialog(
            self,
            model_name=name,
            installed_version=installed,
            available_version=available,
            info=info,
            on_update=self.start_model_update,
        )
        # "Not now" (or closing) drops the affordance; the next Sort-page entry
        # re-fetches and raises it again. No permanent dismissal.
        dialog.rejected.connect(self.dismiss_model_update)
        return dialog

    def dismiss_model_update(self) -> None:
        self._model_update = None
        self._paint_model_update_button()

    def _open_model_update_dialog(self) -> None:
        dialog = self.model_update_dialog()
        if dialog is not None:
            dialog.exec()

    def start_model_update(self) -> None:
        """Accepting the prompt: the Community page's own download+import path."""
        if self._model_update is None:
            return
        _name, _installed, _available, info = self._model_update
        self._model_update = None
        self._paint_model_update_button()
        self.community_page.start_update(info)

    # ----- moderator notes ----------------------------------------------------

    def _community_uid(self) -> str | None:
        model = self._active_model()
        uid = getattr(model, "community_model_uid", None)
        return str(uid) if uid else None

    def _stored_notes(self) -> list[Any]:
        uid = self._community_uid()
        if uid is None or self.db is None:
            return []
        from ..community import notes as notes_store

        return notes_store.load(self.db, uid)

    def _refresh_notes_button(self) -> None:
        notes = self._stored_notes()
        self.notes_button.setText(NOTES_BUTTON.format(count=len(notes)))
        self.notes_button.setVisible(bool(notes))

    def notes_dialog(self) -> Any | None:
        """The dialog, wired but not shown — tests drive its buttons directly."""
        from .dialog_community_notes import build_community_notes_dialog

        uid = self._community_uid()
        if uid is None:
            return None
        model = self._active_model()
        return build_community_notes_dialog(
            self,
            model_name=getattr(model, "name", ""),
            notes=self._stored_notes(),
            on_acknowledge=lambda ids: self._acknowledge_notes(uid, ids),
        )

    def _open_notes_dialog(self) -> None:
        dialog = self.notes_dialog()
        if dialog is not None:
            dialog.exec()

    def _acknowledge_notes(self, uid: str, ids: list[int]) -> None:
        from ..community import notes as notes_store

        notes_store.acknowledge(self.db, uid, ids)
        self._refresh_notes_button()

    def _build_help_dock(self) -> None:
        # A dock, not a free window (JL): pin the guide beside the work while
        # learning, toggle it away after.
        self.help_view = build_help_window(self)
        self.help_dock = self._build_dock("User Guide", self.help_view, ads.RightDockWidgetArea)
        self.help_dock.toggleView(False)

    def _build_themes_dock(self) -> None:
        """Every theme in one list, applied on the click (Seth via JL).

        Settings → Theme is where a theme is *configured*; this is where one is
        *tried*, which is a different activity — you want the whole list in
        front of you and the app repainting under it. Both drive ``set_theme``
        and both are re-read by ``refresh_theme_picker``, so neither can drift
        from the registry or from each other.
        """
        panel = QWidget(self)
        column = QVBoxLayout(panel)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(6)
        self.theme_list = QListWidget(panel)
        self.theme_list.setObjectName("themeList")
        self.theme_list.addItems(theme_names())
        self.theme_list.setCurrentRow(self._theme_row(self.theme_name))
        # Connected last, like the combo: seeding the selection is not a choice.
        self.theme_list.currentTextChanged.connect(self.set_theme)
        column.addWidget(self.theme_list, 1)
        self.theme_dock_edit_button = QPushButton("Edit theme…", panel)
        self.theme_dock_edit_button.clicked.connect(self._open_theme_editor)
        column.addWidget(self.theme_dock_edit_button)

        self.themes_dock = self._build_dock("Themes", panel, ads.RightDockWidgetArea)
        self.themes_dock.toggleView(False)
        # A theme saved from an editor opened before this panel was lands in
        # the registry without passing through here.
        self.themes_dock.viewToggled.connect(self._on_themes_dock_toggled)

    def _on_themes_dock_toggled(self, opened: bool) -> None:
        if opened:
            self.refresh_theme_picker()

    @staticmethod
    def _theme_row(name: str) -> int:
        names = theme_names()
        return names.index(name) if name in names else 0

    def refresh_theme_picker(self) -> None:
        """Re-read the theme registry into both pickers (the theme editor's hook)."""
        if hasattr(self, "theme_combo"):
            blocked = self.theme_combo.blockSignals(True)
            self.theme_combo.clear()
            self.theme_combo.addItems(theme_names())
            self.theme_combo.blockSignals(blocked)
        if hasattr(self, "theme_list"):
            blocked = self.theme_list.blockSignals(True)
            self.theme_list.clear()
            self.theme_list.addItems(theme_names())
            self.theme_list.blockSignals(blocked)
        self._sync_theme_pickers()

    def _sync_theme_pickers(self) -> None:
        """Point both pickers at the live theme without re-applying it."""
        if hasattr(self, "theme_combo"):
            blocked = self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(self.theme_name)
            self.theme_combo.blockSignals(blocked)
        if hasattr(self, "theme_list"):
            blocked = self.theme_list.blockSignals(True)
            self.theme_list.setCurrentRow(self._theme_row(self.theme_name))
            self.theme_list.blockSignals(blocked)

    def _build_menus(self) -> None:
        # menuBar().addMenu(str) hands the QMenu back with Python ownership; the
        # menus have to be kept alive here or shiboken deletes them.
        self.menus: dict[str, Any] = {}
        file_menu = self.menus["File"] = self.menuBar().addMenu("&File")
        open_data = file_menu.addAction("Open data folder")
        open_data.triggered.connect(self._open_data_folder)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit")
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)

        toggle = self.serial_dock.toggleViewAction()
        toggle.setText("Serial Monitor")
        self.menus["View"] = self.menuBar().addMenu("&View")
        self.menus["View"].addAction(toggle)
        history_toggle = self.history_dock.toggleViewAction()
        history_toggle.setText("Classification History")
        self.menus["View"].addAction(history_toggle)
        help_toggle = self.help_dock.toggleViewAction()
        help_toggle.setText("User Guide panel")
        self.menus["View"].addAction(help_toggle)
        themes_toggle = self.themes_dock.toggleViewAction()
        themes_toggle.setText("Themes")
        self.menus["View"].addAction(themes_toggle)
        self.menus["View"].addSeparator()
        # The always-works escape hatch (Seth: floated the history panel and
        # couldn't get it back): drag-to-dock takes dexterity and has failed
        # users on more than one platform; one menu action can't.
        redock = self.menus["View"].addAction("Re-dock panels")
        redock.triggered.connect(self._redock_panels)

        self.menus["Help"] = self.menuBar().addMenu("&Help")
        guide = self.menus["Help"].addAction("User Guide")
        guide.setShortcut(QKeySequence.StandardKey.HelpContents)  # F1
        guide.triggered.connect(self.open_help)
        check = self.menus["Help"].addAction("Check for updates…")
        check.triggered.connect(lambda: self.open_update_dialog(check=True))
        support = self.menus["Help"].addAction("Export support package…")
        support.triggered.connect(self._open_support_dialog)
        self.menus["Help"].addSeparator()
        about = self.menus["Help"].addAction("About")
        about.triggered.connect(self._show_about)
        license_action = self.menus["Help"].addAction("License")
        license_action.triggered.connect(self._show_license)

    # ----- navigation ---------------------------------------------------------

    def open_activity(self, name: str) -> None:
        """What a sidebar click does: every activity is a page of its own."""
        self.show_page(name)

    def show_page(self, name: str) -> None:
        self.pages.setCurrentWidget(self._pages_by_name[name])
        if name == "Sort":
            # Assignments can have changed in Settings since the cards were
            # last drawn; they are cheap to re-read and never cached.
            self._refresh_sort_grid()
            self._refresh_notes_button()
            self._fetch_community_settings()
        elif name == "Models":
            self.models_page.refresh(announce=True)
        elif name == "Community":
            self.community_page.refresh_auth_state()
        elif name == "Train":
            # Headstamps and images change from the Models page and from
            # imports; the counts are read off disk every time, never cached.
            self.train_page.refresh()
        elif name == AI_CONFIG_ACTIVITY:
            # A click on the *muted* entry has to land on the explainer naming
            # the active model, not on whatever the last mode change left.
            self.ai_page.refresh_mode()

    def _open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir())))

    def _show_about(self) -> None:
        from .dialog_about import build_about_dialog

        build_about_dialog(self).exec()

    def _open_support_dialog(self) -> None:
        """Help → Export support package…: the report to paste on Discord."""
        from .dialog_support import open_support_dialog

        open_support_dialog(self)

    def _show_license(self) -> None:
        from .dialog_about import build_license_dialog

        build_license_dialog(self).exec()

    def open_slot_editor(self, slot: int) -> None:
        """Edit what routes to one slot. The catch-all isn't configurable."""
        if int(slot) == 0:
            self.set_status(CATCH_ALL_HINT)
            return
        dialog = SlotAssignDialog(self.config, int(slot), self)
        dialog.changed.connect(self._refresh_sort_grid)
        dialog.exec()
        self._refresh_sort_grid()

    def _refresh_templates(self) -> None:
        """Repopulate the combo for the active model + current run mode."""
        mode = self.config.slot_template_mode()
        self._templates = self.config.list_slot_templates(mode)
        active = self.config.active_slot_template(mode)
        self.template_combo.clear()
        self.template_combo.addItems([t.name for t in self._templates])
        for index, template in enumerate(self._templates):
            if template.id == active.id:
                self.template_combo.setCurrentIndex(index)
                break
        self.template_hint.setText("Package-mode layout" if mode == "package" else "")

    def _template_busy(self) -> bool:
        """Templates swap the whole layout, so keep them out of a live run."""
        if not self._is_running:
            return False
        self.notify(
            "Run in progress",
            "Stop the run before changing sorting templates — switching one reassigns every slot.",
        )
        return True

    def _on_template_selected(self, index: int) -> None:
        if index < 0 or index >= len(self._templates):
            return
        target = self._templates[index]
        if self._template_busy() or self.config.activate_slot_template(target.id) is None:
            self._refresh_templates()  # snap the combo back to the active one
            return
        self._after_template_change(f"Loaded sorting template “{target.name}”.")

    def new_template(self) -> None:
        if self._template_busy():
            return
        mode = self.config.slot_template_mode()
        dialog = NewTemplateDialog(self.config, mode, self.config.active_slot_template(mode).name, self)
        if dialog.exec() and dialog.created is not None:
            self._after_template_change(f"Created sorting template “{dialog.created.name}”.")

    def edit_template(self) -> None:
        if self._template_busy():
            return
        mode = self.config.slot_template_mode()
        dialog = EditTemplateDialog(
            self.config,
            self.config.active_slot_template(mode),
            can_delete=len(self.config.list_slot_templates(mode)) > 1,
            parent=self,
        )
        if dialog.exec():
            self._after_template_change("Sorting templates updated.")

    def _after_template_change(self, status: str) -> None:
        """Counters are per-layout: a slot may hold another headstamp now."""
        self._refresh_templates()
        self._clear_counts()
        self._refresh_sort_grid()
        self.set_status(status)

    def _apply_package_visibility(self) -> None:
        enabled = self.package_check.isChecked()
        self.batch_caption.setVisible(enabled)
        self.batch_spin.setVisible(enabled)

    def _on_package_mode_toggled(self, enabled: bool) -> None:
        self.config.set_run_package_mode(bool(enabled))
        self._apply_package_visibility()
        # Counts, assignments and templates are all mode-specific.
        self._clear_counts()
        self._refresh_templates()
        self._refresh_sort_grid()

    def _on_batch_size_changed(self, value: int) -> None:
        self.config.set_run_package_size(int(value))
        self._refresh_sort_grid()

    def _clear_counts(self) -> None:
        self.slot_grid.reset_counts()
        self._master_count = 0
        self.master_count_label.setText("0")

    def reset_counts(self) -> None:
        """Zero the dashboard's counters and the run's package batches."""
        self._clear_counts()
        reset = getattr(self.run_controller, "reset_package_counts", None)
        if reset is not None:
            reset()
        self.set_status("Counters reset.")

    def reset_slot_count(self, slot: int) -> None:
        """Package mode: empty one bin and let it refill while the run continues."""
        reset = getattr(self.run_controller, "reset_package_slot", None)
        if reset is not None:
            reset(int(slot))
        self.slot_grid.reset_slot(int(slot))
        self.set_status(f"Reset counter for slot {slot}.")

    # ----- active-model mode --------------------------------------------------

    def _active_model(self) -> Any | None:
        if self.db is None:
            return None
        from ..data.repository import ModelRepo, SettingsRepo

        model_id = SettingsRepo(self.db).get_active_model_id()
        return ModelRepo(self.db).get(model_id) if model_id is not None else None

    def _apply_mode_visibility(self) -> None:
        """The mode inks the Train / AI Config pair. **Neither is ever hidden**
        (JL: a hidden activity is one nobody finds).

        At most one of the two is live — a trainable local model makes it
        Train, no active model or an active openai-mode model makes it AI
        Config (both classify over HTTP, and the page edits whichever config
        is in effect) — and a community model makes it neither. The other
        goes muted: still clickable, with the explainer behind it
        (train_page's and ai_page's unavailable panels) saying why and what
        to do.
        """
        from ..data.models import is_openai_model, is_trainable

        model = self._active_model()
        train_live = is_trainable(model)
        ai_live = model is None or is_openai_model(model)
        self._set_activity_unavailable("Train", not train_live)
        self._set_activity_unavailable(AI_CONFIG_ACTIVITY, not ai_live)
        self.sidebar_buttons["Train"].setToolTip(ACTIVITY_TOOLTIP_LIVE if train_live else TRAIN_TOOLTIP_MUTED)
        self.sidebar_buttons[AI_CONFIG_ACTIVITY].setToolTip(
            ACTIVITY_TOOLTIP_LIVE if ai_live else AI_CONFIG_TOOLTIP_MUTED
        )

    def _set_activity_unavailable(self, name: str, unavailable: bool) -> None:
        """Ink a sidebar button as "leads somewhere, but not usable right now".

        A dynamic property rather than ``setEnabled(False)``: the click has to
        keep working, since the explainer it opens is the whole point. Qt only
        re-reads a property-keyed rule on a re-polish, so ask for one.
        """
        button = self.sidebar_buttons[name]
        button.setProperty("unavailable", bool(unavailable))
        style = button.style()
        style.unpolish(button)
        style.polish(button)
        # The icon is a QIcon, which no stylesheet reaches — same split as
        # checked/unchecked (see _paint_sidebar_icon).
        self._paint_sidebar_icon(name)

    def _set_activity_visible(self, name: str, visible: bool) -> None:
        """Community only — it is the one activity that comes and goes (auth)."""
        button = self.sidebar_buttons[name]
        button.setVisible(visible)
        page = self._pages_by_name.get(name)
        if not visible and page is not None and self.pages.currentWidget() is page:
            self.sidebar_buttons["Sort"].setChecked(True)
            self.show_page("Sort")

    def _on_mode_changed(self) -> None:
        """The active model changed: everything scoped to it is re-read."""
        self._apply_mode_visibility()
        self._clear_counts()
        self._refresh_templates()
        self._refresh_sort_grid()
        self.ai_page.refresh_mode()
        # The server's policy and the version prompt belonged to the old model.
        self._clear_community_settings()
        self._refresh_notes_button()
        # Everything on the Train page is scoped to the active model.
        self.train_page.refresh()
        # The library's active marker is the mode, spelled out per row.
        self.models_page.refresh()
        # AI Config mode has no local device; a model switch re-evaluates it.
        self.refresh_device_indicator()

    # ----- theme --------------------------------------------------------------

    def _load_setting(self, key: str) -> Any:
        """Read a settings row, or None if there's no DB / it can't be read."""
        if self.db is None:
            return None
        try:
            from ..data.repository import SettingsRepo

            return SettingsRepo(self.db).get(key)
        except Exception:
            return None

    def _save_setting(self, key: str, value: Any) -> None:
        if self.db is None:
            return
        try:
            from ..data.repository import SettingsRepo

            SettingsRepo(self.db).set(key, value)
        except Exception:
            # A preference that can't be persisted still applies this session.
            pass

    @staticmethod
    def _bytes_to_setting(data: bytes) -> str:
        """Base64 text: SettingsRepo stores JSON-encoded values, not raw bytes."""
        return base64.b64encode(bytes(data)).decode("ascii")

    @staticmethod
    def _setting_to_bytes(value: Any) -> bytes | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return base64.b64decode(value.encode("ascii"))
        except (ValueError, TypeError):
            return None

    def _restore_window_state(self) -> None:
        """Dock layout + the model table's column widths, from the last session.

        The dock half is the manager's own XML state, not ``QMainWindow``'s —
        with QtAds the window has no QDockWidgets left to save. A blob written
        by the pre-QtAds build is simply not this format; ``restoreState``
        answers False and the panels keep their built-in homes, so the switch
        needs no migration.
        """
        state = self._setting_to_bytes(self._load_setting(SETTING_WINDOW_STATE))
        if state is not None:
            self.dock_manager.restoreState(QByteArray(state))
        columns = self._setting_to_bytes(self._load_setting(SETTING_MODELS_COLUMNS))
        if columns is not None:
            self.models_page.restore_header_state(columns)

    def _save_window_state(self) -> None:
        dock_state = bytes(self.dock_manager.saveState().data())
        self._save_setting(SETTING_WINDOW_STATE, self._bytes_to_setting(dock_state))
        self._save_setting(SETTING_MODELS_COLUMNS, self._bytes_to_setting(self.models_page.header_state()))

    def set_theme(self, name: str) -> None:
        """Switch palettes live and remember the choice."""
        resolved = resolve_theme(name)
        self._apply_theme(resolved)
        self._save_setting(SETTING_THEME, resolved)
        # Whichever picker was used, the other one follows — signals blocked,
        # so the follower never re-applies what just happened.
        self._sync_theme_pickers()

    def _apply_theme(self, name: str) -> None:
        self.theme_name = name
        self.palette_colors = THEMES[name]
        self.setStyleSheet(build_stylesheet(self.palette_colors))
        muted = f"color: {self.palette_colors['text_muted']};"
        for label in self._muted_labels:
            label.setStyleSheet(muted)
        # Colors baked into rich text / per-line paints need a hand re-render.
        self._paint_current_result()
        if hasattr(self, "sidebar_buttons"):
            self._paint_sidebar_icons()
        if hasattr(self, "serial_monitor"):
            self.serial_monitor.apply_palette()
        if hasattr(self, "history_view"):
            self.history_view.apply_palette()
        if hasattr(self, "models_page"):
            self.models_page.apply_palette()
        # Indicator dots carry state, not a palette role a stylesheet can reach.
        self._paint_indicators()

    # ----- status -------------------------------------------------------------

    def set_status(self, message: str) -> None:
        self.statusBar().showMessage(str(message))

    def _indicator_html(self, message: str, *, connected: bool) -> str:
        color = self.palette_colors["success" if connected else "error"]
        return f'<span style="color: {color};">●</span> {html.escape(str(message))}'

    def _paint_indicators(self) -> None:
        for label, (message, connected) in (
            (self.camera_label, self._camera_state),
            (self.serial_label, self._serial_state),
        ):
            label.setText(self._indicator_html(message, connected=connected))
        self._paint_preview_placeholder()
        # Every camera/serial connect-state change can flip the Sort page
        # between the guided empty state and the real dashboard.
        self._update_sort_empty_state()

    def _camera_placeholder_text(self) -> str:
        return f"{CAMERA_DEAD_TEXT} — {CAMERA_DEAD_LINK}"

    def _paint_preview_placeholder(self) -> None:
        """A camera that isn't connected says so where the feed would be."""
        if self._camera_state[1]:
            return  # frames are arriving (or about to); the timer owns the label
        # Painted before the Sort page exists on the very first indicator paint.
        label = getattr(self, "preview_label", None)
        if label is not None:
            label.setText(self._camera_placeholder_text())
            label.setCursor(Qt.CursorShape.PointingHandCursor)

    def _on_preview_clicked(self) -> None:
        if not self._camera_state[1]:
            self._open_settings_section("Camera")

    def _set_camera_indicator(self, message: str, *, connected: bool) -> None:
        self._camera_state = (message, connected)
        self._paint_indicators()

    def _set_serial_indicator(self, message: str, *, connected: bool) -> None:
        self._serial_state = (message, connected)
        self._paint_indicators()
        self.bus.post("serial/state", {"connected": connected, "message": message})

    def refresh_device_indicator(self) -> None:
        """Show where local classification runs, once that is known.

        Reads only the device `local_inference` has already picked — never
        imports torch, never triggers the probe or its benchmarks — so it is
        free and safe on the UI thread. Hidden until the first classify(),
        and in AI Config mode, where classification is an HTTP call and no
        local device is involved.
        """
        text: str | None = None
        if self.db is not None and classifier.uses_local_inference(self.db):
            text = local_inference.device_description()
        if text:
            self.device_label.setText(f"Inference: {text}")
            self.device_label.show()
        else:
            self.device_label.hide()

    def _warm_device_indicator(self) -> None:
        """Pick the inference device ahead of the first classify, off-thread.

        The status bar should say where classification will run without the
        user having to feed a case first. Guarded so an AI Config user never
        pays a torch import (the gate rule, §5): only when a local model is
        active and torch is already installed. `is_available()` imports torch
        and runs the device probe on the worker — the same work the first
        classify would have paid — and the on_done just repaints the label.
        Startup-only (behind ``auto_connect``): a model activated later gets
        its indicator from the first classification instead.
        """
        if self.db is None or not classifier.uses_local_inference(self.db):
            return
        if not local_inference.is_installed() or local_inference.device_description():
            return
        self.run_worker(
            local_inference.is_available,
            on_done=lambda _ok: self.refresh_device_indicator(),
            on_error=lambda _exc: None,
        )

    # ----- camera -------------------------------------------------------------

    def start_camera(self) -> None:
        try:
            if self.camera.start_preview():
                self._set_camera_indicator(
                    f"Camera: connected ({self.camera.width}x{self.camera.height})",
                    connected=True,
                )
            else:
                # The red dot alone left the user with nowhere to go (JL).
                self.bus.post("status", CAMERA_FAILED_STATUS)
                self._set_camera_indicator("Camera: failed to start", connected=False)
        except Exception as exc:
            self.bus.post("status", f"Camera error: {exc} — pick a device in Settings → Camera.")
            self._set_camera_indicator("Camera: error", connected=False)

    @staticmethod
    def frame_to_image(frame: np.ndarray) -> QImage:
        """Wrap a BGR numpy frame as a QImage.

        ``QImage`` borrows the buffer it is handed, so the copy is what cuts
        the result loose from a frame the grab thread is about to overwrite.
        """
        buffer = np.ascontiguousarray(frame)
        height, width = buffer.shape[:2]
        image = QImage(buffer.data, width, height, buffer.strides[0], QImage.Format.Format_BGR888)
        return image.copy()

    def _refresh_preview(self) -> None:
        # Hidden is the default; while hidden no frame is fetched or painted
        # (the camera's grab thread runs regardless).
        if not self.show_camera_check.isChecked():
            return
        frame = self.camera.latest_frame()
        if frame is None:
            return
        pixmap = QPixmap.fromImage(self.frame_to_image(frame))
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ----- serial -------------------------------------------------------------

    def _auto_connect_serial(self) -> None:
        """Try the saved port first, then walk the rest until one handshakes.

        Ports are probed on a worker (``try_open`` waits out the handshake);
        ``_after_connect`` is the shared tail with the Settings page's own
        Connect, so both build the run controller and push the init settings.
        """
        saved_port = (self.config.serial.get("port") or "").strip()
        if saved_port == EMULATED_PORT:
            broker = EmulatorBroker()
            broker.try_open()
            self._after_connect(broker, EMULATED_PORT)
            return

        available = serial_broker.list_serial_ports()
        candidates: list[str] = []
        # The saved port is always probed, even if the filter below would
        # skip it — the user chose it once, so it is not a guess.
        if saved_port and saved_port in available:
            candidates.append(saved_port)
        for port in available:
            if port not in candidates and serial_broker.is_probe_candidate(port):
                candidates.append(port)
        skipped = [p for p in available if p not in candidates]
        if skipped:
            self.bus.post(
                "serial/note",
                "skipping (Bluetooth/pseudo, connect manually from Settings → Serial): " + ", ".join(skipped),
            )

        if not candidates:
            self.set_status("No serial ports detected.")
            self._set_serial_indicator("Serial: no ports", connected=False)
            return

        baud = int(self.config.serial.get("baud", 9600))
        probe_timeout = float(self.config.serial.get("handshake_timeout_s", serial_broker.HANDSHAKE_READ_TIMEOUT_S))

        def _probe() -> tuple[Any, str] | tuple[None, None]:
            for port in candidates:
                self.bus.post("status", f"Auto-connect: probing {port}…")
                self.bus.post("serial/note", f"probing {port} @ {baud}…")
                broker = serial_broker.SerialBroker(
                    port=port,
                    baud=baud,
                    require_serial_ready=True,
                    handshake_timeout_s=probe_timeout,
                )
                # Listen *before* the handshake: whatever the board says to a
                # probe that fails is the only evidence of why it failed.
                self._attach_serial_listeners(broker)
                if broker.try_open():
                    broker.start()
                    return broker, port
                self.bus.post("serial/note", f"{port} did not handshake")
            return None, None

        self.set_status(f"Auto-connecting to serial — {len(candidates)} port(s) to try…")
        self.run_worker(
            _probe,
            on_done=self._finalize_auto_connect,
            on_error=lambda exc: self.set_status(f"Auto-connect error: {exc}"),
        )

    def _finalize_auto_connect(self, result: tuple[Any, str] | tuple[None, None]) -> None:
        broker, port = result
        if broker is None or port is None:
            self.set_status("No board responded on any port.")
            self._set_serial_indicator("Serial: no board found", connected=False)
            return
        self._after_connect(broker, port)

    def _attach_serial_listeners(self, broker: Any) -> None:
        """Fan a broker's traffic onto the bus, once — a second attach doubles every line."""
        if getattr(broker, "_bus_listeners_attached", False):
            return
        broker.on_received.append(lambda line: self.bus.post("serial/rx", line))
        broker.on_sent.append(lambda line: self.bus.post("serial/tx", line))
        # Fires on the reader thread (or a failed writer's), so it goes through
        # the bus like everything else rather than touching a widget directly.
        on_disconnect = getattr(broker, "on_disconnect", None)
        if isinstance(on_disconnect, list):
            on_disconnect.append(lambda reason: self.bus.post("serial/disconnected", reason))
        broker._bus_listeners_attached = True

    def _after_connect(self, broker: Any, port: str, *, source: str = "auto") -> None:
        """Shared tail of both connect paths: listeners, persistence, controller."""
        self._attach_serial_listeners(broker)
        self.broker = broker
        baud = int(getattr(broker, "baud", self.config.serial.get("baud", 9600)))
        if port != (self.config.serial.get("port") or "") or baud != int(self.config.serial.get("baud", 9600)):
            self.config.serial["port"] = port
            self.config.serial["baud"] = baud
            self.config.save()
        self._set_serial_indicator(
            f"Serial: connected ({port} @ {getattr(broker, 'baud', '?')}) — {broker.firmware_version}",
            connected=True,
        )
        self.set_status(f"{'Auto-connected' if source == 'auto' else 'Connected'} to {port}.")
        self._rebuild_run_controller()
        # Pushed from the shared connect tail, so auto-connect and the Settings
        # page behave alike.
        if self.config.serial.get("init_on_startup", False):
            settings = dict(self.config.serial.get("init_settings", {}))
            if settings:
                self.run_worker(
                    lambda: broker.update_init_settings(settings),
                    on_done=lambda _r: self.set_status(f"Connected to {port}. Init settings pushed."),
                    on_error=lambda err: self.set_status(f"Init push failed: {err}"),
                )

    def connect_serial(self, port: str | None = None) -> None:
        """Open one explicit port, chosen in Settings → Serial.

        Mirrors ``ui.app.MainWindow.connect_serial``: the emulator is opened
        inline (nothing blocks), a real port opens on a worker because
        ``try_open`` waits out the board's handshake.
        """
        if self.broker is not None:
            try:
                if self.run_controller is not None:
                    self.run_controller.stop()
                self.broker.stop()
            except Exception:
                pass
            self.broker = None
            self.run_controller = None
            self._update_run_buttons()

        if port is None:
            port = (self.config.serial.get("port") or "").strip()
        if not port:
            self.set_status("No port selected.")
            self._set_serial_indicator("Serial: no port selected", connected=False)
            return

        if port == EMULATED_PORT:
            broker: Any = EmulatorBroker()
            broker.try_open()
            self._after_connect(broker, port, source="manual")
            return

        baud = int(self.config.serial.get("baud", 9600))
        broker = serial_broker.SerialBroker(port=port, baud=baud, require_serial_ready=True)
        # As in the probe: a failed open should still leave a trace in the monitor.
        self._attach_serial_listeners(broker)
        self.set_status(f"Connecting to {port}…")

        def _open() -> bool:
            if not broker.try_open():
                return False
            broker.start()
            return True

        def _done(opened: bool) -> None:
            if not opened:
                self.set_status(f"Failed to open {port}.")
                self._set_serial_indicator(f"Serial: failed to open {port}", connected=False)
                return
            self._after_connect(broker, port, source="manual")

        self.run_worker(
            _open,
            on_done=_done,
            on_error=lambda exc: self.set_status(f"Connect error: {exc}"),
        )

    def _on_serial_disconnected(self, reason: Any = None) -> None:
        """The link died on its own: say so, drop the board, stop the run.

        Nothing here reconnects. The board's arm position and the wheel's
        pipeline are only knowable while the link is up, so recovery is an
        explicit reconnect (which re-handshakes through ``try_open``), not a
        silent one — see ``SERIAL_LOST_TEXT``.
        """
        detail = str(reason or "link lost")
        port = str(getattr(self.broker, "port", "") or self.config.serial.get("port") or "")
        self.bus.post("serial/note", f"disconnected — {detail}")
        was_running = self._is_running

        controller, self.run_controller = self.run_controller, None
        if controller is not None:
            controller.stop()
        broker, self.broker = self.broker, None
        if broker is not None:
            try:
                broker.stop()
            except Exception:
                pass

        self._set_serial_indicator(
            f"Serial: disconnected ({port})" if port else "Serial: disconnected",
            connected=False,
        )
        self._update_run_buttons()
        self.set_status(f"Serial disconnected — {detail}")
        if was_running:
            self.beep()
            # Same shape as the package halt: a modal, queued out of the drain
            # so it can't re-enter it.
            QTimer.singleShot(0, self, lambda: self.notify(SERIAL_LOST_TITLE, SERIAL_LOST_TEXT))

    # ----- run ----------------------------------------------------------------

    def _rebuild_run_controller(self) -> None:
        if self.broker is None:
            return
        self.run_controller = RunController(
            config=self.config,
            broker=self.broker,
            camera=self.camera,
            bus=self.bus,
            db=self.db,
        )
        # A fresh controller carries a fresh FeedbackService, so the server's
        # policy has to be re-installed on it.
        self._apply_community_settings()
        self._refresh_sort_grid()
        self._update_run_buttons()

    def notify(self, title: str, text: str) -> None:
        """User-facing warning. Tests patch this — never let a modal open there."""
        QMessageBox.warning(self, title, text)

    def _ai_credentials_missing(self) -> bool:
        """HTTP classification can't run without an API key and a model name.

        Scoped to the HTTP paths: a local model never touches the HTTP
        client, so an unset key there is no reason to refuse a run. An
        active openai-mode model is checked against **its own** config — the
        same one `classify_active` will use — never the app-level one.
        """
        from ..data.models import is_openai_model

        model = classifier.active_model(self.db)
        if is_openai_model(model):
            cfg = model.ai_model_config if model is not None else None
            return cfg is None or not (cfg.api_key and cfg.model)
        if classifier.uses_local_inference(self.db):
            return False
        api = self.config.api
        return not (api.get("api_key") and api.get("model"))

    def _ready_to_sort(self) -> RunController | None:
        """Preflight: board, moderator notes, AI config, checkpoint, torch.

        Each check is asked of the layer that owns the answer, so the Qt shell
        never re-derives the rule. Returns the controller when a run may start.
        An unacknowledged moderator note gates Start (issue #29, A25).
        """
        controller = self.run_controller
        if controller is None or self.broker is None:
            self.set_status("Connect to the board first (Settings → Serial).")
            return None
        from ..community.notes import unacknowledged

        if unacknowledged(self._stored_notes()):
            self.notify(NOTES_GATE_TITLE, NOTES_GATE_TEXT)
            return None
        if self._ai_credentials_missing():
            self.notify(
                "AI not configured",
                "Set the endpoint, API key and model on the AI Config page first.",
            )
            return None
        problem = classifier.checkpoint_problem(self.db)
        if problem is not None:
            self.notify("Model not ready", problem)
            return None
        if classifier.uses_local_inference(self.db) and not self.ensure_torch(
            self.start_run,
            reason="Sorting needs PyTorch",
            # The model decides block-vs-offer on an outdated torch, so the
            # gate needs to know whose checkpoint is about to be loaded.
            model=classifier.active_model(self.db),
        ):
            # The gate re-enters start_run after a successful install.
            return None
        return controller

    @staticmethod
    def _set_button_role(button: QPushButton, object_name: str) -> None:
        """Swap a button's palette role. QSS matches on the objectName, and a
        live widget has to be re-polished for the new rule to take."""
        if button.objectName() == object_name:
            return
        button.setObjectName(object_name)
        style = button.style()
        style.unpolish(button)
        style.polish(button)

    def toggle_run(self) -> None:
        """The one button's handler; the run state decides which half it is."""
        if self._is_running:
            self.stop_run()
        else:
            self.start_run()

    def start_run(self) -> None:
        controller = self._ready_to_sort()
        if controller is None or self._is_running:
            return
        self._refresh_sort_grid()
        controller.start()

    def stop_run(self) -> None:
        if self.run_controller is not None:
            self.run_controller.stop()
            self.set_status("Stopping…")

    def manual_feed(self) -> None:
        controller = self._ready_to_sort()
        if controller is None or self._is_running:
            return
        # One cycle blocks on the board; the bus carries the result back.
        self.run_worker(controller.cycle_once)

    def _on_run_started(self) -> None:
        # Counts survive Stop/Start on purpose: operators stop to clear a jam
        # and restart mid-tray. Only the explicit resets clear.
        self._set_running(True)

    def _on_run_stopped(self) -> None:
        self._set_running(False)
        # Terminal status, or whatever was in flight ("Stopping…",
        # "Classifying…") reads as stuck forever (Seth).
        self.set_status("Run stopped.")

    def _set_running(self, running: bool) -> None:
        self._is_running = running
        self._update_run_buttons()

    def _update_run_buttons(self) -> None:
        connected = self.broker is not None
        running = self._is_running
        # A run that outlives its board still has to be stoppable.
        self.run_button.setEnabled(connected or running)
        self.run_button.setText(RUN_STOP_TEXT if running else RUN_START_TEXT)
        self._set_button_role(self.run_button, "danger" if running else "action")
        self.action_buttons["Manual feed"].setEnabled(connected and not running)
        for button in (self.run_button, self.action_buttons["Manual feed"]):
            button.setToolTip("" if connected else "Connect to the board first")
        # The Train page's Feed drives the same board.
        train_page = getattr(self, "train_page", None)
        if train_page is not None:
            train_page.refresh_connection()

    def _on_run_result(self, result: Any) -> None:
        # `run/result` carries a slot even for a failed cycle, so `ok` is what
        # decides whether a case actually landed anywhere.
        if not isinstance(result, dict) or not result.get("ok"):
            return
        self.slot_grid.increment(int(result.get("slot") or 0))
        self._master_count += 1
        self.master_count_label.setText(str(self._master_count))

    def _on_run_history(self, payload: Any) -> None:
        """The current case: its crop and the call made on it. Nothing accumulates."""
        if not isinstance(payload, dict):
            return
        self._show_crop(payload.get("image"))
        confidence = float(payload.get("confidence", 0) or 0)
        floor = float(getattr(self.config, "run_confidence_floor", 0) or 0)
        label = str(payload.get("label") or "(empty)")
        parent = payload.get("parent")
        self._current_result = (
            f"{parent} · {label}" if parent else label,
            confidence,
            floor <= 0 or confidence >= floor,
        )
        self._paint_current_result()

    def _paint_current_result(self) -> None:
        """The confidence colour is baked in, so a theme switch re-runs this."""
        if self._current_result is None:
            self.result_label.setText(RESULT_EMPTY_TEXT)
            self.result_confidence_label.setText(RESULT_EMPTY_CONFIDENCE)
            self.result_confidence_label.setStyleSheet(f"color: {self.palette_colors['text_muted']};")
            return
        label, confidence, above_floor = self._current_result
        self.result_label.setText(label)
        self.result_confidence_label.setText(f"{confidence:.0f}%")
        color = self.palette_colors["success" if above_floor else "warning"]
        self.result_confidence_label.setStyleSheet(f"color: {color};")

    def _show_crop(self, image: Any) -> None:
        """The headstamp as the classifier saw it — the column's primary panel."""
        if not isinstance(image, np.ndarray) or image.size == 0:
            return
        self.crop_label.set_source(QPixmap.fromImage(self.frame_to_image(image)))

    def beep(self) -> None:
        """Non-blocking batch-complete tone. Best-effort — never fails a handler."""
        try:
            QApplication.beep()
        except Exception:
            pass

    def _on_package_full(self, payload: Any) -> None:
        data = payload if isinstance(payload, dict) else {}
        self.beep()
        self.set_status(f"Slot {data.get('slot')} batch full ({data.get('count')}). Reset it to refill.")

    def _on_package_halt(self, payload: Any) -> None:
        label = (payload or {}).get("label") if isinstance(payload, dict) else None
        self.beep()
        message = (
            f"Run stopped — every slot for “{label or '?'}” is full. "
            "Empty the bins, reset their counters, then Start again."
        )
        self.set_status(message)
        # Operators rely on the dialog here, not just the status line. Queued
        # single-shot: a modal straight from a bus handler re-enters the drain.
        QTimer.singleShot(0, self, lambda: self.notify("Package complete", message))

    # ----- worker dispatch ----------------------------------------------------

    def run_worker(
        self,
        fn: Callable[[], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run `fn` in a daemon thread and post the result back via the bus.

        Workers must never touch widgets; the drain timer delivers the result
        on the main thread.
        """
        # A monotonic token, never id(fn): CPython reuses freed addresses, so
        # two sequential workers can share an id — and with the subscriptions
        # left in place, the second worker's result would also be delivered to
        # the first worker's stale callback (a cartridge list arriving in a
        # username handler, in practice).
        token = next(self._worker_tokens)
        topic_done = f"worker/done/{token}"
        topic_err = f"worker/err/{token}"

        def _deliver_done(payload: Any) -> None:
            _unsubscribe()
            if on_done is not None:
                on_done(payload)

        def _deliver_err(exc: Any) -> None:
            _unsubscribe()
            if on_error is not None:
                on_error(exc)

        def _unsubscribe() -> None:
            self.bus.unsubscribe(topic_done, _deliver_done)
            self.bus.unsubscribe(topic_err, _deliver_err)

        self.bus.subscribe(topic_done, _deliver_done)
        self.bus.subscribe(topic_err, _deliver_err)

        def _run() -> None:
            try:
                self.bus.post(topic_done, fn())
            except Exception as exc:
                traceback.print_exc()
                self.bus.post(topic_err, exc)

        threading.Thread(target=_run, daemon=True).start()

    # ----- lifecycle ----------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        # No confirm-on-close while a run is active — stop the controller and
        # go, the way this app has always closed.
        # The timers first: a closed-but-not-destroyed window (every test
        # window, and the real one between close and quit) must go inert —
        # left running, each keeps draining its bus and repainting its
        # preview forever, and hundreds of those zombie ticks in one event
        # pump is what took an access violation on the Windows CI runner.
        for timer in (self._bus_timer, self._preview_timer):
            try:
                timer.stop()
            except Exception:
                pass
        try:
            if self.run_controller is not None:
                self.run_controller.stop()
        except Exception:
            pass
        try:
            if self.broker is not None:
                self.broker.stop()
        except Exception:
            pass
        try:
            self.camera.stop()
        except Exception:
            pass
        try:
            self._save_window_state()
        except Exception:
            # A preference that can't be persisted must never block shutdown.
            pass
        try:
            # A floated panel is its own top-level window parented to the dock
            # manager, so closing the main window leaves it on screen. QtAds's
            # own teardown call hides the manager and every floating container
            # together; without it the app "won't quit" with a panel torn off.
            self.dock_manager.hideManagerAndFloatingWidgets()
        except Exception:
            pass
        super().closeEvent(event)


def default_qpa_platform() -> None:
    """Linux: prefer XCB (XWayland) over native Wayland.

    A floated panel is frozen under native Wayland — JL hit this live
    (release a floating dock and it can no longer be moved or resized): a
    frameless top-level window can't ask a Wayland compositor to move/resize
    it, an upstream Qt/Wayland limitation, not something fixable in
    application code. It outlived the move to QtAds, whose floating
    containers are the same kind of top-level window, and XCB via XWayland
    still doesn't have the gap.

    ``setdefault`` so an explicit ``QT_QPA_PLATFORM`` (env, or a test's
    ``offscreen``) always wins. The semicolon list, never a bare ``"xcb"``:
    Qt tries entries left to right, so this still falls back to native
    Wayland on a box missing an XWayland dependency (JL hit
    ``libxcb-cursor0`` missing) instead of failing to start.
    """
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb;wayland")


def run_app(config: Any) -> int:
    default_qpa_platform()
    # Before the QApplication: Qt reads RESOURCE_NAME once, when it builds the
    # first window's WM_CLASS (desktop_integration's docstring says why).
    desktop_integration.prepare_process()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    desktop_integration.apply_identity()
    window = QtMainWindow(config)
    window.resize(1024, 768)
    window.show()
    # After the window is up: this rasterises icons through Qt, and nothing it
    # writes is needed to run. Best-effort by contract — see the module.
    desktop_integration.ensure()
    return app.exec()
