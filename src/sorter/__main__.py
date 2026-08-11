"""Entry point — initialize SQLite, load config, launch the Tk main window.

Also hosts the ``--apply-update`` pre-launch hook. ``bootstrap.py`` is what
actually applies a staged update these days — it imports
``sorter.update.apply_update`` directly, before ``uv sync``, so the staged
update's own ``pyproject.toml``/``uv.lock`` is what gets synced. This flag
remains as a compatibility entry point for anything still launching the old
way. Either route is stdlib-only and must stay that way — it runs against a
virtualenv that may not have any third-party packages in it yet.

**The ``sys.path`` shim below must stay above every ``sorter`` import.**
``src/sorter/__main__.py`` is run directly as a script
(``python src/sorter/__main__.py``, not ``python -m sorter`` — the ``sorter``
package is deliberately never installed into the venv, see CLAUDE.md §7), so
Python puts this file's own directory (``src/sorter``) on ``sys.path[0]``
automatically. Without the shim, plain ``import sorter`` would fail — the
package's parent (``src/``) isn't on the path at all — and even a relative
fix that only *adds* ``src/`` would leave ``src/sorter`` on the path too,
where its submodules (``ui``, ``update``, ...) could shadow same-named
third-party packages.

Follows the pattern from reqstool-client's ``command.py``, cited in
https://github.com/reqstool/reqstool-client/blob/main/src/reqstool/command.py#L10-L20
verbatim down to the three properties that matter: guard on ``__package__``
being empty (so this only fires when run as a bare script, never when
``sorter`` is imported as an installed package), remove the auto-added
script directory before inserting its parent, and stay stdlib-only.
"""

from __future__ import annotations

import os
import sys

if __package__ is None or len(__package__) == 0:
    _script_dir = os.path.abspath(os.path.dirname(__file__))
    # Remove the script directory from sys.path: Python adds it automatically, but
    # sorter subpackages (e.g. ui/) would then shadow same-named third-party
    # packages.
    if _script_dir in sys.path:
        sys.path.remove(_script_dir)
    sys.path.insert(0, os.path.abspath(os.path.join(_script_dir, "..")))

    # This branch running at all *is* the fact: a script run this way is
    # never the installed-package case (see sorter.paths.is_installed_package).
    from sorter import paths

    paths.set_installed_package(False)


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

    # Logged here rather than from bootstrap.py: the flag is recorded by
    # whichever shim started this *process* (above, or the root main.py
    # compatibility entry point), so the launcher — a different process, which
    # imports sorter.paths straight from src/ — could only ever report the
    # fallback heuristic, and would print a constant. bootstrap.py pipes this
    # through to the launch log, so it still reaches a bug report.
    print(f"[casesorter] installed package: {paths.is_installed_package()}")

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
