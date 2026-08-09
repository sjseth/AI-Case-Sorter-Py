"""Print exactly what the classifier sees when deciding local vs HTTP.

Run this with the same interpreter the app uses, from the repo root:

    Windows:  .venv\\Scripts\\python tools\\diagnose_active_model.py
    Linux:    .venv/bin/python tools/diagnose_active_model.py

`classify_active` picks local inference only when ALL of these hold:
  * a model is active (settings.default_model_id is set)
  * that row still exists
  * its model_path is non-empty
  * the file at model_path exists on disk

If any one fails it falls back to the AI Config HTTP server, which is what a
`localhost:8000 /v1/chat/completions` connection error means. This prints each
condition separately so you can see which one is the problem.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sorter import paths
from sorter.db import Database
from sorter.repository import ModelRepo, SettingsRepo


def main() -> int:
    print(f"data root : {paths.app_data_dir()}")
    db_path = paths.db_path()
    print(f"database  : {db_path}  (exists={db_path.exists()})")
    if not db_path.exists():
        print("\n-> No database. The app has never run against this data root.")
        return 1

    db = Database(db_path)
    db.connect()
    settings = SettingsRepo(db)
    models = ModelRepo(db)

    active_id = settings.get_active_model_id()
    print(f"\nactive model id (settings.default_model_id) = {active_id!r}")
    if active_id is None:
        print("-> AI Config mode. Every classification goes over HTTP.")
        print("   Fix: activate a model on the Models tab.")
        _print_library(models)
        return 1

    model = models.get(active_id)
    if model is None:
        print(f"-> default_model_id={active_id} points at a row that no longer exists.")
        print("   Fix: re-activate a model on the Models tab.")
        _print_library(models)
        return 1

    print(f"  name       : {model.name}")
    print(f"  model_type : {model.model_type}")
    print(f"  community  : {model.community_model_uid}")
    print(f"  model_path : {model.model_path!r}")

    if not model.model_path:
        print("\n-> model_path is EMPTY: this model has no trained checkpoint, so")
        print("   classification falls back to the AI Config HTTP server.")
        print("   A community download lands here if its archive shipped without")
        print("   the model file (an images-only share).")
        _print_expected_checkpoint(model.id)
        return 1

    if not Path(model.model_path).exists():
        print(f"\n-> model_path does NOT exist on disk: {model.model_path}")
        print("   The row points at a checkpoint that was moved or deleted, so")
        print("   classification silently falls back to the HTTP server.")
        _print_expected_checkpoint(model.id)
        return 1

    size = Path(model.model_path).stat().st_size
    print(f"\n-> LOCAL inference. Checkpoint present ({size / 1e6:.1f} MB).")
    print("   If you still saw a /v1/chat/completions call, the run was started")
    print("   before this model was activated — restart the run.")
    return 0


def _print_expected_checkpoint(model_id: int) -> None:
    d = paths.model_trained_dir(model_id)
    print(f"\n   expected checkpoint dir: {d}")
    if d.exists():
        found = list(d.iterdir())
        print(f"   contents: {[p.name for p in found] or 'EMPTY'}")
    else:
        print("   contents: directory does not exist")


def _print_library(models: ModelRepo) -> None:
    print("\ninstalled models:")
    for m in models.list():
        has = bool(m.model_path and Path(m.model_path).exists())
        print(f"  id={m.id:<4} {m.name:<28} type={m.model_type:<17} checkpoint={has}")


if __name__ == "__main__":
    raise SystemExit(main())
