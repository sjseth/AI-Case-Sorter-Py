"""Modal training-progress window.

Spawned by `tab_train.py` when the user clicks Train. Subscribes to the
`training/*` bus topics and updates its progress label + console as the
subprocess emits markers. Closes automatically on terminal events
(`training/done`, `training/failed`, `training/cancelled`).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any

from ..events import EventBus
from .theme import PALETTE


class TrainingProgressDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        bus: EventBus,
        *,
        on_cancel: Callable[[], None],
        on_closed: Callable[[dict[str, Any] | None], None] | None = None,
        title: str = "Training progress",
    ) -> None:
        super().__init__(parent)
        self.bus = bus
        self.on_cancel = on_cancel
        self.on_closed = on_closed
        self.title(title)
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.configure(bg=PALETTE["bg_surface"])
        self.geometry("720x420")
        self.protocol("WM_DELETE_WINDOW", self._user_close)

        wrap = ttk.Frame(self, padding=12)
        wrap.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="Starting…")
        ttk.Label(wrap, textvariable=self.status_var, style="Header.TLabel").pack(side=tk.TOP, anchor="w")

        console_wrap = ttk.Frame(wrap, style="Card.TFrame", padding=4)
        console_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(8, 8))
        self.console = tk.Text(
            console_wrap,
            height=14,
            wrap=tk.NONE,
            bg=PALETTE["bg_input"],
            fg=PALETTE["text"],
            insertbackground=PALETTE["accent"],
            highlightthickness=0,
            borderwidth=0,
            state=tk.DISABLED,
        )
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Scrollbar(console_wrap, orient=tk.VERTICAL, command=self.console.yview).pack(side=tk.RIGHT, fill=tk.Y)
        self.console.config(yscrollcommand=lambda *a: None)  # detached scrollbar

        btns = ttk.Frame(wrap)
        btns.pack(side=tk.BOTTOM, fill=tk.X)
        self._cancel_btn = ttk.Button(btns, text="Cancel training", command=self._cancel, style="Danger.TButton")
        self._cancel_btn.pack(side=tk.LEFT)
        self._close_btn = ttk.Button(btns, text="Close", command=self._user_close, state=tk.DISABLED)
        self._close_btn.pack(side=tk.RIGHT)

        # Bus subscriptions — we collect (topic, handler) pairs so we can
        # unsubscribe on close.
        self._final_payload: dict[str, Any] | None = None
        self._unsub: list[Callable[[], None]] = []
        self._subscribe("training/start", self._on_start)
        self._subscribe("training/epoch", self._on_epoch)
        self._subscribe("training/log", self._on_log)
        self._subscribe("training/error", self._on_error)
        self._subscribe("training/done", self._on_done)
        self._subscribe("training/failed", self._on_failed)
        self._subscribe("training/cancelled", self._on_cancelled)

    def _subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        self.bus.subscribe(topic, handler)
        # Best-effort unsubscribe: EventBus may not expose one. We mark the
        # dialog as "destroyed" so stale events are dropped.

    # ----- bus handlers -------------------------------------------------------

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _append(self, text: str) -> None:
        if not self._alive():
            return
        self.console.config(state=tk.NORMAL)
        self.console.insert(tk.END, text)
        self.console.see(tk.END)
        self.console.config(state=tk.DISABLED)

    def _on_start(self, payload: dict[str, Any]) -> None:
        if not self._alive():
            return
        self.status_var.set(
            f"Training {payload.get('model', '')} — "
            f"epochs={payload.get('epochs')} classes={payload.get('classes')} "
            f"images={payload.get('images')} device={payload.get('device')}"
        )
        self._append(f"[start] {payload}\n")

    def _on_epoch(self, payload: dict[str, Any]) -> None:
        if not self._alive():
            return
        ep = payload.get("epoch")
        tot = payload.get("total")
        tl = payload.get("train_loss")
        ta = payload.get("train_acc")
        va = payload.get("val_acc")
        bits = [
            f"Epoch {ep}/{tot}",
            f"train_loss={tl:.4f}" if isinstance(tl, (int, float)) else "",
            f"train_acc={ta:.4f}" if isinstance(ta, (int, float)) else "",
            f"val_acc={va:.4f}" if isinstance(va, (int, float)) else "",
        ]
        self.status_var.set("  ".join(b for b in bits if b))
        self._append(f"[epoch] {payload}\n")

    def _on_log(self, line: str) -> None:
        if line:
            self._append(line + "\n")

    def _on_error(self, line: str) -> None:
        if line:
            self._append(f"[err] {line}\n")

    def _on_done(self, payload: dict[str, Any]) -> None:
        self._terminal("Done", payload)

    def _on_failed(self, payload: dict[str, Any]) -> None:
        self._terminal(f"Failed (rc={payload.get('return_code')})", payload)

    def _on_cancelled(self, payload: dict[str, Any]) -> None:
        self._terminal("Cancelled.", payload)

    def _terminal(self, label: str, payload: dict[str, Any]) -> None:
        if not self._alive():
            return
        self._final_payload = payload
        self.status_var.set(label)
        self._append(f"[{label.lower()}] {payload}\n")
        self._cancel_btn.config(state=tk.DISABLED)
        self._close_btn.config(state=tk.NORMAL)

    # ----- buttons ------------------------------------------------------------

    def _cancel(self) -> None:
        self._cancel_btn.config(state=tk.DISABLED)
        self.status_var.set("Cancel requested…")
        self.on_cancel()

    def _user_close(self) -> None:
        if callable(self.on_closed):
            self.on_closed(self._final_payload)
        try:
            self.destroy()
        except tk.TclError:
            pass
