"""Login modal — kicks off the MSAL interactive flow."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from ..auth import AuthError, PortInUseError


class LoginDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, app: Any) -> None:
        super().__init__(parent)
        self.app = app
        self.title("Sign in")
        # wm_transient's stub wants a Wm (Tk/Toplevel), not the broader
        # Misc `parent` type; winfo_toplevel() resolves to the actual
        # top-level window, which is what Tk already does internally.
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frm,
            text=("Sign in to access community models.\n\nA browser window will open to complete the sign-in."),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Sign in", command=self._sign_in, style="Accent.TButton").pack(side=tk.RIGHT, padx=(0, 8))

    def _sign_in(self) -> None:
        def _work():
            return self.app.auth.login_interactive()

        def _ok(_result):
            self.app._mount_community_tab()
            messagebox.showinfo("Signed in", "Community tab is now available.", parent=self)
            self.destroy()

        def _fail(exc):
            if isinstance(exc, PortInUseError):
                messagebox.showerror(
                    "Port in use",
                    f"{exc}\n\nClose any other instance of the app and try again.",
                    parent=self,
                )
            elif isinstance(exc, AuthError):
                messagebox.showerror("Sign-in failed", str(exc), parent=self)
            else:
                messagebox.showerror("Sign-in failed", repr(exc), parent=self)

        self.app.run_worker(_work, on_done=_ok, on_error=_fail)
