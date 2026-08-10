"""File-system locations.

Everything the app writes lives under a single **data root**, resolved once
per process. Layout:

    <data root>/
    ├── config/
    │   ├── casesorter.db    ← SQLite database
    │   └── msal_cache.bin   ← MSAL token cache (chmod 0600 on POSIX)
    ├── models/
    │   └── <model_id>/
    │       ├── images/          ← raw training images
    │       ├── run_images/      ← images captured during a run (opt-in)
    │       ├── feedback_images/ ← below-threshold community feedback queue
    │       ├── reports/         ← evaluator HTML reports
    │       └── trainedmodel/    ← <model_id>.pth (PyTorch zip archive)
    └── updates/             ← staged app updates (see sorter/updater.py)

Resolution order:

1. ``CASESORTER_DATA_DIR`` — explicit override; wins over everything.
2. A ``portable.txt`` marker next to ``main.py`` — keeps the data in
   ``<app>/data`` for USB-stick / self-contained installs.
3. The per-user OS location: ``%LOCALAPPDATA%\\CaseSorter`` on Windows,
   ``$XDG_DATA_HOME/CaseSorter`` (default ``~/.local/share/CaseSorter``)
   elsewhere.

**Why the data root is no longer app-local by default:** the in-app updater
replaces the app folder in place. Keeping user data — trained models,
databases, captured images — outside that folder makes the updater safe by
construction rather than by remembering to exclude the right paths.
``migrate_legacy_data_dir()`` moves an existing ``<app>/data`` up on first
run so this is invisible to anyone upgrading.

This module is deliberately **stdlib-only and import-light**: the pre-launch
update step (``main.py --apply-update``) imports it before the virtualenv has
any third-party packages in it.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "CaseSorter"

# Presence of this file next to main.py pins the data root to <app>/data.
PORTABLE_MARKER = "portable.txt"


def app_root() -> Path:
    """App folder (one level above ``sorter/``) — where ``main.py`` lives."""
    return Path(__file__).resolve().parent.parent


# Retained for callers that predate the rename.
_app_root = app_root


def legacy_data_dir() -> Path:
    """The pre-0.2 location: ``<app>/data``."""
    return app_root() / "data"


def find_uv() -> str | None:
    """Locate the uv binary bootstrap.py uses to sync/run the app.

    Checked in the same order bootstrap.py checks it: the project-local
    install first (``.uv/bin/``, where bootstrap.py puts it -- and
    deliberately does *not* add to PATH, see its module docstring), then
    PATH. Used by anything that needs to invoke uv after launch -- e.g.
    dialog_install_torch.py, since a uv-managed venv doesn't ship pip by
    default and ``uv pip install --python <interpreter>`` is the equivalent.
    """
    local = app_root() / ".uv" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    if local.exists():
        return str(local)
    return shutil.which("uv")


def is_portable() -> bool:
    """True when the portable marker pins the data root inside the app folder."""
    return (app_root() / PORTABLE_MARKER).exists()


def _os_data_dir() -> Path:
    """Per-user data location for the current platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def app_data_dir() -> Path:
    """Root for everything the app writes. See the module docstring."""
    override = os.environ.get("CASESORTER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if is_portable():
        return legacy_data_dir()
    return _os_data_dir()


def migrate_legacy_data_dir() -> Path | None:
    """Move a pre-0.2 ``<app>/data`` folder to the new root. Idempotent.

    Returns the destination when a migration happened, else ``None``. Never
    raises: a failed migration leaves the legacy folder untouched, and the
    caller carries on with an empty new root rather than refusing to start.

    A ``os.replace`` is tried first (atomic, instant); if the two live on
    different volumes it falls back to a copy, and the legacy folder is only
    removed once the copy has fully succeeded.
    """
    if os.environ.get("CASESORTER_DATA_DIR") or is_portable():
        return None

    legacy = legacy_data_dir()
    dest = _os_data_dir()
    if not legacy.is_dir() or dest.exists():
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(legacy, dest)
        return dest
    except OSError:
        pass

    # Cross-volume (or Windows holding a handle): copy, then drop the original.
    try:
        shutil.copytree(legacy, dest)
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        return None
    shutil.rmtree(legacy, ignore_errors=True)
    return dest


def config_dir() -> Path:
    return app_data_dir() / "config"


def db_path() -> Path:
    return config_dir() / "casesorter.db"


def token_cache_path() -> Path:
    return config_dir() / "msal_cache.bin"


def models_dir() -> Path:
    """Root for all per-model folders."""
    return app_data_dir() / "models"


def updates_dir() -> Path:
    """Scratch + staging area for in-app updates (see ``sorter/updater.py``)."""
    return app_data_dir() / "updates"


def logs_dir() -> Path:
    """Where ``bootstrap.py`` records what happened on each launch.

    Under the data root like everything else here, and deliberately not in
    the app folder: the in-app updater replaces that wholesale (see
    ``sorter/updater.py``), so a log kept there would be deleted by the very
    update whose failure you are trying to read about. The Windows installer
    writes its own logs beside these, computing the same path itself -- it
    runs before there is a Python that could import this.
    """
    return app_data_dir() / "logs"


def export_temp_dir() -> Path:
    """App-local scratch folder for model export/share ZIPs.

    Kept inside the app's own data directory rather than the OS temp dir,
    which on Windows can be locked, cleaned mid-write, or otherwise
    unavailable. Created on demand by callers.
    """
    return app_data_dir() / "tmp"


def model_dir(model_id: int) -> Path:
    return models_dir() / str(model_id)


def model_images_dir(model_id: int) -> Path:
    return model_dir(model_id) / "images"


def model_run_images_dir(model_id: int) -> Path:
    """Where the run screen's optional 'Store Images' feature writes captures."""
    return model_dir(model_id) / "run_images"


def model_feedback_dir(model_id: int) -> Path:
    """Where below-threshold community feedback-loop captures are staged.

    This folder IS the feedback queue: files are uploaded then deleted (or
    dropped on failure). No database row mirrors them — polling this small
    folder is the source of truth for the OnRunComplete / Manual modes.
    """
    return model_dir(model_id) / "feedback_images"


def model_reports_dir(model_id: int) -> Path:
    """Where the model evaluator writes its HTML reports."""
    return model_dir(model_id) / "reports"


def model_trained_dir(model_id: int) -> Path:
    return model_dir(model_id) / "trainedmodel"


def model_trained_path(model_id: int) -> Path:
    """Conventional file path for the trained model checkpoint."""
    return model_trained_dir(model_id) / f"{model_id}.pth"


def ensure_directories() -> None:
    """Create every top-level directory if missing. Safe to call repeatedly."""
    for d in (app_data_dir(), config_dir(), models_dir()):
        d.mkdir(parents=True, exist_ok=True)


def ensure_model_subtree(model_id: int) -> None:
    """Create the `<model_id>/images` and `<model_id>/trainedmodel` dirs."""
    model_images_dir(model_id).mkdir(parents=True, exist_ok=True)
    model_trained_dir(model_id).mkdir(parents=True, exist_ok=True)
