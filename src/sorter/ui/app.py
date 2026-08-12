"""Main Tk application shell — notebook + status bar + cross-thread wiring."""

from __future__ import annotations

import threading
import time
import tkinter as tk
import traceback
from collections import deque
from collections.abc import Callable
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from .. import __version__
from ..control.events import EventBus
from ..control.run_controller import RunController
from ..data.config import Config
from ..hardware import serial_broker
from ..hardware.camera import Camera
from ..hardware.serial_emulator import EMULATED_PORT, EmulatorBroker
from .tab_ai import AiTab
from .tab_camera import CameraTab
from .tab_community import CommunityTab
from .tab_imageproc import ImageProcTab
from .tab_models import ModelsTab
from .tab_run import RunTab
from .tab_serial import SerialTab
from .tab_train import TrainTab
from .theme import (
    PALETTE,
    SETTING_CUSTOM_THEMES,
    SETTING_THEME,
    apply_theme,
    halftone_ink,
    load_custom_themes,
    paint_gradient,
    paint_halftone,
    resolve_theme,
    retheme_widgets,
    theme_names,
)
from .widgets import ScrollableFrame

PREVIEW_FPS = 20
HEADER_HEIGHT = 36
# Gap between the theme picker and the right edge of the title bar.
HEADER_PAD = 12
# Margin around the notebook. A screened theme gets a wider one, since that
# margin is the canvas its halftone prints on.
PAGE_MARGIN = 12
PAGE_MARGIN_TOP = 8
PAGE_MARGIN_SCREENED = 20
# Resting label of the status-bar update button, i.e. what it says when there
# is nothing waiting. Clicking it in that state runs an explicit check.
CHECK_FOR_UPDATES_LABEL = "Check for updates"
# Serial lines kept for a monitor window opened later. The interesting traffic
# is a failed auto-connect, which happens seconds before anyone can click.
SERIAL_BACKLOG_LINES = 2000


class MainWindow:
    def __init__(self, config: Config, db: Any | None = None) -> None:
        self.config = config
        self.db = db if db is not None else getattr(config, "db", None)
        self.bus = EventBus()
        self.root = tk.Tk()
        self.root.title(f"AI Case Sorter - v{__version__}")
        self.root.geometry("1024x768")
        self.root.minsize(960, 660)

        load_custom_themes(self._load_setting(SETTING_CUSTOM_THEMES))
        self.theme_name = resolve_theme(self._load_setting(SETTING_THEME))
        self.fonts = apply_theme(self.root, theme=self.theme_name)

        self.broker: Any | None = None
        # (kind, epoch seconds, line) — see SERIAL_BACKLOG_LINES.
        self.serial_backlog: deque[tuple[str, float, str]] = deque(maxlen=SERIAL_BACKLOG_LINES)
        self._serial_monitor: Any | None = None
        self.camera = Camera(
            device_index=int(config.camera.get("device_index", 0)),
            width=int(config.camera.get("width", 640)),
            height=int(config.camera.get("height", 480)),
        )
        self.run_controller: RunController | None = None

        # Gradient title bar at the top — the visible "gradient background"
        # of the modernised look. Painted on a Canvas because Tk widgets
        # can't render gradients directly.
        self.header_canvas = tk.Canvas(
            self.root,
            height=HEADER_HEIGHT,
            bg=PALETTE["bg_window"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.header_canvas.pack(side=tk.TOP, fill=tk.X)
        # Theme picker, parked in the top-right of the title bar. It rides on
        # the canvas as a window item so it floats over the gradient;
        # `_repaint_header` keeps it pinned to the right edge on resize.
        self.theme_var = tk.StringVar(value=self.theme_name)
        self.theme_combo = ttk.Combobox(
            self.header_canvas,
            textvariable=self.theme_var,
            values=theme_names(),
            state="readonly",
            style="Header.TCombobox",
            width=max(len(name) for name in theme_names()) + 1,
        )
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_selected)
        self._theme_window = self.header_canvas.create_window(
            0,
            HEADER_HEIGHT // 2,
            anchor=tk.E,
            window=self.theme_combo,
        )
        self.theme_new_button = ttk.Button(
            self.header_canvas,
            text="\u2699",
            width=2,
            style="HeaderIcon.TButton",
            takefocus=False,
            command=self.open_theme_editor,
        )
        self._theme_new_window = self.header_canvas.create_window(
            0,
            HEADER_HEIGHT // 2,
            anchor=tk.E,
            window=self.theme_new_button,
        )
        self.header_canvas.bind("<Configure>", self._repaint_header)

        # Status bar (must exist before tabs that call set_status).
        self.status_var = tk.StringVar(value="Idle.")
        self.serial_status_var = tk.StringVar(value="Serial: disconnected")
        self.camera_status_var = tk.StringVar(value="Camera: disconnected")
        status_bar = ttk.Frame(self.root, style="StatusBar.TFrame")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(status_bar, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)
        # Sign-in / sign-out button on the right side of the status bar.
        self.signin_var = tk.StringVar(value="Sign in")
        self.signin_button = ttk.Button(
            status_bar,
            textvariable=self.signin_var,
            command=self._on_signin_click,
        )
        self.signin_button.pack(side=tk.RIGHT, padx=12, pady=4)
        # Update affordance. Blue "update" hue per theme.py's colour rules.
        #
        # This button is always present, and that is the point: it used to be
        # packed only once a check had found something, which left a user who
        # was up to date with no route into the update dialog at all — so no
        # way to read release notes, and (since the version picker landed) no
        # way to reach the picker either. The label carries the state instead:
        # "Check for updates" when there is nothing to do, and the button
        # runs an explicit, non-silent check that opens the dialog whatever
        # the answer turns out to be.
        self._update_status_bar = status_bar
        self._update_info: Any | None = None
        self._pending_update: Any | None = None
        self.update_button_var = tk.StringVar(value=CHECK_FOR_UPDATES_LABEL)
        self.update_button = ttk.Button(
            status_bar,
            textvariable=self.update_button_var,
            style="Update.TButton",
            command=self._on_update_button_click,
        )
        self.update_button.pack(side=tk.RIGHT, padx=(0, 4), pady=4)
        # Pack serial first so it ends up rightmost; camera sits to its left.
        # Each indicator is a [dot][text] pair grouped in a sub-frame so the
        # dot stays glued to its label when the bar resizes.
        self._serial_connected = False
        self._camera_connected = False
        self.serial_dot = self._build_status_indicator(
            status_bar,
            self.serial_status_var,
            on_click=self.open_serial_monitor,
        )
        self.camera_dot = self._build_status_indicator(
            status_bar,
            self.camera_status_var,
            on_click=lambda: self.select_tab("Camera"),
        )
        # Packed last on purpose: when the bar runs out of room, pack starves
        # whatever was packed last, and a truncated transient message costs
        # less than a truncated connection state.
        ttk.Label(
            status_bar,
            textvariable=self.status_var,
            anchor=tk.W,
            style="Status.TLabel",
        ).pack(side=tk.LEFT, padx=12, pady=6)

        # The notebook rides on a backdrop canvas rather than being packed
        # straight into the root: the canvas owns the margin around it, which
        # is the one place a theme can print a halftone screen behind the UI
        # (ttk widgets always fill their own background — nothing shows
        # through them). `_layout_page` keeps the notebook inset and repaints.
        self.page = tk.Canvas(
            self.root,
            bg=PALETTE["bg_window"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.page.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        notebook = ttk.Notebook(self.page)
        self._notebook_window = self.page.create_window(
            0,
            0,
            window=notebook,
            anchor=tk.NW,
        )
        self._page_size = (0, 0)
        self.page.bind("<Configure>", self._layout_page)
        # Expose the notebook BEFORE constructing tabs so that tabs that
        # want to bind <<NotebookTabChanged>> on it (e.g. TrainTab) can
        # find it via `app.notebook`.
        self.notebook = notebook

        # Each tab sits inside a ScrollableFrame so that on small displays
        # (e.g. 1280x720) the content can scroll vertically rather than
        # being clipped beyond the window edge.
        def _add_scrolled(tab_cls, label):
            container = ScrollableFrame(notebook)
            tab = tab_cls(container.body, config=config, bus=self.bus, app=self)
            tab.pack(fill=tk.BOTH, expand=True)
            notebook.add(container, text=label)
            return tab

        self.run_tab = _add_scrolled(RunTab, "Run")
        self.models_tab = _add_scrolled(ModelsTab, "Models")
        self._train_tab_container = ScrollableFrame(notebook)
        self.train_tab = TrainTab(self._train_tab_container.body, config=config, bus=self.bus, app=self)
        self.train_tab.pack(fill=tk.BOTH, expand=True)
        notebook.add(self._train_tab_container, text="Train")
        # AI Config tab — always created, but its visibility tracks the
        # active runtime mode. The Notebook's `hide()` / `add()` methods
        # let us toggle the tab in/out of the tab bar without destroying it.
        self._ai_tab_container = ScrollableFrame(notebook)
        self.ai_tab = AiTab(self._ai_tab_container.body, config=config, bus=self.bus, app=self)
        self.ai_tab.pack(fill=tk.BOTH, expand=True)
        notebook.add(self._ai_tab_container, text="AI Config")
        self.imageproc_tab = _add_scrolled(ImageProcTab, "Image Processing")
        self.serial_tab = _add_scrolled(SerialTab, "Serial Config")
        self.camera_tab = _add_scrolled(CameraTab, "Camera")
        # Community tab is hidden until the user signs in.
        self.community_tab: CommunityTab | None = None
        self._community_container: ttk.Frame | None = None
        self._add_scrolled = _add_scrolled
        from ..community.auth import AuthManager

        try:
            self.auth: AuthManager | None = AuthManager()
        except Exception:
            self.auth = None
        if self.auth is not None and self.auth.is_authenticated():
            self._mount_community_tab()

        # Apply initial tab visibility for the AI Config + Train tabs.
        # AI Config shows in AI mode; Train shows in local-model mode.
        self._apply_ai_config_visibility()
        self._apply_train_tab_visibility()
        self.bus.subscribe("mode/changed", lambda _payload: self._apply_ai_config_visibility())
        self.bus.subscribe("mode/changed", lambda _payload: self._apply_train_tab_visibility())

        self.bus.subscribe("status", self.set_status)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_bus)
        self.root.after(int(1000 / PREVIEW_FPS), self._refresh_preview)

        # The Camera tab runs a full device/resolution probe on startup and
        # starts the preview once it picks the best resolution (see
        # CameraTab._auto_detect_on_startup). Starting it here too would
        # race with that probe trying to open the same device.
        # Auto-connect to the board once the UI is on screen.
        self.root.after(200, self._auto_connect_serial)
        # Update check runs well after startup so a slow or blocked network
        # never delays the window appearing or the board connecting.
        self.root.after(2500, self._check_for_updates_on_startup)

    # ----- software updates ---------------------------------------------------

    def _check_for_updates_on_startup(self) -> None:
        """Silent startup check. Never surfaces an error — only good news."""
        from ..update import updater

        try:
            # An already-staged update outranks a fresh check: what the user
            # needs is the restart prompt, not another download.
            pending = updater.pending_update()
        except Exception:
            pending = None
        if pending is not None:
            self.note_pending_update(pending)
            return

        if updater.checks_disabled() or not self._auto_check_enabled():
            return
        self.check_for_updates(silent=True)

    def _auto_check_enabled(self) -> bool:
        if self.db is None:
            return True
        try:
            from ..data.repository import SettingsRepo
            from ..update.updater import SETTING_CHECK_ON_STARTUP

            return bool(SettingsRepo(self.db).get(SETTING_CHECK_ON_STARTUP, True))
        except Exception:
            return True

    def check_for_updates(self, *, silent: bool = True) -> None:
        """Look for a newer release on a worker thread.

        `silent=True` (startup) only reveals the status-bar button on a hit;
        `silent=False` (explicit request) always opens the dialog so the user
        gets an answer either way.
        """
        from ..update import updater

        def _work() -> Any:
            return updater.check_for_update()

        def _done(info: Any) -> None:
            self._update_info = info
            if info is not None:
                self._show_update_button(f"Update to {info.version}")
                return
            # Nothing newer. Leave the button at its resting label rather than
            # a stale "Update to ..." from an earlier check, and only open the
            # dialog if the user asked — a startup check must stay silent.
            self._show_update_button(CHECK_FOR_UPDATES_LABEL)
            if not silent:
                self.set_status("You're up to date.")
                self.open_update_dialog()

        def _error(exc: Exception) -> None:
            if not silent:
                self.set_status(f"Update check failed: {exc}")

        if not silent:
            self.set_status("Checking for updates…")
        self.run_worker(_work, on_done=_done, on_error=_error)

    def _on_update_button_click(self) -> None:
        """Open the dialog if we already know about something, else go look.

        Once a check has found a release (or an update is staged), the button
        is a shortcut straight into the dialog. Before that it is the manual
        "check now" the app previously had no way to ask for, so it runs a
        non-silent check — which opens the dialog even when the answer is "you
        are up to date", because that is still an answer, and it is the way to
        reach the version picker.
        """
        if self._update_info is not None or self._pending_update is not None:
            self.open_update_dialog()
            return
        self.check_for_updates(silent=False)

    def note_pending_update(self, pending: Any) -> None:
        """Record a staged update and switch the button to the restart prompt."""
        self._pending_update = pending
        self._show_update_button("Restart to update")

    def _show_update_button(self, label: str) -> None:
        self.update_button_var.set(label)
        try:
            # `winfo_manager()` reports the geometry manager ("" until packed).
            # Deliberately not `winfo_ismapped()`, which is false whenever the
            # window is withdrawn or minimised and would re-pack every call.
            if not self.update_button.winfo_manager():
                self.update_button.pack(side=tk.RIGHT, padx=(0, 4), pady=4)
        except tk.TclError:
            pass

    def open_update_dialog(self) -> None:
        from .dialog_update import UpdateDialog

        UpdateDialog(
            self.root,
            info=self._update_info,
            app=self,
            pending=self._pending_update,
        )

    # ----- status -------------------------------------------------------------

    # ----- AI Config tab visibility (mode-gated) ------------------------------

    def _apply_ai_config_visibility(self) -> None:
        """Show the AI Config tab in AI-Config mode; hide it when a local model is active."""
        if self.db is None:
            return
        from ..data.repository import SettingsRepo

        active_id = SettingsRepo(self.db).get_active_model_id()
        try:
            if active_id is None:
                self.notebook.add(self._ai_tab_container, text="AI Config")
                # Re-add appends to the end; re-order so it lands after Train.
                self._restore_ai_config_position()
            else:
                self.notebook.hide(self._ai_tab_container)
        except tk.TclError:
            # `hide` on an already-hidden tab or `add` on an unknown widget
            # both raise — both are safe no-ops here.
            pass

    def _restore_ai_config_position(self) -> None:
        """Keep the AI Config tab between Train and Image Processing."""
        try:
            target_index = self.notebook.index(self._ai_tab_container)
            # Find the Train tab index; AI Config should follow it.
            tabs = self.notebook.tabs()
            train_index = None
            for i, tid in enumerate(tabs):
                if self.notebook.tab(tid, "text") == "Train":
                    train_index = i
                    break
            if train_index is not None and target_index != train_index + 1:
                self.notebook.insert(train_index + 1, self._ai_tab_container)
        except tk.TclError:
            pass

    def _apply_train_tab_visibility(self) -> None:
        """Show the Train tab only for a local model this user owns.

        Two things hide it. AI Config mode, because AI models can't be
        trained from this client (the inverse of
        `_apply_ai_config_visibility`). And a community download, because
        that model belongs to its publisher — see `models.is_trainable`.
        """
        if self.db is None:
            return
        from ..data.models import is_trainable
        from ..data.repository import ModelRepo, SettingsRepo

        active_id = SettingsRepo(self.db).get_active_model_id()
        active = ModelRepo(self.db).get(active_id) if active_id is not None else None
        try:
            if active_id is None or not is_trainable(active):
                self.notebook.hide(self._train_tab_container)
            else:
                self.notebook.add(self._train_tab_container, text="Train")
                # Re-add appends to the end; nudge it back to the position it
                # occupied at construction (after Models, before AI Config).
                self._restore_train_tab_position()
        except tk.TclError:
            pass

    def _restore_train_tab_position(self) -> None:
        """Keep the Train tab between Models and AI Config."""
        try:
            target_index = self.notebook.index(self._train_tab_container)
            tabs = self.notebook.tabs()
            models_index = None
            for i, tid in enumerate(tabs):
                if self.notebook.tab(tid, "text") == "Models":
                    models_index = i
                    break
            if models_index is not None and target_index != models_index + 1:
                self.notebook.insert(models_index + 1, self._train_tab_container)
        except tk.TclError:
            pass

    # ----- community tab (auth-gated) -----------------------------------------

    def _mount_community_tab(self) -> None:
        """Add the Community tab if not already present."""
        if self.community_tab is not None:
            return
        container = self._add_scrolled(CommunityTab, "Community")
        self.community_tab = container
        self.signin_var.set("Sign out")

    def _unmount_community_tab(self) -> None:
        """Remove the Community tab (called on logout)."""
        if self.community_tab is None:
            return
        for tab_id in self.notebook.tabs():
            if self.notebook.tab(tab_id, "text") == "Community":
                self.notebook.forget(tab_id)
                break
        self.community_tab = None
        self.signin_var.set("Sign in")

    def _on_signin_click(self) -> None:
        if self.auth is None:
            self.set_status("Authentication unavailable.")
            return
        if self.auth.is_authenticated():
            try:
                self.auth.logout()
            except Exception as exc:
                self.set_status(f"Sign-out failed: {exc}")
                return
            self._unmount_community_tab()
            self.set_status("Signed out.")
            return
        from .dialog_login import LoginDialog

        LoginDialog(self.root, self)

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _build_status_indicator(
        self,
        parent: tk.Misc,
        text_var: tk.StringVar,
        *,
        on_click: Callable[[], None] | None = None,
    ) -> tk.Label:
        """[●][text] pair on the right side of the status bar.

        Returns the dot Label so callers can recolour it (green/red) when
        the underlying connection state flips. With `on_click` the whole pair
        becomes a link — underlined on hover, since a bare label gives no hint
        that it does anything.
        """
        frame = ttk.Frame(parent, style="StatusBar.TFrame")
        frame.pack(side=tk.RIGHT, padx=12, pady=6)
        dot = tk.Label(
            frame,
            text="●",  # BLACK CIRCLE
            background=PALETTE["bg_window"],
            foreground=PALETTE["error"],
            font=self.fonts["small"],
        )
        dot.pack(side=tk.LEFT, padx=(0, 6))
        label = ttk.Label(frame, textvariable=text_var, style="Status.TLabel")
        label.pack(side=tk.LEFT)
        if on_click is not None:
            self._make_clickable(frame, dot, label, on_click)
        return dot

    def _make_clickable(
        self,
        frame: ttk.Frame,
        dot: tk.Label,
        label: ttk.Label,
        on_click: Callable[[], None],
    ) -> None:
        """Turn a status indicator into a link: hand cursor + hover underline."""
        rest_font = ttk.Style(self.root).lookup("Status.TLabel", "font") or self.fonts["small"]
        hover_font = tkfont.Font(root=self.root, font=rest_font)
        hover_font.configure(underline=True)
        for widget in (frame, dot, label):
            widget.configure(cursor="hand2")
            # Every part of the pair has to answer the click — the dot and the
            # gap between them are as clickable-looking as the text.
            widget.bind("<Button-1>", lambda _e: on_click())
            widget.bind("<Enter>", lambda _e: label.configure(font=hover_font))
            # Clear the override rather than restoring the captured font, so
            # the label goes back to following Status.TLabel — a widget-level
            # font would outlive the next theme switch.
            widget.bind("<Leave>", lambda _e: label.configure(font=""))

    def select_tab(self, title: str) -> None:
        """Bring the named notebook tab to the front (no-op if it isn't there)."""
        try:
            for tab_id in self.notebook.tabs():
                if self.notebook.tab(tab_id, "text") == title:
                    self.notebook.select(tab_id)
                    return
        except tk.TclError:
            pass

    def open_serial_monitor(self) -> None:
        """Open (or re-focus) the detachable serial monitor."""
        from .serial_monitor import SerialMonitorWindow

        window = self._serial_monitor
        if window is not None:
            try:
                if window.winfo_exists():
                    window.deiconify()
                    window.lift()
                    window.focus_set()
                    return
            except tk.TclError:
                pass
        self._serial_monitor = SerialMonitorWindow(self.root, app=self)

    def _set_camera_indicator(self, message: str, *, connected: bool) -> None:
        self.camera_status_var.set(message)
        self._camera_connected = connected
        self.camera_dot.config(
            foreground=PALETTE["success" if connected else "error"],
        )

    def _set_serial_indicator(self, message: str, *, connected: bool) -> None:
        self.serial_status_var.set(message)
        self._serial_connected = connected
        self.serial_dot.config(
            foreground=PALETTE["success" if connected else "error"],
        )
        # The monitor window mirrors this indicator; it may not be open.
        self.bus.post("serial/state", {"connected": connected, "message": message})

    def _refresh_status_indicators(self) -> None:
        """Re-apply the dot colors from the live palette (after a theme switch).

        The generic re-colouring pass can't tell a red dot from any other red,
        so the source of truth — connected or not — repaints them here.
        """
        for dot, connected in (
            (self.camera_dot, self._camera_connected),
            (self.serial_dot, self._serial_connected),
        ):
            dot.config(
                background=PALETTE["bg_window"],
                foreground=PALETTE["success" if connected else "error"],
            )

    # ----- header gradient ----------------------------------------------------

    def _repaint_header(self, _event=None) -> None:
        """Paint the gradient header, overlay the app title, pin the theme picker."""
        canvas = self.header_canvas
        canvas.configure(bg=PALETTE["bg_window"])
        paint_gradient(
            canvas,
            color_a=PALETTE["bg_gradient_a"],
            color_b=PALETTE["bg_gradient_b"],
            direction="horizontal",
        )
        # Themes that ask for it get a halftone screen over the gradient; for
        # the rest this clears the field and costs nothing. It fades in from
        # the right so the screen never lands under the app title.
        paint_halftone(
            canvas,
            color=halftone_ink(),
            fade_from="right",
            fade_span=max(1, canvas.winfo_width()) * 0.55,
        )
        canvas.delete("title")
        self._place_theme_picker()
        title_id = canvas.create_text(
            18,
            HEADER_HEIGHT // 2,
            anchor=tk.W,
            text="AI Case Sorter",
            fill=PALETTE["text"],
            font=self.fonts["title"],
            tags="title",
        )
        # Place the subtitle to the right of the actual rendered title text.
        bbox = canvas.bbox(title_id)
        subtitle_x = (bbox[2] + 12) if bbox else 200
        canvas.create_text(
            subtitle_x,
            HEADER_HEIGHT // 2 + 3,
            anchor=tk.W,
            text="Open Source Client",
            fill=PALETTE["text_muted"],
            font=self.fonts["small"],
            tags="title",
        )

    def _place_theme_picker(self) -> None:
        """Lay out [Theme] [dropdown] [+] against the right edge of the bar."""
        canvas = self.header_canvas
        combo = getattr(self, "theme_combo", None)
        if combo is None:
            return
        right = canvas.winfo_width() - HEADER_PAD
        canvas.coords(self._theme_new_window, right, HEADER_HEIGHT // 2)
        right -= self.theme_new_button.winfo_reqwidth() + 4
        canvas.coords(self._theme_window, right, HEADER_HEIGHT // 2)
        canvas.tag_raise(self._theme_window)
        canvas.tag_raise(self._theme_new_window)
        canvas.create_text(
            right - combo.winfo_reqwidth() - 8,
            HEADER_HEIGHT // 2,
            anchor=tk.E,
            text="Theme",
            fill=PALETTE["text_muted"],
            font=self.fonts["small"],
            tags="title",
        )

    # ----- page backdrop ------------------------------------------------------

    def _layout_page(self, event=None, *, force: bool = False) -> None:
        """Inset the notebook in its backdrop and screen the margin.

        The margin is what the halftone prints on, so themes that ask for dots
        get a wider one — enough to read as a comic panel border rather than
        padding that happens to have specks in it.
        """
        width = self.page.winfo_width()
        height = self.page.winfo_height()
        if width < 2 or height < 2:
            return
        if not force and (width, height) == self._page_size:
            return
        self._page_size = (width, height)

        ink = halftone_ink()
        margin = PAGE_MARGIN_SCREENED if ink else PAGE_MARGIN
        top = margin if ink else PAGE_MARGIN_TOP
        # No bottom margin without dots — the plain themes' notebook has always
        # run to the status bar, and an empty gap there would just look wrong.
        bottom = margin if ink else 0

        self.page.coords(self._notebook_window, margin, top)
        self.page.itemconfigure(
            self._notebook_window,
            width=max(1, width - 2 * margin),
            height=max(1, height - top - bottom),
        )

        bands = (
            ((0, 0, margin, height), "left"),
            ((width - margin, 0, width, height), "right"),
            ((0, 0, width, top), "top"),
            ((0, height - bottom, width, height), "bottom"),
        )
        for i, (box, edge) in enumerate(bands):
            paint_halftone(
                self.page,
                color=ink,
                box=box,
                fade_from=edge,
                fade_span=margin * 1.4,
                clear=(i == 0),
            )

    # ----- theme --------------------------------------------------------------

    def _load_setting(self, key: str):
        """Read a settings row, or None if there's no DB / it can't be read."""
        if self.db is None:
            return None
        try:
            from ..data.repository import SettingsRepo

            return SettingsRepo(self.db).get(key)
        except Exception:
            return None

    def _on_theme_selected(self, _event=None) -> None:
        self.set_theme(self.theme_var.get())
        # The dropdown keeps focus (and its highlight) after a pick; hand it
        # back so the title bar settles.
        self.root.focus_set()

    def set_theme(self, name: str) -> None:
        """Switch palettes live and remember the choice."""
        resolved = resolve_theme(name)
        previous = dict(PALETTE)
        self.theme_name = resolved
        self.theme_var.set(resolved)
        self.fonts = apply_theme(self.root, theme=resolved)
        # ttk widgets follow the restyled theme on their own; the classic Tk
        # widgets (and any open dialog's) need their baked-in colors remapped.
        retheme_widgets(self.root, previous)
        self._repaint_header()
        self._layout_page(force=True)
        self._refresh_status_indicators()
        # Text tags aren't in retheme_widgets' reach, so every serial console
        # repaints its own — the Serial tab's, and the monitor window's if open.
        for widget in (getattr(self.serial_tab, "console", None), self._serial_monitor):
            if widget is None:
                continue
            try:
                if widget.winfo_exists():
                    widget.apply_palette()
            except tk.TclError:
                pass
        self._save_setting(SETTING_THEME, resolved)

    def _save_setting(self, key: str, value) -> None:
        if self.db is None:
            return
        try:
            from ..data.repository import SettingsRepo

            SettingsRepo(self.db).set(key, value)
        except Exception:
            # A preference that can't be persisted still applies this session.
            pass

    def open_theme_editor(self) -> None:
        """Create a new theme from the active one, or edit a saved one."""
        from .dialog_theme_editor import ThemeEditorDialog

        ThemeEditorDialog(self.root, app=self)

    def refresh_theme_picker(self) -> None:
        """Re-read the theme list into the dropdown (after an edit or import)."""
        self.theme_combo.configure(values=theme_names())
        self.theme_var.set(self.theme_name)
        self._place_theme_picker()

    def save_custom_themes(self) -> None:
        """Persist every user-made theme (the editor calls this after a change)."""
        from .theme import custom_themes_payload

        self._save_setting(SETTING_CUSTOM_THEMES, custom_themes_payload())

    # ----- camera -------------------------------------------------------------

    def start_camera(self) -> None:
        try:
            ok = self.camera.start_preview()
            if not ok:
                self.set_status("Camera failed to start. Check device index in Camera tab.")
                self._set_camera_indicator("Camera: failed to start", connected=False)
            else:
                self._set_camera_indicator(
                    f"Camera: connected ({self.camera.width}x{self.camera.height})",
                    connected=True,
                )
        except Exception as exc:
            self.set_status(f"Camera error: {exc}")
            self._set_camera_indicator("Camera: error", connected=False)

    def stop_camera(self) -> None:
        self.camera.stop()
        self.set_status("Camera stopped.")
        self._set_camera_indicator("Camera: disconnected", connected=False)

    def restart_camera(
        self,
        device_index: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Recreate the Camera with the given (or saved) device + size."""
        self.camera.stop()
        cam_cfg = self.config.camera
        self.camera = Camera(
            device_index=int(device_index if device_index is not None else cam_cfg.get("device_index", 0)),
            width=int(width if width is not None else cam_cfg.get("width", 640)),
            height=int(height if height is not None else cam_cfg.get("height", 480)),
        )
        self.start_camera()
        if self.run_controller is not None:
            self.run_controller.camera = self.camera

    def capture_frame(self):
        return self.camera.capture_frame()

    def _refresh_preview(self) -> None:
        frame = self.camera.latest_frame()
        if frame is not None:
            self.ai_tab.update_preview(frame)
            self.camera_tab.refresh_preview(frame)
        self.root.after(int(1000 / PREVIEW_FPS), self._refresh_preview)

    # ----- serial -------------------------------------------------------------

    def _auto_connect_serial(self) -> None:
        """Try the saved port first, then walk available ports until one handshakes.

        Falls through to alternate ports if the saved port is present but
        unresponsive.
        """
        saved_port = (self.config.serial.get("port") or "").strip()
        if saved_port == EMULATED_PORT:
            self.connect_serial()
            return

        available = serial_broker.list_serial_ports()
        candidates: list[str] = []
        if saved_port and saved_port in available:
            candidates.append(saved_port)
        for port in available:
            if port not in candidates:
                candidates.append(port)

        if not candidates:
            self.set_status("No serial ports detected.")
            self._set_serial_indicator("Serial: no ports", connected=False)
            return

        baud = int(self.config.serial.get("baud", 9600))
        probe_timeout = float(self.config.serial.get("handshake_timeout_s", serial_broker.HANDSHAKE_READ_TIMEOUT_S))

        def _probe() -> tuple[object, str] | tuple[None, None]:
            for port in candidates:
                self.bus.post("status", f"Auto-connect: probing {port}…")
                self.bus.post("serial/note", f"probing {port} @ {baud}…")
                # Opening the port asserts DTR which resets the Arduino, and
                # the board needs ~1-2 s to boot before it can answer
                # `version`. Probe timeout is configurable in Serial Config.
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

    def _finalize_auto_connect(self, result) -> None:
        broker, port = result
        if broker is None:
            self.set_status("No board responded on any port.")
            self._set_serial_indicator("Serial: no board found", connected=False)
            return
        self._after_connect(broker, port, source="auto")

    def _attach_serial_listeners(self, broker: Any) -> None:
        """Fan a broker's traffic onto the bus, once.

        The auto-connect probe wires each candidate before its handshake, so
        the winner reaches `_after_connect` already listening — attaching a
        second time would double every line.
        """
        if getattr(broker, "_bus_listeners_attached", False):
            return
        broker.on_received.append(lambda line: self.bus.post("serial/rx", line))
        broker.on_sent.append(lambda line: self.bus.post("serial/tx", line))
        broker._bus_listeners_attached = True

    def _after_connect(self, broker, port: str, *, source: str) -> None:
        """Wire callbacks, persist the port, optionally push init settings.

        Shared by auto-connect and the manual Connect button.
        """
        self._attach_serial_listeners(broker)
        self.broker = broker
        if port != (self.config.serial.get("port") or ""):
            self.config.serial["port"] = port
            self.config.save()
        self._set_serial_indicator(
            f"Serial: connected ({port} @ {getattr(broker, 'baud', '?')}) — {broker.firmware_version}",
            connected=True,
        )
        self.set_status(f"{'Auto-connected' if source == 'auto' else 'Connected'} to {port}.")
        self._rebuild_run_controller()

        if self.config.serial.get("init_on_startup", False):
            self.set_status(f"Connected to {port}. Pushing init settings…")
            settings = dict(self.config.serial.get("init_settings", {}))
            self.run_worker(
                lambda: broker.update_init_settings(settings),
                on_done=lambda _r: self.set_status(f"Connected to {port}. Init settings pushed."),
                on_error=lambda err: self.set_status(f"Init push failed: {err}"),
            )

    def connect_serial(self, port: str | None = None) -> None:
        """Open a single, explicit port. If port is None, use the saved value.

        Run on the Tk main thread; the open is synchronous because the user
        clicked Connect and is waiting for the result.
        """
        if self.broker is not None:
            try:
                self.broker.stop()
            except Exception:
                pass
            self.broker = None

        if port is None:
            port = (self.config.serial.get("port") or "").strip()
        if not port:
            self.set_status("No port selected.")
            self._set_serial_indicator("Serial: no port selected", connected=False)
            return

        if port == EMULATED_PORT:
            broker = EmulatorBroker()
            broker.try_open()
        else:
            broker = serial_broker.SerialBroker(
                port=port,
                baud=int(self.config.serial.get("baud", 9600)),
                require_serial_ready=True,
            )
            # Same reason as the probe: a failed open should still leave a trace.
            self._attach_serial_listeners(broker)
            if not broker.try_open():
                self.set_status(f"Failed to open {port}.")
                self._set_serial_indicator(f"Serial: failed to open {port}", connected=False)
                return
            broker.start()

        self._after_connect(broker, port, source="manual")

    def disconnect_serial(self) -> None:
        if self.broker is not None:
            try:
                self.broker.stop()
            except Exception:
                pass
            self.broker = None
        self._set_serial_indicator("Serial: disconnected", connected=False)
        self.set_status("Serial disconnected.")

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

    # ----- worker dispatch ----------------------------------------------------

    def run_worker(
        self,
        fn: Callable[[], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run `fn` in a daemon thread and post the result back via the bus."""

        topic_done = f"worker/done/{id(fn)}"
        topic_err = f"worker/err/{id(fn)}"

        if on_done is not None:
            self.bus.subscribe(topic_done, on_done)
        if on_error is not None:
            self.bus.subscribe(topic_err, on_error)

        def _run() -> None:
            try:
                result = fn()
                self.bus.post(topic_done, result)
            except Exception as exc:
                traceback.print_exc()
                self.bus.post(topic_err, exc)

        threading.Thread(target=_run, daemon=True).start()

    # ----- bus drain loop -----------------------------------------------------

    def _drain_bus(self) -> None:
        self.bus.drain(max_items=128)
        # Pump serial-log events into the serial tab.
        self.root.after(50, self._drain_bus)

    # ----- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        # Fill the replay backlog now that the bus is alive. Displaying the
        # traffic is each SerialConsole's own subscription, not this.
        for kind in ("rx", "tx", "note"):
            self.bus.subscribe(f"serial/{kind}", lambda line, kind=kind: self._log_serial(kind, line))
        self.root.mainloop()

    def _log_serial(self, kind: str, line: str) -> None:
        """Keep one serial line for a monitor window opened later."""
        self.serial_backlog.append((kind, time.time(), str(line)))

    def _on_close(self) -> None:
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
        self.root.destroy()
