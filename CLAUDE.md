# CLAUDE.md

Guidance for AI assistants (and humans) working in this repository. It maps the
architecture, the moving parts, and the conventions so a new contributor can be
productive without reverse-engineering the whole tree. **Keep this file current:
when you add a tab, change the data model, or alter a subsystem boundary, update
the relevant section here in the same change.**

---

## 1. What this project is

The **AI Case Sorter** is a cross-platform (Windows + Linux/Ubuntu) desktop
application that drives a physical machine which sorts spent brass cartridge
casings by **headstamp** (the stamp on the base of the case). A camera
photographs each case, an image classifier predicts the headstamp, and a
serial-connected sorting machine drops the case into the correct bin.

It is the **full-parity Python/Tkinter version of the existing Windows-only
WinForms application** and is intended to eventually replace it. Much of the
code deliberately mirrors the WinForms behavior.

The "community" features (model sharing, downloads, feedback loop) authenticate
against a hosted backend at `reloadingrecipes.com` via Azure AD B2C. The app
runs fully without ever signing in — community features are the only auth-gated
surface.

Two ways to classify:
- **AI Config mode** (no local model active): send the cropped image to an
  OpenAI-compatible HTTP server (`/v1/chat/completions`).
- **Local model mode**: run a PyTorch **ConvNeXt** model locally. The model can
  be one the user trained in the Train tab, a pretrained model downloaded from
  the community, or one imported from a ZIP — running locally does **not** require
  the user to have trained it. PyTorch is an **optional** dependency
  (`pip install .[ml]`) installed on demand.

---

## 2. Running, testing, layout

**Entry point:** `src/sorter/__main__.py` → initializes paths, opens the
SQLite DB (migrating from a legacy `data/config.json` if present), loads
`Config`, and launches `sorter.ui.app.MainWindow`. Launched as
`python -m sorter` with `PYTHONPATH=src`, which `bootstrap.py` sets on the
child process — the package is deliberately **never installed** into the venv
(`uv sync --no-install-project`), so the environment is what makes `-m`
resolve. **No module in `src/sorter/` may rewrite `sys.path`**
(`tests/unit/test_entry_point.py` enforces it): the launcher owns the path,
and a hand-rolled shim that inserts `src/sorter` instead of `src/` puts every
subpackage on the path as a top-level name, where `ui`, `data` and `update`
shadow same-named third-party packages.

**One exception, and it is never run from this tree:**
`src/sorter/_legacy_entry.py` is copied into the sdist as a root `main.py` by
`pyproject.toml`'s `[tool.hatch.build.targets.sdist.force-include]`. An in-app
update is applied by the copy **already installed**, and every release up to
1.1.0 ends that launch with `python main.py` from a `bootstrap.py` already in
memory when the new tree lands — so the *archive* needs one even though the
source tree doesn't. Keeping it out of the repo root is what lets the source
layout be final: retiring the shim is one deleted line in `pyproject.toml`.
`tests/integration/test_cross_version_update.py` runs 1.1.0's real updater
against a real built sdist to keep all of this honest.

**Launch (handles the Python runtime, system deps, and dependency sync
automatically):**
- Linux/macOS: `./start.sh` (`--auto` / `AUTO_INSTALL=1` auto-confirms `sudo`
  package installs for libGL/glib — the only system packages still needed;
  see below)
- Windows: `start.bat`
- Either just hands off to `bootstrap.py`, which does the actual work via
  [uv](https://docs.astral.sh/uv/): installs uv itself if it isn't already
  present (into a project-local `.uv/`, not system-wide), provisions the
  pinned Python version from `.python-version` (uv's own build, bundling
  Tcl/Tk — the app's *system* Python only has to be new enough to run
  `bootstrap.py` itself, not to run the app), syncs dependencies from the
  committed `uv.lock`, then launches. See `bootstrap.py`'s module docstring
  for the full ordering and why it has to stay stdlib-only.
- Directly (once synced), same as `bootstrap.py` does it — this is the form to
  use under a debugger (insert `-m debugpy --listen 5678 --wait-for-client`
  before `-m sorter`; see CONTRIBUTING.md) or for a tight edit-run loop, since
  `start.sh` re-does uv discovery, the Linux library probe and a sync every
  time:
  ```bash
  PYTHONPATH=src uv run --no-sync python -m sorter     # PowerShell: $env:PYTHONPATH="src"
  ```
  Both halves matter. **`PYTHONPATH=src`** is what `-m` resolves through,
  since the package is never installed. **`--no-sync`** because a bare `uv
  run` syncs *and installs the project*, firing hatch-vcs's build hook: with
  no `.git` present that overwrites a correct `src/sorter/_version.py` with
  `fallback-version = "0.0.0"`, and `0.0.0+unknown` parses as a pre-release,
  so every launch then sees the current release as newer and re-prompts.

**Tests:** `pytest` from the repo root (`tests/conftest.py` puts `src/` on
`sys.path`; it lives at `tests/` top-level so it applies to both
subdirectories below it). `tests/unit/` mirrors `src/sorter/`'s subpackages
one-for-one (`tests/unit/hardware/`, `tests/unit/data/`, …), plus a handful of
modules that test something at the package's own top level (`test_paths.py`,
`test_bootstrap.py`, `test_version.py`, `test_installer_scripts.py`) and stay
directly under `tests/unit/`. Everything in `tests/unit/` uses synthetic
fixtures only; `tests/integration/` stays flat — the two files that shell out
to a real external tool (`uv build`, `git-cliff`) instead, each self-skipping
if that tool is missing; `pytest -m "not integration"` skips them outright.
CI (`.github/workflows/build.yml`) runs the full matrix on every push/PR —
run `pytest` locally before pushing regardless, since CI turnaround is slower
than your own machine. The suite is threading-fragile by design (see
`tests/conftest.py`); don't parallelize it.

**Python:** 3.12+ floor (`pyproject.toml`); `.python-version` pins the actual
version uv provisions for the app itself, independent of that floor. **Core
deps:** pyserial, opencv-python, numpy, Pillow, requests, msal, platformdirs,
sqlite-utils (+ `pygrabber` on Windows). **Optional ML deps:** torch,
torchvision.

```
AI-Case-Sorter-Py/
├── bootstrap.py              # cross-platform launcher logic (Python+uv+deps+update)
├── start.sh / start.bat     # thin per-OS shims that just call bootstrap.py
├── pyproject.toml           # package metadata; [ml] extra = torch/torchvision
├── uv.lock                  # committed, exact dependency resolution
├── .python-version           # Python version uv provisions for the app
├── src/
│   └── sorter/               # all application code (never installed — see above)
│       ├── __main__.py         # entry point (+ `--apply-update` pre-launch hook)
│       ├── _legacy_entry.py    # shipped as the archive's root main.py; see §2
│       ├── paths.py            # on-disk layout; stdlib-only, imported before uv sync
│       ├── control/            # event bus + the sort loop
│       ├── hardware/           # serial, camera, image processing
│       ├── data/                # SQLite persistence + model ZIP import/export
│       ├── ml/                  # classification, local inference, evaluation
│       ├── community/           # auth, community backend client, feedback loop
│       ├── update/              # self-update: check/stage + pre-launch apply
│       ├── training/            # out-of-process ConvNeXt trainer
│       └── ui/                  # Tkinter UI (tabs + dialogs + theme)
├── installer/               # Windows bootstrapper (see §7)
└── tests/                   # pytest suite, mirrors src/sorter/'s subpackages
```

The data root lives **outside** the repo by default — see §6.

---

## 3. Architecture at a glance

The app separates **hardware I/O**, **control logic**, **persistence**, and
**UI** into independent, testable layers, glued by a thread-safe event bus.
Since #58's `src/` layout, that sentence is literally the top level of
`src/sorter/`: `hardware/` ↔ hardware I/O, `control/` ↔ control logic
(the event bus and the sort loop), `data/` ↔ persistence, `ui/` ↔ UI — plus
`ml/` (classification/inference/evaluation), `community/` (auth + the
community backend client + the feedback loop), `update/` (self-update), and
`training/` (the out-of-process trainer), each its own subpackage.

```mermaid
flowchart TB
    UI["UI — Tkinter, main thread<br/>app.MainWindow · ttk.Notebook tabs · modal dialogs · theme"]
    Bus["control.events.EventBus<br/>Queue-backed pub/sub"]

    UI -- "subscribes (drained on main thread)" --> Bus
    Bus -- "run_worker(fn) spawns" --> UI

    Bus --> RC["control.run_controller<br/>sort loop, daemon thread"]
    Bus --> SB["hardware.serial_broker<br/>UART protocol, reader+ping threads"]
    Bus --> CAM["hardware.camera<br/>cv2 grab thread"]
    Bus --> TM["training.manager<br/>subprocess + stdout JSON markers"]

    RC --> CLF["ml.classifier"]
    CLF --> LI["ml.local_inference (torch)"]
    CLF --> API["ml.api_client (HTTP)"]

    CAM --> IP["hardware.image_proc<br/>Hough crop"]

    TM --> TC["train_convnext.py<br/>ConvNeXt, separate process"]
```

- **Persistence:** `data.config.Config` → `data.repository.*Repo` →
  `data.db.Database` (SQLite, WAL)
- **Filesystem:** `paths.*` (top-level — stdlib-only, imported before `uv
  sync`) defines the `data/` layout; `data.model_io` handles ZIP import/export
- **Community:** `community.auth.AuthManager` (MSAL) →
  `community.community_api.CommunityApi` (HTTPS);
  `community.feedback.FeedbackService` owns the below-threshold image queue

### The event bus (`sorter/control/events.py`)
A single `EventBus` with a thread-safe `Queue`. Workers call `bus.post(topic,
payload)` from any thread; the Tk main loop calls `bus.drain()` on a 50 ms
`root.after` timer to dispatch queued events to subscribers **on the main
thread**, so handlers can safely touch widgets. Handler exceptions are
swallowed. Topics are slash-namespaced strings: `run/*`, `test/*`, `serial/*`,
`training/*`, `mode/changed`, `feedback/*`, `community/*`. This is the **only**
sanctioned way for worker threads to update the UI.

---

## 4. Module reference (`sorter/`)

### Persistence & configuration (`sorter/data/`)
- **`db.py`** — `Database`: owns one `sqlite3.Connection` (WAL, foreign keys on,
  `check_same_thread=False` with an `RLock` serializing multi-statement
  transactions / SAVEPOINTs). Schema: idempotent DDL plus ordered migration
  steps run through `sqlite_utils.Migrations` (`MIGRATIONS`), whose
  `_sqlite_migrations` tracking table is what decides run-once — `PRAGMA
  user_version` (`SCHEMA_VERSION = 5`) is stamped informationally, never
  downgraded. A legacy DB has no tracking table, so every step runs on first
  open whatever the stamp claims; **every step is therefore presence-guarded
  and idempotent** (that same property repairs databases stamped current by a
  pre-ladder build but structurally incomplete). Step names are load-bearing:
  renaming one makes every install run it again.
  `ensure_initialized()` creates the DB, runs a one-shot import from legacy
  `data/config.json` (renaming it `.bak`), or seeds a default cartridge+model.
  Tables: `cartridges`, `models`, `headstamp_parents`, `headstamps`,
  `slot_templates`, `settings`.
- **`repository.py`** — `CartridgeRepo`, `ModelRepo`, `HeadstampRepo`,
  `HeadstampParentRepo`, `SlotTemplateRepo`, `SettingsRepo`. All SQL is
  **parameterized**. `SettingsRepo` is a typed key/value store (JSON-encoded
  values) and holds `default_model_id` (the "active model").
- **`config.py`** — `Config`: in-memory mirror of the `settings` sections (`api`,
  `serial`, `image_proc`, `camera`) plus the canonical `DEFAULTS`. Headstamps are
  **not cached** — they're read fresh from the DB on every access (scoped to the
  active model; AI Config mode stashes them in a settings key). Also the home of
  routing logic: `slot_for_headstamp`, package-mode slot maps, parent
  classifications, auto-select, run options (confidence floor, store-images mode),
  and the sorting-template API (see below).
- **`models.py`** — dataclasses: `Model`, `Headstamp`, `Cartridge`, `SlotTemplate`,
  `TrainingConfig`, `AIModelConfig`, `ImageProcessingConfig`, plus normalizers
  (`normalize_upload_mode`, `SUPPORTED_MODEL_MODES`, `SLOT_TEMPLATE_MODES`).
- **`model_io.py`** (`sorter/data/model_io.py` — grouped with the rest of
  persistence, not a separate layer: it's a model persisted to a ZIP instead
  of SQLite) — model **ZIP** import/export; see the *Training & evaluation*
  entry below for what it does, kept there to stay next to the training
  workflow it feeds.

### Filesystem (`sorter/paths.py` — top level, not under `data/`)
- **`paths.py`** — single source of truth for the on-disk layout (see §6) and
  the legacy-data migration. `CASESORTER_DATA_DIR` overrides the data root.
  **Stdlib-only and import-light on purpose:** the pre-launch update step
  imports it before the venv has any third-party packages. Also tracks
  `is_installed_package()` — whether this process's `sorter` is a real
  install (pip/uv wheel) vs. a source checkout run as a script, which is how
  this app is actually always launched (see §2); `_legacy_entry.py` records
  the authoritative answer when it is the one running, falling back to a
  `site-packages`/`dist-packages` path heuristic for anything that imports
  `sorter` another way (e.g. a test). `bootstrap.py`'s launch log records it.

### Community backend config (`sorter/community/appenv.py`)
- **`appenv.py`** — developer overrides for the community backend, read from the
  environment with an optional `.env` (real env vars always win; see
  `.env.example`). `api_base()` applies `CASESORTER_API_BASE` over the
  production default; `tls_verify()` returns what to pass `requests` as
  `verify=` — a `CASESORTER_API_CA_BUNDLE` path, or `False` when
  `CASESORTER_API_INSECURE=1` **and** the base URL is loopback (it is ignored,
  with a warning, for any other host). `__main__.py` calls `load_dotenv()` at
  startup; `CommunityApi` resolves both at construction, not import.

### Active-model concept
"Active model" = `settings.default_model_id`. When **absent**, the app is in
**AI Config mode** (cloud HTTP classification, headstamps in a settings key).
When **set**, that local model is active (Train tab visible, local inference
used, headstamps in the `headstamps` table). Activating a model posts
`mode/changed`, which toggles tab visibility.

### Sorting templates
A **sorting template** is a named snapshot of the Run tab's slot assignments, so
one model can carry several bin layouts ("Range brass", "Match prep") and switch
between them from the Run tab's template dropdown.

- **Scope:** per model (`model_id NULL` = AI Config mode) **and** per run mode.
  Standard and package mode keep separate lists — package assignments are
  many-to-many (one headstamp in several slots), so a layout from one mode is
  meaningless in the other. `config.slot_template_mode()` picks the list.
- **Storage:** rows in `slot_templates`; `assignments_json` is name-keyed
  (`{"headstamps": {name: slot}, "parents": {name: slot}}`, or
  `{"slots": {slot: [names]}}` for package mode) so a template survives a
  headstamp being deleted and re-added. Unknown names are ignored on apply.
- **The live assignments stay authoritative.** A run still reads
  `headstamps.slot` / `headstamp_parents.slot` / the package slot map — templates
  never sit in the hot path. The *active* template (settings key
  `active_slot_template:<model id|ai>:<mode>`) is kept in lock-step with them by
  `Config.sync_active_slot_template()`, called from every slot mutation, so
  there is no explicit "save template" step. Switching is therefore a straight
  save-current / load-next swap (`activate_slot_template`), and applying a
  template **clears** any slot it doesn't mention.
- **Seeding/upgrade:** the first read of a scope with no rows creates "Default"
  holding whatever is currently assigned, so existing installs keep their layout.
  The last template in a scope can't be deleted.

### Hardware control (`sorter/hardware/`)
- **`serial_broker.py`** — `SerialBroker`: ASCII command protocol over UART
  (default 9600 8N1). A **reader thread** parses responses and fans them out to
  callback lists (`on_done`/`on_ok`/`on_error`/`on_received`/…); a **ping thread**
  keeps the link alive; a write lock serializes commands. Key commands: `xf:0`
  (feed one), `xf:<slot>` (force feed + sort), bare `<slot>` (sort imaged case),
  `sortto:<slot>` (move arm), `getconfig` (JSON board state), `version`, `stop`,
  `<key>:<value>` (set board param). `try_open()` does a version handshake.
  Responses are matched as **anchored tokens** — the line, stripped and
  lowercased, equals `ok`/`done`/`error`/`waiting` or begins with it followed
  by a non-alphanumeric delimiter — so `error: broken sensor` routes as the
  error it is and `undone` doesn't satisfy a pending feed. Everything else
  falls through to `on_response`.
  `try_open()` talks to the port directly rather than through the reader
  thread, so it **fires `on_received`/`on_sent` by hand** for the banner and
  the `version` exchange — without that the serial monitor is blank for
  exactly the connection that failed. `send_raw` writes a string verbatim (no
  newline added or stripped) for the monitor's line-ending selector; every
  protocol helper still goes through `send_command`, which owns the `\n` the
  firmware expects.
  **The authority on what the board actually prints is the firmware, which
  lives upstream — not here and not the emulator** (the emulator only ever
  emits the clean tokens the parser wants, so it can never disprove a parser
  bug). Read it at
  [`CS72_Firmware_V1.7.ino`](https://github.com/sjseth/AI-Case-Sorter-CS7.2/blob/main/MicroController/CS72_Firmware_V1.7/CS72_Firmware_V1.7.ino)
  before changing the protocol; `tests/unit/test_serial_broker.py` pins the
  vocabulary derived from it, against a named upstream commit.
- **`serial_emulator.py`** — `SerialEmulator`: drop-in fake mirroring the broker
  API (port name `"Emulated"`), responding after a timer delay. Enables running
  and testing without hardware.
- **`camera.py`** — `Camera`: `cv2.VideoCapture` with a background **grab thread**
  keeping the latest frame; platform backends (CAP_DSHOW on Windows w/ optional
  pygrabber for friendly names + resolution probing, CAP_V4L2 on Linux, MJPG for
  ≥1080p). `enumerate_devices` / `list_cameras_with_metadata` for the Camera tab.

### The sort loop (`sorter/control/run_controller.py`)
- **`run_controller.py`** — `RunController`: the production loop on a daemon
  thread. Per case: capture → `image_proc.crop_headstamp` → optional primer mask
  → `classifier.classify_active` → `_resolve_destination(label, confidence)` →
  `broker.sort_and_move(slot)`. Handles the 5-position wheel pipeline
  (`_last_classified_slot`), the **confidence floor** (below → catch-all slot 0),
  a `NoLocalCheckpointError` from `classify_active` (stops the run with the
  reason; the Run tab also pre-flights this at Start so no case is fed),
  **auto-select trays**, **package/batch mode** (`_package_counts` under a lock),
  optional run-image storage, and feedback capture. Also `cycle_once()` (manual
  feed) and `test_once()` (feed+classify, no sort). Posts `run/*` and `test/*`.

### Classification (`sorter/ml/`)
- **`classifier.py`** — `classify_active`: **the active model alone picks the
  backend.** A model is active → local inference; AI Config mode (no active
  model) → HTTP. Passes the trained `image_size` through. A local model whose
  checkpoint is missing raises `NoLocalCheckpointError` — it does **not**
  degrade to HTTP. That fallback existed and was a trap: a renamed data folder
  or an images-only community share left `model_path` unusable and the app
  quietly POSTed case images to whatever the AI Config tab last pointed at,
  surfacing only as a connection error naming a host the user wasn't knowingly
  using. Switching backends is the user's call, on the Models tab. `active_model`
  / `uses_local_inference` / `has_local_checkpoint` / `checkpoint_problem`
  expose the decision alone, so the UI can ask "does this need PyTorch?" and
  "can this model actually classify?" before starting a run — keep them in
  lock-step with `classify_active` or the install gate (§5) drifts from reality.
- **`local_inference.py`** — lazy-imports torch; picks the device once; caches
  loaded models by `(path, mtime)`; runs all inference through a single-threaded
  executor to keep cuDNN state warm. Detects the checkpoint's classifier layout
  and rebuilds the ConvNeXt head. Loads checkpoints with
  `torch.load(..., weights_only=True)` so a malicious `.pth` (community download
  or imported ZIP) cannot execute code on load. Two presence checks, and the
  difference matters: `is_installed()` is a `find_spec` probe (free, safe on the
  UI thread) and is what the install gate uses; `is_available()` actually
  imports torch and on first call runs the device probe + benchmark dump, which
  would freeze the UI if called from a button handler.
- **`api_client.py`** — stateless HTTP client (`classify`, `get_headstamps`)
  against an OpenAI-compatible server. JPEG-encodes the frame to a base64 data
  URL, renders the `{{headstamps}}` prompt placeholder, parses `choices[0]...`
  and a top-level `confidence` float.
- **`image_proc.py`** — crops the headstamp to a fixed **480×480 BGR** canvas.
  Default strategy: **Hough circles** (`HoughParams`); a dormant **line-scan**
  strategy is ported but UI-hidden. `apply_primer_mask` (none/use/hide),
  `overlay_detection` for preview.

### Training & evaluation (`sorter/training/`, plus evaluation in `sorter/ml/` and ZIP import/export in `sorter/data/model_io.py`)
- **`training/manager.py`** — `TrainingManager`: spawns `train_convnext.py` as a
  **subprocess** (clean cancellation, no GIL fights), pumps stdout for
  `[PROGRESS] {json}` markers, and re-emits them as `training/*` events. SIGTERM
  → SIGKILL escalation on cancel. `build_command` builds the argv (list form, no
  shell).
- **`training/train_convnext.py`** — the worker script. Trains
  `convnext_{tiny,small,base,large}` via torchvision pretrained weights; AdamW +
  cosine LR, optional focal loss, label smoothing, stochastic depth, SWA, mixed
  precision. Saves a dict checkpoint: `{model_state_dict, classes, base,
  image_size, ...}` via `torch.save`. Module-level dataset classes so Windows
  `spawn` DataLoader workers can pickle them.
- **`training/dataset.py`** — filename convention helpers. Training images are
  `{label}__{ticks}.jpg` where `ticks` is the **.NET `DateTime.Ticks`** value
  (for interop with the legacy Windows app). `save_training_image`,
  `feedback_filename` (`{label}__{confidence}__{ticks}.jpg`), `parse_label`,
  `class_counts`. Labels are run through `safe_label` before becoming filenames
  (classification labels can come from an untrusted classification server).
- **`evaluator.py`** — offline batch evaluation of a model against a labeled
  folder, with folder-label→model-class mapping (auto-suggest via token scoring)
  and `summarize` (per-class accuracy/confidence).
- **`eval_report.py`** — self-contained interactive HTML report (base64 thumbnails
  + embedded results JSON), a verbatim port of the legacy app's report. ⚠️ Result
  rows are interpolated into a `<script>` block, so the report is only safe to
  open for locally-evaluated, trusted image folders.
- **`gpu_detect.py`** — shells out to `nvidia-smi` (torch not yet installed) to
  detect a compute-capability ≥ 8.0 NVIDIA GPU for the Install-PyTorch dialog.
- **`image_store.py`** (`sorter/data/image_store.py`) — pure pathlib helpers to
  list/filter/reclassify/delete training images by their `{headstamp}__{ticks}`
  filenames.
- **`models.py` ownership helpers** (`sorter/data/models.py`) —
  `is_foreign_model` / `is_trainable` / `FOREIGN_MODEL_TYPES`: the single
  definition of "this model belongs to someone else" (see §5, *Model
  ownership*).
- **`model_io.py`** (`sorter/data/model_io.py`) — model **ZIP** import/export compatible with the WinForms
  format (`manifest.json` + `model/<id>.pth` + `images/*`). Accepts both
  snake_case and WinForms PascalCase manifest keys. Import **rejects `..`
  traversal entries** and only uses entry basenames; export strips paths/secrets.
  An archive whose `community_model_uid` is already installed is an **update**:
  `import_model` refreshes that row in place (same model id → same directories,
  headstamp slots, and sorting templates) instead of creating a duplicate, and
  keeps the local name / feedback-loop opt-in / AI config. `find_update_target`
  tells a caller which path an archive will take; `update_existing=False`
  forces a separate copy. `community_download=True` marks the install as the
  publisher's, and an update never downgrades that (§5, *Model ownership*).

### Self-update (`sorter/update/`; see §7 for the full flow)
- **`updater.py`** — GitHub Releases check, version comparison, and download →
  verify → stage. Needs `requests`. Never writes to the app folder.
- **`apply_update.py`** — the pre-launch half: copies a staged tree over the app
  folder, with backup/rollback. **Stdlib-only** — it runs before `uv sync`.
  Grouped with `updater.py` under `sorter/update/` for subsystem organization
  only; `sorter/update/__init__.py` is (like every subpackage `__init__.py`)
  deliberately empty, so importing this module alone never drags in
  `updater.py`'s `requests` import. Also stamps `src/sorter/_version.py` with
  the applied version when the archive didn't carry one, so an install
  updated from the source-archive fallback doesn't keep reporting
  `0.0.0+unknown` and re-prompting forever.

### Community / cloud (`sorter/community/`)
- **`auth.py`** — `AuthManager`: MSAL `PublicClientApplication` against Azure AD
  B2C (hardcoded tenant/client/authority/redirect, mirroring WinForms). Token
  cache is a single file, chmod 0600 on POSIX. Decodes ID-token claims **for
  display only** (signature not verified — never used for authz). Auth is
  optional; the only gated surface is the Community tab.
- **`community_api.py`** — `CommunityApi`: HTTPS client for
  `reloadingrecipes.com/api` (cartridges, model search, download via Azure-blob
  SAS URL, feedback-image upload, wish-list fetch, model share). Bearer token
  pulled fresh from `AuthManager` per call. Downloads/uploads stream with atomic
  writes. Base URL and TLS trust come from `appenv` (see above); `verify` is
  passed **per request**, never set on the session — `REQUESTS_CA_BUNDLE` /
  `CURL_CA_BUNDLE` in the environment outrank `session.verify`, so a
  session-level setting is silently ignored on machines that set them.
- **`feedback.py`** — `FeedbackService`: the community **feedback loop**. When a
  community model with the loop enabled produces a below-floor prediction (floor
  clamped to ≥ 50), the cropped image is staged to
  `data/models/<id>/feedback_images/`. **The folder is the queue** (no DB mirror);
  `upload_pending` drains it via `CommunityApi`, deleting on success or drop on
  failure. Debug tracing to stderr is **off by default** — enable with
  `CASESORTER_FEEDBACK_DEBUG=1`.
  Also owns the **wish list** (model balancing): `GET /Models/FetchWishList`
  returns the classifications a model is short of images for. The Run tab fetches
  it on a worker thread at Start (gated on `is_feedback_model`, so an opted-out
  user's auth path is untouched) and clears it at Stop; `should_capture` then
  captures on *below floor **or** wanted label*. Wish-list capture applies to
  continuous runs only (not Manual Feed), is capped at
  `MAX_WISH_LIST_CAPTURES_PER_LABEL` (40) per classification per run, and **fails
  open** — any error or non-200 installs an empty list, i.e. confidence-only
  behavior. No UI surface.

---

## 5. The UI (`sorter/ui/`)

`MainWindow` (`app.py`) is the shell: gradient title bar (with the theme picker
parked at its right edge), a `ttk.Notebook` of tabs (each wrapped in a
`ScrollableFrame` for small displays, and hosted on a backdrop canvas that owns
the margin around it), and a status bar with connection indicators + sign-in.
Both indicators are links (hand cursor, underline on hover): serial opens the
serial monitor, camera selects the Camera tab. The transient message shares
that bar and is packed **last**, so a crowded bar truncates it rather than the
standing connection state. It owns the `EventBus`, `SerialBroker`,
`Camera`, `RunController`, and `AuthManager`, auto-connects serial/camera on
startup, and runs the bus drain loop. `run_worker(fn, on_done, on_error)` is the
standard helper for offloading blocking work to a thread and marshaling the
result back through the bus.

**Tab visibility is mode-driven:** Train shows for a local active model **that
this user owns** (`models.is_trainable` — see *Model ownership* below); AI
Config shows in AI Config mode; Community is mounted only while signed in. The
`mode/changed` event re-evaluates this.

### Model ownership
A model installed from the **Community tab** is stamped `model_type =
"CommunityManaged"` by `import_model(..., community_download=True)`, and
`models.is_trainable()` is False for it: the local checkpoint is the
publisher's, retraining forks it from the version they keep updating, and the
archive usually ships without the images it was built from. `ReadOnly` (the
legacy app's marker) is treated the same. `_merge_onto_installed` never lets
an update downgrade ownership.

**`community_model_uid` is not an ownership signal** — sharing your own model
stamps a UID onto your local copy, so a UID means "exists in the community",
not "isn't yours". Ownership is decided by *how the model reached this
machine*, which is why the flag is a parameter of `import_model` rather than
something read out of the manifest (a publisher's own copy is `Standard`, and
that's what they export). A plain ZIP import stays owned — it's just as likely
to be a user restoring their own model onto a new machine.

### The PyTorch install gate
PyTorch is the optional `[ml]` extra, so a fresh install has none. **The rule:
torch is installed the first time something actually needs it, and never
before** — an AI Config user must never be prompted. `ui/torch_gate.py` is the
single entry point; it opens `dialog_install_torch` and re-enters the caller on
success:

```python
if not ensure_torch(self, self._start, reason="Sorting needs PyTorch"):
    return
```

Gated: Run tab Start + Manual feed (only when `classifier.uses_local_inference`
is True), the evaluator, and training. The Train tab's Feed *offers* rather
than gates — capturing and labelling images is exactly the workflow that
doesn't need torch, so declining costs only the predicted-label convenience and
is remembered for the session. Call it on the **main thread only** (it opens a
modal), and never gate on `is_available()`.

### Tabs (`tab_*.py`)
| Tab | File | Purpose |
|-----|------|---------|
| **Run** | `tab_run.py` | Production sorting. Sorting-template bar; flow-grid of slot cards + per-slot headstamp checkboxes with live counts; Start/Stop/Manual-feed; package-mode counters. The largest UI module. |
| **Models** | `tab_models.py` | Model library: browse/filter, create, edit, **activate**, import/export, delete. Synthetic "Use AI Config" row. |
| **Train** | `tab_train.py` | Feed→capture→classify→label→save loop; "Sort While Training"; launches training (Install-PyTorch dialog if needed → progress dialog). |
| **AI Config** | `tab_ai.py` | HTTP server config (endpoint/key/model/prompt/encoding), headstamp manager, single-shot test. Visible only in AI Config mode. |
| **Camera** | `tab_camera.py` | Device + resolution detection and live preview. |
| **Serial** | `tab_serial.py` | Connection, 14 board init settings, sort-arm test, airdrop config, and a `SerialConsole` + "Open monitor ↗" (see `serial_console.py`). |
| **Image Proc** | `tab_imageproc.py` | Tune Hough params + primer mask + LED brightness against a captured frame (before/after preview). |
| **Community** | `tab_community.py` | Browse/search/download community models; share entry point. Auth-gated. |

### Dialogs (`dialog_*.py`)
`dialog_training_progress` (live training console), `dialog_training_config`
(hyperparameters), `dialog_model_editor` (create/edit model + feedback-loop
opt-in), `dialog_install_torch` (installs torch/torchvision into the venv via
uv, falling back to pip),
`dialog_login` (MSAL interactive sign-in), `dialog_model_evaluator` (run eval +
HTML report + history), `dialog_model_images` + `dialog_image_preview` (training
image browser/reclassify/delete), `dialog_share_model` (publish to community),
`dialog_slot_template` (new / rename / delete a sorting template),
`dialog_theme_editor` (build a theme from the active one: a color picker per
palette role, a canvas preview of a miniature app, and JSON export/import —
reached from the gear beside the title-bar theme picker; "Save & apply"
writes back to a saved theme, rename included, and "Create new…" always makes
a separate one, so a built-in is never the thing being written to),
`dialog_update` (release notes → download progress → "Restart to update"; §7).

### Shared UI infrastructure
- **`theme.py`** — `THEMES`, the live `PALETTE`, `apply_theme(root, theme=…)`
  (fonts + ttk styles, single source of truth), `retheme_widgets`,
  `paint_gradient`. **Every color in the app comes from here.**
  - **Themes.** `THEMES` maps a display name to a full palette; the user picks
    one from the dropdown in the title bar and it's stored in the `ui.theme`
    setting (`theme.SETTING_THEME`). Ships with Dark (the original), Light,
    Sepia, Midnight Blue, Gothic, and Comic Book. **The role of each key is
    fixed; only its color changes per theme** — a new theme is a copy of
    `_DARK` with new values, and it must define exactly the same keys.
    `success` mirrors `action` and `error` mirrors `danger`, so a theme with
    no green (Comic Book, where blue is "go") has a blue "connected"
    indicator, not a green one.
  - **Halftone screens.** `HALFTONE_INK` names the themes that print a
    ben-day dot field, and the ink to print it in; `paint_halftone` prints
    one over any box of a canvas, fading in from whichever edge you name.
    Only canvases can carry it — ttk widgets always fill their own
    background, so nothing shows through them. Two places screen themselves,
    both app chrome: the title bar (`app._repaint_header`) and the margin
    around the notebook (`app._layout_page` — the notebook rides on a
    backdrop canvas for exactly this reason). Keep it to the chrome: a screen
    behind the working area of a tab is noise, not decoration.
  - **Ink outlines.** `INK_OUTLINE` names the themes that draw comic-book
    borders and how many pixels wide; everything else stays flat and
    borderless. `apply_theme` reads it for panels, cards, buttons and fields.
    A card's outline belongs to the card, not to the layout rows inside it —
    those use `row_style(card_style)` (`Card.TFrame` → `CardRow.TFrame`),
    which shares the fill but never the border. Cards that restyle their
    children on hover/selection must map through `row_style` too.
  - **Switching is live**, so it must stay that way: `apply_theme` reloads the
    ttk styles (which every ttk widget follows on its own) and
    `retheme_widgets` walks the widget tree translating the colors baked into
    classic Tk widgets (`tk.Label`, `tk.Canvas`, `tk.Text`) at construction.
    That translation is by color value, which is why no two roles inside one
    theme may share a color — except `success`/`error`, which must equal
    `action`/`danger` (`tests/unit/test_theme.py` enforces both rules).
  - **`PALETTE` is mutated in place** on a switch. Read it at call time
    (`PALETTE["bg_card"]`); never copy a color into a module-level constant.
  - **User-made themes.** `BUILTIN_THEMES` is what ships; `THEMES` is the live
    registry — built-ins plus whatever the theme editor has saved.
    `register_custom_theme` adds one (and its halftone/outline options),
    `rename_custom_theme` moves one (a rename is not copy-and-delete — the
    theme keeps its place and options), `custom_themes_payload` is what the
    app persists to the `ui.custom_themes` setting, and `load_custom_themes`
    re-registers them at startup, before the saved theme name is resolved.
    Names are capped at `MAX_THEME_NAME` because the picker is sized to the
    longest of them. From then on a user
    palette is an ordinary entry in `THEMES` — nothing downstream knows the
    difference. `normalize_palette` is the gate: it fills gaps from a base
    theme, drops unknown keys and non-colors, and forces `success`/`error`
    back onto `action`/`danger`, so neither a hand-edited settings row nor an
    imported file can produce a broken palette..
  - **Hue is meaning.** Dark keeps its chrome (window, panels, cards, inputs,
    borders, text, focus/selection tints) **neutral grayscale**, reserving hue
    for action buttons (`action*` green = primary/go, `update*` blue = refresh
    something installed, `danger*` red = stop/destructive) and status text.
    The tinted themes keep the same discipline internally: their surfaces are
    one low-saturation family so the action buttons stay the most saturated
    thing on screen. Don't add a saturated surface to any theme.
- **`widgets.py`** — `ScrollableFrame` (pass `viewport=(w, h)` to fix how much
  is visible and let the rest scroll), `ImagePanel` (shows BGR numpy frames),
  `NumericField`, labeled-entry/button-row helpers.
- **`monitor.py`** — detachable history window: ring buffer of recent
  classifications with a color "snake" trailing the latest. Subscribes `run/history`.
- **`serial_console.py`** — `SerialConsole`, the Arduino-IDE-style traffic log.
  **The Serial Config tab's "Serial monitor / debug" panel and the detached
  monitor window are the same widget**, embedded twice, so neither can grow a
  feature the other lacks — which is exactly how they had already drifted (the
  tab log was one colour, uncoloured, unfilterable and capped at 500 lines).
  Autoscroll / timestamps / pause (held lines flush on resume, they are not
  dropped), Clear, Save…, a line-ending selector that sends through
  `broker.send_raw`, and command history. A case-insensitive substring filter
  and per-direction (RX/TX/notes) toggles narrow the view: `matches()` is the
  single predicate, applied by `_render` as lines arrive, by `_rerender` when
  the filter changes, and by `dump()` so Save… writes what's on screen. It
  matches the board's text, **not** the rendered line, so the `<-`/`->`
  prefixes and timestamps can't be filtered against. `_lines` always keeps
  everything — the filter hides, never deletes. **The log is never re-ordered**
  (no column sort): serial traffic only reads correctly in the order it
  happened, since a command and its reply are one exchange. Subscribes
  `serial/rx`, `serial/tx` and `serial/note` (probe commentary) itself, and
  **replays `MainWindow.serial_backlog`** on construction — the rolling deque
  `app._log_serial` fills — because the traffic worth reading (a failed
  auto-connect probe) happens seconds before anyone can open a window.
  The **speed picker is part of the component**, not something a host bolts
  on, and it is the *only* baud control in the app — the Serial tab's
  Connection panel deliberately has none, so one widget owns
  `config.serial["baud"]`, persisting and reconnecting as it is picked (which
  is also why `SerialTab.save()` does not write that key). `BAUD_RATES` is
  what a 16 MHz AVR can generate inside 8N1's ±2% tolerance — **not** the
  Arduino IDE's ladder, so don't re-add 230400. Two consoles can still be
  live at once (the tab's and an open window's), so `_sync_baud` re-reads the
  setting on `serial/state` and they can't drift apart; `on_baud_changed`
  tells a host that wants to react (the window repaints its header). The one
  host-specific option is `detach_command` (the tab's "Open monitor ↗", on
  the control row rather than a row of its own). Text tags bake their colours
  in and `retheme_widgets` can't reach them, so `set_theme` calls
  `apply_palette()` on every live console.
- **`serial_monitor.py`** — `SerialMonitorWindow`: a `SerialConsole` in its own
  window, under a connection header (port, baud, firmware behind a dot
  mirroring the status bar's — it subscribes `serial/state` for that). Opened
  by clicking the status bar's `● Serial: …` indicator or the Serial tab's
  button; `app.open_serial_monitor()` keeps it to one instance.
- **`torch_gate.py`** — `ensure_torch(parent, proceed, reason=…)`: the only
  sanctioned way to front a local-model action with the PyTorch install dialog.
  See *The PyTorch install gate* above.
- **`sysutil.py`** — `open_path` (os.startfile / open / xdg-open).

---

## 6. Data & on-disk layout

Everything the app writes lives under a single **data root**, resolved once by
`paths.app_data_dir()`:

1. `CASESORTER_DATA_DIR` — explicit override, wins over everything.
2. A `portable.txt` marker next to `bootstrap.py` → `<app>/data` (USB-stick installs).
3. Otherwise the per-user OS location: `%LOCALAPPDATA%\CaseSorter` on Windows,
   `$XDG_DATA_HOME/CaseSorter` (default `~/.local/share/CaseSorter`) elsewhere.

**The data root is outside the app folder by default, and that is load-bearing:**
the in-app updater replaces the app folder wholesale (§7). Keeping user data out
of it makes the updater safe by construction rather than by maintaining an
exclusion list. `paths.migrate_legacy_data_dir()` moves a pre-0.2 `<app>/data`
up on first run, so upgrades are invisible. `<app>/data` is still **gitignored**
and must never be committed.

```
<data root>/
├── config/
│   ├── casesorter.db      # SQLite (all settings, models, headstamps)
│   └── msal_cache.bin     # MSAL token cache (chmod 0600 on POSIX)
├── models/
│   └── <model_id>/
│       ├── images/          # raw training images   {label}__{ticks}.jpg
│       ├── run_images/      # opt-in run captures
│       ├── feedback_images/ # below-threshold feedback queue (folder == queue)
│       ├── reports/         # evaluator HTML reports
│       └── trainedmodel/    # <model_id>.pth checkpoint
├── logs/                  # launcher + installer logs (§7)
│   ├── launch.log           # this launch; previous kept as launch.prev.log
│   └── install-<stamp>.log  # one per install-windows.ps1 run
└── updates/               # staged app updates (§7)
    ├── pending/             # extracted tree awaiting the next launch
    ├── pending.json         # its metadata — a SIBLING, never inside pending/
    ├── backup/              # previous version, kept for rollback
    └── last_applied.json
```

**Filename convention** (WinForms-compatible): training images are
`{label}__{ticks}.jpg`; feedback images are `{label}__{confidence}__{ticks}.jpg`,
where `ticks` is the .NET `DateTime.Ticks` value.

---

## 7. Updates & Windows install

Non-developers get the app without git, and keep it current from inside the app.
There is no git dependency anywhere in this path: a release tarball over HTTPS
has the same trust anchor as `git pull` over HTTPS, and the source tree is
~1 MB, so delta transfer buys nothing.

**Version:** derived from the git tag at build time (`pyproject.toml`'s
`[tool.hatch.version] source = "vcs"`, via hatch-vcs), not hand-bumped —
removes the old "forgot to bump `__version__` in the release commit" footgun
entirely; the manual step just doesn't exist anymore. hatch-vcs's build hook
writes `src/sorter/_version.py` (gitignored, generated), which
`src/sorter/__init__.py` imports as its first choice, falling back to
`importlib.metadata` (an actual pip/uv install from a wheel) and finally a
literal placeholder if neither is available — see that file's comments for
why each tier exists.

Two things this makes load-bearing that weren't before:

- **hatch-vcs needs `.git` to derive a version, and a downloaded release has
  none.** `bootstrap.py`'s `uv sync`/`uv run` therefore run with
  `--no-install-project`/`--no-sync` specifically so the build hook never
  fires for an end user at all — confirmed empirically that running it
  without `.git` either hard-crashes the build, or (with a
  `fallback-version` configured) silently *overwrites* an already-correct
  `src/sorter/_version.py` with that fallback. See `bootstrap.py`'s docstring.
- **The version has to reach the user some other way, then.** `updater.py`'s
  `_pick_asset` downloads the release's own **sdist** by exact name
  (`ai_case_sorter-<tag>.tar.gz`) — the same file `uv build` already
  produces and `publish.yml` already attaches, not a separately built
  artifact. hatch-vcs's build hook stamps `src/sorter/_version.py` into every
  build target it runs against, sdist included, so it already carries the
  correct version with nothing to copy in by hand. (An earlier revision
  built a bespoke `git archive`-based zip for this instead; that second
  build path was the actual cause of a real version mismatch once the tree
  looked "dirty" to git for unrelated reasons — gone rather than fixed
  again.) That's what makes a downloaded release able to report its own
  version correctly with no `.git` anywhere in it.

**The flow is stage now, apply at next launch:**

```mermaid
flowchart TD
    A["updater.check_for_update()<br/>GET /releases/latest, compare tags — needs requests"]
    B["updater.stage_update()<br/>download → verify → &lt;data&gt;/updates/pending/<br/>(the app folder is NOT touched)"]
    C(["restart"])
    D["apply_update<br/>run by bootstrap.py BEFORE uv sync — stdlib ONLY"]
    E["sorter.update.apply_update<br/>backup → copy over app dir → prune → clear pending"]

    A --> B --> C --> D --> E
```

- **`updater.py`** — check/download/stage. Traversal-safe extraction (same
  rejections as `model_io`, plus rejecting every non-regular-file entry, which
  only a tarball can carry, and a containment check on the resolved output
  path), strips the single top-level wrapper (the sdist's `<name>-<version>/`
  or, on the fallback, GitHub's `<repo>-<tag>/`), requires at least one of
  `REQUIRED_ENTRY_SETS` to be fully present before trusting an archive (the
  current `src/sorter/__init__.py` layout, **or** the pre-#58 flat
  `main.py` + `sorter/__init__.py` layout — the flat-layout entry stays
  forever, not because this tree can ever produce it again, but because the
  in-app updater on an install that predates #58 is the *only* thing that
  can ever validate an archive going forward, and that check can't be patched
  after the fact; see #58's issue thread for why the two-release migration
  this implies doesn't actually need engineering around it), and caps archive
  size and entry count. Staging is atomic: `pending/` only ever exists complete.
  - `check_for_update()` (`GET /releases/latest`) is unchanged: latest stable
    only, newer-than-current only, used for the silent startup check and the
    dialog's default. `list_releases()` (`GET /repos/{repo}/releases`) is
    additive, for the version picker below — every published release, newest
    first, drafts always excluded, pre-releases excluded unless
    `include_prereleases=True`. Both run every tag through `_TAG_RE` (a
    malformed one reaches the fallback archive URL and could redirect it to a
    different repo) and resolve the download through `_pick_asset`, so a
    version chosen in the picker resolves to the same archive the single-release
    check would have handed back for it. The two diverge on how a bad tag
    fails: `check_for_update` has exactly one release to report, so it raises;
    `list_releases` has many, so it drops the offending one rather than hiding
    every legitimate release behind it.
  - **Why `REQUIRED_ENTRY_SETS` still carries the flat-layout tuple:** the
    updater that validates a new release archive is whatever version is
    *already installed* on a user's machine, and updates aren't cumulative —
    an install several releases behind the current one only ever fetches
    `/releases/latest` and validates that single archive with whatever check
    it shipped with. A pre-#58 install therefore has to keep recognizing a
    plain ZIP/reinstall of the current, `src/`-layout app as "the app" for as
    long as any such install might still exist; there is no way to patch that
    logic retroactively after the fact. `REQUIRED_ENTRIES` (the flat-layout
    tuple) exists purely for that backward direction now — nothing this repo
    builds produces that shape anymore. `tests/integration/test_sdist_contents.py`
    asserts the real, current sdist against `REQUIRED_ENTRY_SETS` as a whole
    (satisfies *at least one* set), not against the flat tuple specifically.
- **`apply_update.py`** — **must stay stdlib-only.** It runs against a venv that
  may hold nothing at all yet; importing `requests` here would break the very
  launch it exists to fix. Backs up everything it will overwrite, rolls back on
  failure, and **always exits 0** so a broken updater can never stop the app
  starting. Pruning stale files is confined to `PRUNE_ROOTS`; `PROTECTED_TOP_LEVEL`
  (`.git`, `.venv`, `.uv`, `data`, `.env`, `portable.txt`) is never touched.
  - **`PRUNE_ROOTS` covers both layouts** (`src/sorter`, `sorter`) because an
    archive can be either: the version picker makes downgrading to a pre-#58
    release a supported move, and an install upgraded *from* one leaves a
    stale root `sorter/` behind (the old updater empties the flat package,
    then re-creates `sorter/_version.py` inside it). Empty directories left
    by a prune are swept afterwards, best-effort.
- **The launch that applies an update re-launches itself.** `bootstrap.py`
  resolved its own module and the entry-point path before the update replaced
  the tree underneath it, so continuing would sync and then launch the
  *previous* release's paths — which is precisely how a layout change breaks.
  `apply_pending_update()` returns whether it applied anything;
  `relaunch_after_update()` then re-runs `bootstrap.py` from disk, once
  (`CASESORTER_BOOTSTRAP_RELAUNCHED` guards the loop). subprocess, not
  `os.execv`: exec on Windows returns cmd.exe to its prompt, defeating
  `start.bat`'s pause-on-failure.
- **1.0.0 and 1.0.1 cannot update directly to a `src/`-layout release.**
  `REQUIRED_ENTRY_SETS` arrived in 1.1.0 (#62); before it the only accepted
  set was the flat `("main.py", "sorter/__init__.py")`, so those installs
  reject the archive outright as "not the app". Nothing can patch an already
  installed updater, so this is permanent: those users step to 1.1.0 via the
  version picker first. Say so in the release notes of the first `src/`
  release.
- **Why a pre-launch step at all:** on Windows the venv's `.pyd`/`.dll` files
  (opencv, numpy) are locked while the app runs, so in-place replacement is
  unreliable. Applying before Python loads anything sidesteps locking — and puts
  a new `pyproject.toml`/`uv.lock` in place *before* `bootstrap.py` calls
  `uv sync`, so dependency changes install on the same restart. There's no
  hash-based marker to worry about anymore: `uv sync` reconciles against the
  venv's actual contents, not a proxy for them.
- **UI** — `dialog_update.py` (notes → progress → "Restart to update") reached
  from a status-bar button in `app.py` that appears only when there's something
  to do. A silent check runs 2.5 s after startup; opt out via the dialog's
  checkbox (`updates.check_on_startup`) or `CASESORTER_UPDATE_DISABLED=1`.
  The dialog opens showing only what the startup check already found — the
  latest stable release, or "up to date" — with nothing further fetched over
  the network. A "Choose a different version…" button is what triggers
  `updater.list_releases()` (on a worker thread, same dialog-local
  `Queue`-and-poller pattern as the download progress below — a second,
  independent queue/poller pair, since a picker load and a download can in
  principle both be in flight); it reveals a version combobox plus a "Show
  prereleases" opt-in. Picking an entry — including one older than what's
  currently offered, and even when the plain check said there was nothing
  newer — replaces what "Download & Install" targets and the notes/detail
  text update to match; the detail text says explicitly when the selection
  isn't the newest release available. **This picker is the only route to a
  release candidate** — the startup check hits `/releases/latest`, which
  excludes prereleases, so an rc is never announced.
- **Release candidates.** `release.yml`'s `prerelease` input (`none`/`rc`/`b`/
  `a`) cuts one: everything a release does, minus the `promote` step, so the
  GitHub release stays a prerelease permanently instead of as a staging state.
  Three things hold it together, and each is pinned by a test:
  - **`.github/scripts/next-prerelease.sh`** picks the number, because
    git-cliff only emits final versions. `0.5.0` → `0.5.0rc1` → `0.5.0rc2`,
    compared numerically so `rc9` → `rc10`.
  - **`cliff.toml`'s `tag_pattern` is anchored at both ends**, making rc tags
    invisible to git-cliff — otherwise `--bumped-version` hands back the rc
    tag *itself* as the next version, and the release's notes would start at
    the candidate rather than the last stable tag.
  - **PEP 440's canonical spelling, no separator** (`0.5.0rc1`). hatchling
    names the sdist from the normalized version and `release.yml` asserts the
    two match, so `0.5.0-rc1` fails the release by design. `updater.is_newer`
    ranks the same segments, which is what lets an rc install see the eventual
    release as newer.
- **`installer/`** — `install-windows.ps1` (+ `.bat` wrapper) provisions Python
  via winget or a silent python.org install, lays the app down in
  `%LOCALAPPDATA%\Programs\CaseSorter`, and hands off to `start.bat` (which just
  calls `bootstrap.py`). Per-user, no admin. See `installer/README.md`. The
  Python it provisions only ever runs `bootstrap.py` — uv provisions the
  interpreter the app itself runs on — so `$PythonMin` tracks
  `requires-python` while `$PythonWinget` / `$PythonFallback` track
  `.python-version`, which lets uv reuse the already-present install instead
  of downloading a near-identical second one.
- **Logging, and why it spans three files.** The chain is
  `install-windows.ps1` → `start.bat` → `bootstrap.py` → the app, and every
  step after the first runs in a **detached console that closes with the
  process** — so a traceback out of the app was visible for a fraction of a
  second and then gone, reaching the user as "nothing happened". Both halves
  now write to `<data root>/logs/` (§6): the installer via `Start-Transcript`,
  and `bootstrap.py` via `open_log()` + `run_app()`, which **pipes the app's
  stdout/stderr and echoes each line** so the output is both live and on disk.
  Three rules to preserve: logs live under the data root, never the app folder
  (the updater replaces that wholesale); logging is best-effort and must never
  block a launch (an unwritable data dir loses the log, not the app); and
  `start.bat` **pauses on a non-zero exit** so the window can't close over the
  error. `uv sync` deliberately keeps inherited stdio — its progress bars
  redraw with carriage returns that a line reader would turn into junk.

---

## 8. Conventions & gotchas

- **Conventional Commits are load-bearing, not cosmetic.** The commit type you
  pick *is* the version bump — git-cliff derives the release version from it,
  and there is no version string in the source to edit (§7). A mistyped `fix:`
  ships a wrong version, not just a wrong changelog line. Full type list and
  rules: `CONTRIBUTING.md`.
  - The mapping is `fix` → patch, `feat` → minor, `!`/`BREAKING CHANGE:` →
    major — **except that a breaking change below 1.0.0 bumps the minor**
    (`0.1.0` + `feat!:` → `0.2.0`). `cliff.toml` sets
    `breaking_always_bump_major = false` for this; git-cliff's default would
    send that straight to `1.0.0`, letting one commit message declare the API
    stable. **Reaching 1.0.0 requires passing `version` explicitly** to the
    Release workflow — nothing auto-detects it. Verified against git-cliff
    2.13.1 and pinned in `tests/integration/test_cliff_config.py`, which runs the real
    binary in CI (`release-config` job in `lint.yml`).
  - Each changelog line also carries `by @user` and, for a **squash-merged**
    PR only, `in #N` — see `RELEASING.md` for why the two resolve
    independently.
- **Threading rule:** never touch Tk widgets off the main thread. Do blocking
  work in `run_worker`/daemon threads and `bus.post(...)`; the drain loop
  delivers handlers on the main thread. **`widget.after()` is not an escape
  hatch** — it registers a Tcl command and is itself unsafe off the main
  thread. A worker must hand results to a `Queue` (the bus, or a dialog-local
  one as in `dialog_update.py`) that a main-thread poller drains.
  `dialog_install_torch.py` predates this note and still calls `after()` from
  its pip-pump thread; don't copy that pattern.
- **Legacy-app interop is intentional.** Many odd choices (PascalCase manifest
  keys, .NET ticks filenames, ConvNeXt-mode integer mapping, the exact serial
  command strings, the verbatim HTML report) exist so this app round-trips with
  the legacy Windows app — preserve that compatibility when editing these.
- **PyTorch is optional and lazily imported.** Guard any torch use; surface a
  friendly "install PyTorch" path rather than letting an `ImportError` escape.
  Don't add torch to the core `dependencies` in `pyproject.toml` — it's the
  `[ml]` extra. Any **new** entry point that runs a model locally must go
  through `ui/torch_gate.ensure_torch` (§5) — a bare `LocalInferenceError`
  reaching the user is the bug that gate exists to prevent.
- **DB access is shared across threads** via one connection + RLock. Wrap
  multi-statement work in `db.transaction()` (reentrant via SAVEPOINT).
- **Headstamps are read fresh, not cached** — don't reintroduce a cached
  snapshot (it previously caused silent data loss).
- **Cloud features depend on the hosted `reloadingrecipes.com` backend** and a
  specific Azure B2C tenant. The API base URL and its TLS trust are
  environment-overridable (`appenv`, `.env.example`) so you can run against a
  local copy of the backend; the **Azure B2C tenant/client/scopes in `auth.py`
  are still hardcoded**, so a fork pointing at its own identity provider has to
  edit that file. The backend itself is a separate service, not in this repo.
- **Releases drive the updater.** There is no version string to edit — tagging
  *is* the bump (§7), and the release workflow derives the tag from the commit
  types since the last one. `/releases/latest` excludes pre-releases, which is
  exactly what makes the `prerelease` input safe: an rc reaches testers who go
  looking for it and nobody else. See `RELEASING.md`.
- **The distribution path assumes a public repo.** The installer and updater
  both fetch anonymously over HTTPS; against a private repo every request
  404s and there is no in-band way to tell that apart from "no releases yet"
  (the API returns 404 for both). If the repo must stay private, distribution
  has to move off GitHub — see `installer/README.md`.
- **CI** (`.github/workflows/build.yml`) runs `pytest` across a
  [3.12, 3.13, 3.14] × [Linux, Windows] matrix on every push and PR, plus a
  `launcher-smoke` job that actually runs `start.sh`/`start.bat` end to end.
  Still run `pytest` locally before pushing — faster feedback than waiting on
  CI. Most UI modules need a display — `xvfb-run -a pytest` covers them on a
  headless box; without tkinter installed those modules skip rather than
  fail. `lint.yml` also runs the [ty](https://docs.astral.sh/ty/) type checker
  (`uv run ty check`), and it is **blocking** — the tree is at zero
  diagnostics, so anything it reports is something the PR introduced. Run it
  locally alongside `pytest` and `ruff`. **Fix the code, don't silence the
  checker:** every `# ty: ignore[rule]` in the tree carries a comment saying
  why the finding is genuinely unfixable, and they are all one of two cases —
  optional dependencies absent by design (torch/torchvision are the `[ml]`
  extra, pygrabber/comtypes are Windows-only) or gaps in opencv's bundled
  stubs. Note the job does a **full** `uv sync` rather than `--only-group dev`:
  ty resolves third-party imports from the environment, so without the runtime
  deps the output drowns in unresolved-import noise. The one `[tool.ty]` block
  in `pyproject.toml` exists because `src/sorter/_version.py` is generated and
  gitignored (§7): it is absent in CI (which never fires the build hook) and
  present for anyone who has run `uv build`, so the `# ty: ignore` on its
  import would otherwise flip to an *unused* ignore and fail the build for
  contributors only. The override silences `unused-ignore-comment` for that
  one file and nowhere else.
  `install-windows.ps1` gets its own workflow
  (`.github/workflows/installer-smoke.yml`), not `build.yml`'s blanket
  trigger: it needs a real published release to exercise its interesting
  path (sdist matching, `tar.exe` extraction), so it's path-filtered to
  `installer/**` on PR/push plus a `workflow_call` that `release.yml` makes
  against the release it just cut, gating promotion to `latest`. Not
  `release: published` — that fires alongside the sdist upload it depends
  on, and the release is still a prerelease at that point, so
  `/releases/latest` would resolve to the previous one. It runs
  with `shell: powershell` (Windows PowerShell 5.1), not `pwsh`, deliberately
  — that's the interpreter a real double-click via `install-windows.bat`
  uses, and the one the script's own top-of-file comment calls out for its
  BOM/codepage decoding quirks.
- See **`CONTRIBUTING.md`** for how to set up and contribute.
