"""Entry point — initialize SQLite, load config, launch the Tk main window.

Reached through the root ``main.py``, which puts ``src/`` on ``sys.path``
first; this module does no path setup of its own.

Also hosts the ``--apply-update`` pre-launch hook, which must stay
stdlib-only — it runs against a virtualenv that may hold no third-party
packages yet. ``bootstrap.py`` applies staged updates by importing
``sorter.update.apply_update`` directly; the flag remains for anything still
launching the old way.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Pre-launch update hook. Deliberately ahead of every other import so it
    # cannot pull in a dependency the venv doesn't have yet.
    if "--apply-update" in args:
        from sorter.update.apply_update import main as apply_main

        return apply_main()

    from sorter import paths
    from sorter.community import appenv
    from sorter.data.config import Config
    from sorter.data.db import Database
    from sorter.ui.app import MainWindow

    # One-time move of a pre-0.2 `<app>/data` folder to the per-user location.
    # No-op for portable installs, an explicit CASESORTER_DATA_DIR, or once
    # it has already run.
    moved = paths.migrate_legacy_data_dir()
    if moved is not None:
        print(f"[casesorter] moved data folder to {moved}")

    # Developer overrides (community API base URL / TLS trust). Silent unless
    # something is actually configured — see sorter/community/appenv.py and
    # .env.example.
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
