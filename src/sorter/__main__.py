"""Entry point — initialize SQLite, load config, launch the Tk main window.

Also hosts the ``--apply-update`` pre-launch hook. ``bootstrap.py`` is what
actually applies a staged update these days — it imports
``sorter.apply_update`` directly, before ``uv sync``, so the staged update's
own ``pyproject.toml``/``uv.lock`` is what gets synced. This flag remains as a
compatibility entry point for anything still launching the old way. Either
route is stdlib-only and must stay that way — it runs against a virtualenv
that may not have any third-party packages in it yet.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    # Pre-launch update hook. Deliberately ahead of every other import so it
    # cannot pull in a dependency the venv doesn't have yet.
    if "--apply-update" in args:
        from sorter.apply_update import main as apply_main

        return apply_main()

    from sorter import appenv, paths
    from sorter.config import Config
    from sorter.db import Database
    from sorter.ui.app import MainWindow

    # One-time move of a pre-0.2 `<app>/data` folder to the per-user location.
    # No-op for portable installs, an explicit CASESORTER_DATA_DIR, or once
    # it has already run.
    moved = paths.migrate_legacy_data_dir()
    if moved is not None:
        print(f"[casesorter] moved data folder to {moved}")

    # Developer overrides (community API base URL / TLS trust). Silent unless
    # something is actually configured — see sorter/appenv.py and .env.example.
    for line in appenv.startup_report(appenv.load_dotenv()):
        print(f"[casesorter] {line}")

    paths.ensure_directories()
    legacy_json = paths.app_data_dir() / "config.json"

    db = Database()
    db.ensure_initialized(legacy_config_json=legacy_json if legacy_json.exists() else None)

    config = Config(db).load()
    MainWindow(config).run()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
