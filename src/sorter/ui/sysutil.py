"""Small cross-platform desktop helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_path(path: Path | str) -> bool:
    """Open a file or folder in the OS file manager / default handler.

    Cross-platform: ``os.startfile`` on Windows, ``open`` on macOS, ``xdg-open``
    elsewhere. Returns True if the launch was issued.
    """
    p = str(Path(path))
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)  # type: ignore[attr-defined]  # Windows-only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except Exception:
        return False
