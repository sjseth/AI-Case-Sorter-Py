"""Render a theme palette (``ui/palettes.py``) as a Qt stylesheet.

Every color comes from the palette argument; nothing here is hardcoded, so a
theme added or edited in the theme editor themes the whole shell.

Key roles are documented in ``ui/palettes.py``'s ``_DARK`` dict.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

# A QGroupBox's frame starts this far below its top edge — the title sits in
# that margin. A splitter dividing two group boxes offsets its handle by the
# same amount (`#groupSplitter` below), or the divider runs above the borders
# it divides.
GROUPBOX_TOP_MARGIN = 10

# The check/radio indicator, in px. Small enough to sit inside a label's line
# box, large enough to read as a control.
INDICATOR_SIZE = 14

# Only these roles are read, so a palette that is missing one (a hand-edited
# settings row) falls back rather than raising mid-stylesheet.
_FALLBACK = {
    "bg_window": "#131313",
    "bg_surface": "#1c1c1c",
    "bg_card": "#272727",
    "bg_card_hover": "#333333",
    "bg_card_sel": "#474747",
    "bg_input": "#0b0b0b",
    "bg_gradient_a": "#2f2f2f",
    "bg_gradient_b": "#0c0c0c",
    "border": "#3a3a3a",
    "border_focus": "#8f8f8f",
    "text": "#d4d4d4",
    "text_highlight": "#ffffff",
    "text_muted": "#9a9a9a",
    "text_subtle": "#6f6f6f",
    "text_inverse": "#121212",
    "accent": "#e0e0e0",
    "accent_dim": "#2e2e2e",
    "update": "#60a5fa",
    "update_hover": "#93c5fd",
    "update_press": "#3b82f6",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "action": "#22c55e",
    "action_hover": "#4ade80",
    "action_press": "#16a34a",
    "danger": "#ef4444",
    "danger_hover": "#f87171",
    "danger_press": "#dc2626",
}


def unavailable_ink(palette: dict[str, str]) -> str:
    """The mode pair's muted ink: text_subtle pulled 45% toward the window.

    text_subtle alone reads as merely unfocused, not inactive (JL: a muted
    AI Config still looked available). One function inks both the QSS text
    rule and the painted icon (app._paint_sidebar_icon), so they can't drift.
    """
    a = QColor(palette.get("text_subtle", _FALLBACK["text_subtle"]))
    b = QColor(palette.get("bg_window", _FALLBACK["bg_window"]))
    t = 0.45
    mixed = QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )
    return mixed.name()


def build_stylesheet(palette: dict[str, str]) -> str:
    """QSS for one palette: window, sidebar, menus, QtAds panels, buttons, fields, status bar."""
    c = {**_FALLBACK, **{k: v for k, v in palette.items() if isinstance(v, str)}}
    return f"""
QMainWindow, QWidget {{
    background-color: {c["bg_window"]};
    color: {c["text"]};
}}

/* Unstyled, this is a few px of plain background — indistinguishable from
   the universal QWidget fill above, so a dock edge had nothing to grab. */
QMainWindow::separator {{
    background-color: {c["border"]};
    width: 5px;
    height: 5px;
}}
QMainWindow::separator:hover {{ background-color: {c["border_focus"]}; }}

/* The three label colors below are also what inks the activity icons — a
   stylesheet can't reach a QIcon, so app._paint_sidebar_icon renders them
   from the same three roles. Change one here and change it there. */
#sidebar {{ background-color: {c["bg_surface"]}; }}
#sidebar QToolButton {{
    background: transparent;
    color: {c["text_muted"]};
    border: none;
    border-radius: 3px;
    padding: 8px 2px;
}}
#sidebar QToolButton:hover {{ background-color: {c["bg_card_hover"]}; }}
#sidebar QToolButton:checked {{
    background-color: {c["bg_card_sel"]};
    color: {c["text_highlight"]};
}}
/* "Leads somewhere, but not usable in this mode" — Train with no trainable
   model active. Deliberately NOT :disabled: the click still works, and the
   page it opens is what explains the state (app._set_activity_unavailable).
   The :checked twin is spelled out because the rule above would otherwise
   out-specify this one while the muted activity is the open page. */
#sidebar QToolButton[unavailable="true"],
#sidebar QToolButton[unavailable="true"]:checked {{ color: {unavailable_ink(c)}; }}
/* Splits the always-live surfaces from the Train / AI Config mode pair. */
#sidebarSeparator {{ background-color: {c["border"]}; margin: 4px 6px; }}
QMenuBar {{
    background-color: {c["bg_surface"]};
    color: {c["text"]};
}}
QMenuBar::item {{ background: transparent; padding: 4px 10px; }}
QMenuBar::item:selected {{ background-color: {c["bg_card_sel"]}; }}
QMenu {{
    background-color: {c["bg_card"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
}}
QMenu::item {{ padding: 4px 22px; }}
QMenu::item:selected {{
    background-color: {c["bg_card_sel"]};
    color: {c["text_highlight"]};
}}
QMenu::separator {{ height: 1px; background-color: {c["border"]}; margin: 4px 0; }}

/* Qt Advanced Docking System. QtAds ships its own stylesheet; app.py turns it
   off (DisableStylesheet) so these rules are the only thing painting panels,
   which is what makes them follow a theme switch. Selectors are QtAds's own
   C++ class names — `ads::CDockWidgetTab` is spelled `ads--CDockWidgetTab` in
   QSS, since `::` there means a subcontrol. Flat and palette-only, same
   discipline as the rest of this sheet. */
ads--CDockManager, ads--CDockContainerWidget, ads--CFloatingDockContainer {{
    background-color: {c["bg_window"]};
}}
ads--CDockWidget {{
    background-color: {c["bg_window"]};
    color: {c["text"]};
    border-color: {c["border"]};
}}
ads--CDockAreaWidget {{
    background-color: {c["bg_window"]};
}}
ads--CDockAreaTitleBar {{
    background-color: {c["bg_surface"]};
    border-bottom: 1px solid {c["border"]};
    padding: 0px;
}}

/* The tab is the drag handle, so the active one has to read as selected. */
ads--CDockWidgetTab {{
    background-color: {c["bg_surface"]};
    color: {c["text_muted"]};
    border: none;
    padding: 5px 10px;
}}
ads--CDockWidgetTab:hover {{ background-color: {c["bg_card_hover"]}; }}
ads--CDockWidgetTab[activeTab="true"] {{
    background-color: {c["bg_card_sel"]};
    color: {c["text_highlight"]};
}}
ads--CDockWidgetTab QLabel {{ color: {c["text_muted"]}; background: transparent; }}
ads--CDockWidgetTab[activeTab="true"] QLabel {{ color: {c["text_highlight"]}; }}

/* The splitter between panels: unstyled it is plain background, so a panel
   edge has nothing to grab (same reason QMainWindow::separator was styled). */
ads--CDockSplitter::handle {{ background-color: {c["border"]}; }}
ads--CDockSplitter::handle:hover {{ background-color: {c["border_focus"]}; }}

/* The drop-indicator overlay — the affordance QtAds is here for. It paints
   itself from the widget palette, not QSS; all it needs from us is to be
   spared the universal QWidget fill at the top of this sheet, which would
   otherwise put an opaque slab over the panel being dropped onto. */
ads--CDockOverlay, ads--CDockOverlayCross {{ background: transparent; }}

/* Title-bar and tab buttons. These carry no icon of their own — QtAds sets
   them from its resources in the stylesheet we disabled, so turning that off
   without restoring these leaves every close/undock button blank. The `:/ads`
   resources are compiled into the library and always available. */
#tabsMenuButton {{
    qproperty-icon: url(:/ads/images/tabs-menu-button.svg);
    qproperty-iconSize: 16px;
}}
#dockAreaCloseButton, #tabCloseButton {{
    qproperty-icon: url(:/ads/images/close-button.svg),
        url(:/ads/images/close-button-disabled.svg) disabled;
    qproperty-iconSize: 16px;
}}
#detachGroupButton {{
    qproperty-icon: url(:/ads/images/detach-button.svg),
        url(:/ads/images/detach-button-disabled.svg) disabled;
    qproperty-iconSize: 16px;
}}
#tabsMenuButton, #dockAreaCloseButton, #tabCloseButton, #detachGroupButton {{
    background: transparent;
    border: none;
    padding: 2px;
}}
#tabsMenuButton:hover, #dockAreaCloseButton:hover,
#tabCloseButton:hover, #detachGroupButton:hover {{
    background-color: {c["bg_card_hover"]};
    border-radius: 3px;
}}

/* A torn-off panel is its own window: it gets the title bar treatment too. */
ads--CFloatingWidgetTitleBar {{
    background-color: {c["bg_surface"]};
    qproperty-maximizeIcon: url(:/ads/images/maximize-button.svg);
    qproperty-normalIcon: url(:/ads/images/restore-button.svg);
}}
#floatingTitleCloseButton {{
    qproperty-icon: url(:/ads/images/close-button.svg);
    qproperty-iconSize: 16px;
    border: none;
    margin: 3px;
}}
#floatingTitleMaximizeButton {{
    qproperty-iconSize: 16px;
    border: none;
    margin: 3px;
}}

QPlainTextEdit#serialLog {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
}}

QListWidget {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
}}
QListWidget::item {{ padding: 5px 8px; }}
QListWidget::item:selected {{
    background-color: {c["bg_card_sel"]};
    color: {c["text_highlight"]};
}}

QTreeWidget#modelTable, QTreeWidget#headstampTable {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
}}
QTreeWidget#modelTable::item, QTreeWidget#headstampTable::item {{ padding: 4px 6px; }}
QTreeWidget#modelTable::item:selected, QTreeWidget#headstampTable::item:selected {{
    background-color: {c["bg_card_sel"]};
    color: {c["text_highlight"]};
}}
QTreeWidget#modelTable QHeaderView::section, QTreeWidget#headstampTable QHeaderView::section {{
    background-color: {c["bg_card"]};
    color: {c["text_muted"]};
    border: none;
    border-bottom: 1px solid {c["border"]};
    padding: 5px 6px;
}}
/* Per-row action buttons: the row is the item's own background, and a
   toolbar-sized button would make every row that tall. */
QWidget#rowActions {{ background: transparent; }}
QTreeWidget#modelTable QPushButton {{ padding: 2px 8px; }}

QFrame#slotCard {{
    background-color: {c["bg_card"]};
    border: 1px solid {c["border"]};
    border-radius: 6px;
}}
/* The whole card is a click target, so it lights up like one. */
QFrame#slotCard:hover {{ background-color: {c["bg_card_hover"]}; }}
/* The card's background would otherwise cascade into its labels' own fills. */
QFrame#slotCard QLabel {{ background: transparent; }}
QLabel#slotTitle {{ color: {c["text"]}; font-weight: bold; }}
QLabel#slotCount {{ color: {c["text_highlight"]}; font-size: 22px; font-weight: bold; }}
QLabel#slotPackage {{ color: {c["text_muted"]}; }}
QLabel#slotNames {{ color: {c["text_muted"]}; }}
QLabel#slotEdit {{ color: {c["text_subtle"]}; }}

QLabel#masterCount {{ color: {c["text_highlight"]}; font-size: 20px; font-weight: bold; }}
QLabel#cropPanel {{
    background-color: {c["bg_input"]};
    color: {c["text_muted"]};
    border: 1px solid {c["border"]};
}}
/* The Sort column's current-case line. The confidence colour is state (above
   or below the floor), so _paint_current_result sets it per widget. */
QLabel#currentHeadstamp {{ color: {c["text_highlight"]}; font-size: 18px; font-weight: bold; }}
QLabel#currentConfidence {{ font-size: 16px; font-weight: bold; }}

QDialog {{ background-color: {c["bg_window"]}; }}
QLabel#dialogHint, QLabel#rowHint {{ color: {c["text_muted"]}; }}
QCheckBox, QRadioButton {{ background: transparent; color: {c["text"]}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c["text_subtle"]}; }}
/* Left to the platform style these all but vanish on a dark surface, and a
   checkbox then reads as a plain label. Checked is a solid `action` fill —
   the same "on/go" role the primary buttons carry. */
QCheckBox::indicator, QRadioButton::indicator {{
    width: {INDICATOR_SIZE}px;
    height: {INDICATOR_SIZE}px;
    border: 1px solid {c["border_focus"]};
    background-color: {c["bg_input"]};
}}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: {INDICATOR_SIZE // 2 + 1}px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c["accent"]}; }}
/* Before :checked, which shares its specificity: a disabled toggle that is ON
   must still read as on, so the later rule has to be the checked one. */
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {c["border"]};
    background-color: {c["bg_surface"]};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {c["action"]};
    border-color: {c["action"]};
}}
QSpinBox, QDoubleSpinBox {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 3px;
    padding: 3px 4px;
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c["border_focus"]}; }}

QLabel#mutedLabel {{ color: {c["text_muted"]}; }}
QLabel#updateTitle {{ color: {c["text_highlight"]}; font-weight: bold; }}
QLabel#updateVersion {{ color: {c["accent"]}; }}

QTextBrowser {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 3px;
    padding: 6px;
}}
QLabel#aiResultLabel {{ color: {c["text_highlight"]}; font-weight: bold; }}

QFrame#thumbTile {{ border: 1px solid transparent; border-radius: 4px; }}
QFrame#thumbTile:hover {{ background-color: {c["bg_card_hover"]}; }}
QFrame#thumbTile[selected="true"] {{ border: 2px solid {c["accent"]}; }}
QFrame#thumbTile QLabel {{ background: transparent; }}
QLabel#thumbImage {{ background-color: {c["bg_input"]}; border-radius: 3px; }}
QLabel#thumbCaption {{ color: {c["text_muted"]}; font-size: 8pt; }}
QLabel#imagePreview {{
    background-color: {c["bg_input"]};
    border: 1px solid {c["border"]};
    border-radius: 4px;
}}

QPlainTextEdit {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 3px;
}}
QPlainTextEdit:focus {{ border-color: {c["border_focus"]}; }}

QGroupBox {{
    border: 1px solid {c["border"]};
    border-radius: 4px;
    margin-top: {GROUPBOX_TOP_MARGIN}px;
    color: {c["text"]};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {c["text_muted"]};
}}

QSlider::groove:horizontal {{
    background-color: {c["bg_input"]};
    height: 4px;
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background-color: {c["accent"]};
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}

QProgressBar {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 3px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {c["action"]}; border-radius: 2px; }}

QSplitter::handle {{ background-color: {c["border"]}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
/* A splitter between two group boxes: the margin drops the painted handle to
   where their frames actually start (the widget itself is unchanged, so the
   grab area keeps its full height). */
QSplitter#groupSplitter::handle:horizontal {{ margin-top: {GROUPBOX_TOP_MARGIN}px; }}

QToolTip {{
    background-color: {c["bg_card"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
}}

QTabWidget::pane {{
    background-color: {c["bg_surface"]};
    border: 1px solid {c["border"]};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {c["bg_card"]};
    color: {c["text_muted"]};
    border: 1px solid {c["border"]};
    border-bottom: none;
    padding: 6px 16px;
    margin-right: 2px;
}}
QTabBar::tab:hover {{ background-color: {c["bg_card_hover"]}; }}
QTabBar::tab:selected {{
    background-color: {c["bg_card_sel"]};
    color: {c["text"]};
}}

QPushButton {{
    background-color: {c["accent_dim"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 3px;
    padding: 5px 14px;
}}
QPushButton:hover {{ background-color: {c["bg_card_hover"]}; }}
QPushButton:disabled {{ color: {c["text_muted"]}; }}

QPushButton#action {{
    background-color: {c["action"]};
    color: {c["text_inverse"]};
    border-color: {c["action"]};
}}
QPushButton#action:hover {{
    background-color: {c["action_hover"]};
    border-color: {c["action_hover"]};
}}
QPushButton#action:pressed {{
    background-color: {c["action_press"]};
    border-color: {c["action_press"]};
}}

QPushButton#update {{
    background-color: {c["update"]};
    color: {c["text_inverse"]};
    border-color: {c["update"]};
}}
QPushButton#update:hover {{
    background-color: {c["update_hover"]};
    border-color: {c["update_hover"]};
}}
QPushButton#update:pressed {{
    background-color: {c["update_press"]};
    border-color: {c["update_press"]};
}}

QPushButton#danger {{
    background-color: {c["danger"]};
    color: {c["text_inverse"]};
    border-color: {c["danger"]};
}}
QPushButton#danger:hover {{
    background-color: {c["danger_hover"]};
    border-color: {c["danger_hover"]};
}}
QPushButton#danger:pressed {{
    background-color: {c["danger_press"]};
    border-color: {c["danger_press"]};
}}

QComboBox, QLineEdit {{
    background-color: {c["bg_input"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 3px;
    padding: 4px 8px;
    selection-background-color: {c["bg_card_sel"]};
    selection-color: {c["text"]};
}}
QComboBox:focus, QLineEdit:focus {{ border: 1px solid {c["border_focus"]}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {c["bg_card"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    selection-background-color: {c["bg_card_sel"]};
    selection-color: {c["text"]};
}}

QStatusBar {{
    background-color: {c["bg_window"]};
    color: {c["text_muted"]};
}}
QStatusBar::item {{ border: none; }}
"""
