"""Entry point — initialize SQLite, load config, launch the main window.

Reached through the root ``main.py``, which puts ``src/`` on ``sys.path``
first; this module does no path setup of its own.

Also hosts the ``--apply-update`` pre-launch hook, which must stay
stdlib-only — it runs against a virtualenv that may hold no third-party
packages yet. ``bootstrap.py`` applies staged updates by importing
``sorter.update.apply_update`` directly; the flag remains for anything still
launching the old way.
"""

from __future__ import annotations

import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Pre-launch update hook. Deliberately ahead of every other import so it
    # cannot pull in a dependency the venv doesn't have yet.
    if "--apply-update" in args:
        from sorter.update.apply_update import main as apply_main

        return apply_main()

    from sorter import logging_setup, paths
    from sorter.community import appenv
    from sorter.data.config import Config
    from sorter.data.db import Database

    # Before anything that logs. The data-folder move below is the one thing
    # that can't be: it decides where the log file goes.
    moved = paths.migrate_legacy_data_dir()
    logging_setup.configure()
    # NOT `__name__`: run as `python -m sorter` this module is called
    # `__main__`, which sits outside the `sorter` hierarchy the handlers are
    # installed on — every line from here would go nowhere.
    log = logging.getLogger("sorter.main")

    # First line of every session. A log whose first entry is the failure gives
    # a bug report nothing to place it against -- which version, which Python,
    # which data folder. Cheap enough to be unconditional.
    import sorter

    log.info(
        "CaseSorter %s starting — python=%s platform=%s data=%s installed=%s",
        sorter.__version__,
        sys.version.split()[0],
        sys.platform,
        paths.app_data_dir(),
        paths.is_installed_package(),
    )

    # One-time move of a pre-0.2 `<app>/data` folder to the per-user location.
    # No-op for portable installs, an explicit CASESORTER_DATA_DIR, or once
    # it has already run.
    if moved is not None:
        log.info("moved data folder to %s", moved)

    # Developer overrides (community API base URL / TLS trust). Silent unless
    # something is actually configured — see sorter/community/appenv.py and
    # .env.example.
    for line in appenv.startup_report(appenv.load_dotenv()):
        log.info("%s", line)

    paths.ensure_directories()
    legacy_json = paths.app_data_dir() / "config.json"

    db = Database()
    db.ensure_initialized(legacy_config_json=legacy_json if legacy_json.exists() else None)

    config = Config(db).load()

    from sorter.ui.app import run_app

    # opencv-python bundles its own Qt and registers its plugin dir on
    # import (already done, transitively, by the line above). Loading cv2's
    # plugins against PySide6's Qt libraries mixes two Qt builds — a known
    # source of startup failures and rendering artifacts. Scrub cv2's entries
    # so QApplication only ever sees PySide6's own plugins.
    plugin_path = os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", "")
    if plugin_path:
        kept = [p for p in plugin_path.split(os.pathsep) if p and "cv2" not in p]
        if kept:
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.pathsep.join(kept)
        else:
            os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    run_app(config)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
