"""Tests for the data-root layout and the legacy-folder migration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sorter import paths


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    """Every test here decides its own root; never inherit the caller's."""
    monkeypatch.delenv("CASESORTER_DATA_DIR", raising=False)


def test_app_root_is_the_folder_holding_bootstrap_py() -> None:
    """Anchored on real files, because a relative re-derivation proves nothing.

    ``app_root()`` is ``paths.py``'s own path minus three components, and the
    obvious test -- re-deriving the same thing from ``__file__`` -- is a
    tautology that stays green whatever the count is. Verified: mutating it
    to ``.parent.parent`` left the whole suite passing.

    Off by one is not cosmetic. ``app_root()`` is what ``apply_update`` backs
    up, overwrites and prunes, and what ``find_uv``/``is_portable``/
    ``legacy_data_dir`` resolve against. One level down and the updater
    operates on ``src/``.
    """
    root = paths.app_root()
    assert (root / "bootstrap.py").is_file()
    assert (root / "src" / "sorter" / "paths.py").is_file()
    assert (root / "pyproject.toml").is_file()


def test_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path))
    assert paths.app_data_dir() == tmp_path
    assert paths.config_dir() == tmp_path / "config"
    assert paths.db_path() == tmp_path / "config" / "casesorter.db"
    assert paths.token_cache_path() == tmp_path / "config" / "msal_cache.bin"
    assert paths.models_dir() == tmp_path / "models"
    assert paths.updates_dir() == tmp_path / "updates"


def test_default_root_is_outside_the_app_folder(monkeypatch, tmp_path: Path) -> None:
    """The updater overwrites the app folder — user data must not live in it."""
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(paths, "_os_data_dir", lambda: tmp_path / "userdata")
    root = paths.app_data_dir()
    assert root == tmp_path / "userdata"
    assert (tmp_path / "app") not in root.parents


def test_portable_marker_pins_data_into_the_app_folder(monkeypatch, tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / paths.PORTABLE_MARKER).write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "app_root", lambda: app)
    assert paths.is_portable() is True
    assert paths.app_data_dir() == app / "data"


def test_os_data_dir_per_platform(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths._os_data_dir() == tmp_path / "local" / "CaseSorter"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths._os_data_dir() == tmp_path / "xdg" / "CaseSorter"


def test_per_model_layout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path))
    mid = 7
    assert paths.model_dir(mid) == tmp_path / "models" / "7"
    assert paths.model_images_dir(mid) == tmp_path / "models" / "7" / "images"
    assert paths.model_feedback_dir(mid) == tmp_path / "models" / "7" / "feedback_images"
    assert paths.model_trained_dir(mid) == tmp_path / "models" / "7" / "trainedmodel"
    assert paths.model_trained_path(mid) == tmp_path / "models" / "7" / "trainedmodel" / "7.pth"


def test_ensure_directories_creates_top_level(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path))
    paths.ensure_directories()
    assert paths.config_dir().is_dir()
    assert paths.models_dir().is_dir()


def test_ensure_model_subtree(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path))
    paths.ensure_model_subtree(42)
    assert paths.model_images_dir(42).is_dir()
    assert paths.model_trained_dir(42).is_dir()


def test_export_temp_dir_is_under_the_data_root(monkeypatch, tmp_path: Path) -> None:
    # The share/export scratch folder must live under the app's own data dir,
    # never the OS temp dir (which on Windows can be locked or cleaned mid-write).
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path))
    assert paths.export_temp_dir() == tmp_path / "tmp"
    assert paths.export_temp_dir().parent == paths.app_data_dir()


# ----- legacy migration -------------------------------------------------------


def _fake_install(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "app"
    dest = tmp_path / "userdata"
    app.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: app)
    monkeypatch.setattr(paths, "_os_data_dir", lambda: dest)
    return app, dest


def test_migration_moves_legacy_data(monkeypatch, tmp_path: Path) -> None:
    app, dest = _fake_install(monkeypatch, tmp_path)
    legacy = app / "data"
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "casesorter.db").write_text("db", encoding="utf-8")
    (legacy / "models" / "3" / "images").mkdir(parents=True)
    (legacy / "models" / "3" / "images" / "WIN__1.jpg").write_text("x", encoding="utf-8")

    moved = paths.migrate_legacy_data_dir()

    assert moved == dest
    assert (dest / "config" / "casesorter.db").read_text(encoding="utf-8") == "db"
    assert (dest / "models" / "3" / "images" / "WIN__1.jpg").is_file()
    assert not legacy.exists()


def test_migration_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    app, dest = _fake_install(monkeypatch, tmp_path)
    (app / "data" / "config").mkdir(parents=True)
    assert paths.migrate_legacy_data_dir() == dest
    # Second run is a no-op, not a second move.
    assert paths.migrate_legacy_data_dir() is None


def test_migration_does_not_clobber_an_existing_root(monkeypatch, tmp_path: Path) -> None:
    app, dest = _fake_install(monkeypatch, tmp_path)
    (app / "data").mkdir()
    (app / "data" / "stale.txt").write_text("old", encoding="utf-8")
    dest.mkdir(parents=True)
    (dest / "casesorter.db").write_text("live", encoding="utf-8")

    assert paths.migrate_legacy_data_dir() is None
    assert (dest / "casesorter.db").read_text(encoding="utf-8") == "live"
    assert (app / "data" / "stale.txt").is_file()


def test_migration_skipped_when_portable(monkeypatch, tmp_path: Path) -> None:
    app, dest = _fake_install(monkeypatch, tmp_path)
    (app / paths.PORTABLE_MARKER).write_text("", encoding="utf-8")
    (app / "data").mkdir()
    assert paths.migrate_legacy_data_dir() is None
    assert (app / "data").is_dir()
    assert not dest.exists()


def test_migration_skipped_when_env_override_set(monkeypatch, tmp_path: Path) -> None:
    app, dest = _fake_install(monkeypatch, tmp_path)
    (app / "data").mkdir()
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "elsewhere"))
    assert paths.migrate_legacy_data_dir() is None
    assert not dest.exists()


def test_migration_no_legacy_folder(monkeypatch, tmp_path: Path) -> None:
    _fake_install(monkeypatch, tmp_path)
    assert paths.migrate_legacy_data_dir() is None


# ----- find_uv ---------------------------------------------------------------
# bootstrap.py and dialog_install_torch.py both need to locate uv after it's
# been installed -- the latter because a uv-managed venv doesn't ship pip, so
# `uv pip install --python <interpreter>` is how it installs torch into the
# already-running venv.


def test_find_uv_prefers_project_local_install(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    # Match the OS-appropriate binary name find_uv() itself looks for
    # (uv.exe on Windows) -- this ran green on Linux but failed for real on
    # Windows CI when it was hardcoded to the POSIX name.
    binary_name = "uv.exe" if paths.os.name == "nt" else "uv"
    local = tmp_path / ".uv" / "bin" / binary_name
    local.parent.mkdir(parents=True)
    local.touch()
    monkeypatch.setattr(paths.shutil, "which", lambda _name: "/usr/bin/some-other-uv")

    assert paths.find_uv() == str(local)


def test_find_uv_falls_back_to_path_when_no_local_install(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda _name: "/usr/bin/uv")

    assert paths.find_uv() == "/usr/bin/uv"


def test_find_uv_returns_none_when_uv_is_nowhere(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda _name: None)

    assert paths.find_uv() is None
