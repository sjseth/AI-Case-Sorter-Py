# Installers

For people who just want to run the app — no git. `install-windows.ps1` is
the Windows one and most of this page; `install-unix.sh` covers Linux and
macOS ([below](#linux-and-macos-install-unixsh)).

## For users (Windows)

1. Download **`install-windows.bat`** and **`install-windows.ps1`** into the
   same folder (or grab them from a release archive).
2. Double-click `install-windows.bat`.

It installs to `%LOCALAPPDATA%\Programs\CaseSorter` (per-user, no admin
rights) and adds a Start Menu entry. First launch installs the Python
dependencies and takes a few minutes; after that the app starts immediately.

**Updates are handled inside the app** — the status bar shows *Update
available* when there's a new release, and *Restart to update* once it has
downloaded. There's no need to re-run this installer, though re-running it is
a safe way to repair a broken install.

## What it does

| Step | Detail |
|---|---|
| Python | Uses an existing Python 3.12+ if one is present. Otherwise installs one via `winget`, falling back to a silent per-user python.org install. |
| App | Downloads the latest release's sdist (`ai_case_sorter-<version>.tar.gz`) over HTTPS, checks it against the SHA-256 GitHub publishes for that asset, and extracts it with `tar.exe`. Falls back to the source archive if that asset is absent. **No git.** |
| Checksum | A mismatch, or a published checksum that can't be read, stops the install before anything is extracted. A download with no published checksum — the source-archive fallback always, or a release GitHub returns none for — still installs, over HTTPS alone, and says *Not verified* in the log. The in-app updater applies the same rule. |
| Launch | Hands off to `start.bat`, which calls `bootstrap.py` — that's what owns the venv and dependency sync now, via [uv](https://docs.astral.sh/uv/), not `pip install`. |

## Where things live

```
%LOCALAPPDATA%\Programs\CaseSorter\   ← the app (replaced by updates)
%LOCALAPPDATA%\CaseSorter\            ← your data (never touched by updates)
    ├── config\casesorter.db
    ├── models\<id>\...
    ├── logs\                         ← install + launch logs (see below)
    └── updates\                      ← staged update, pending restart
```

## When something goes wrong

Both halves of the process leave a log in `%LOCALAPPDATA%\CaseSorter\logs\`:

| File | Written by | Covers |
|---|---|---|
| `install-<timestamp>.log` | `install-windows.ps1` | Finding/installing Python, downloading and extracting the release. One per run, kept. |
| `launch.log` | `bootstrap.py` | Everything from `start.bat` onwards: uv, the dependency sync, and the app's own output including any traceback. Replaced each launch; the run before is kept as `launch.prev.log`. |

They live under the data root rather than the app folder on purpose: the
installer overwrites the app folder and the in-app updater replaces it
wholesale, so a log kept there would be destroyed by the next thing that goes
wrong. Attach these when reporting a problem — "no window appeared" is almost
always answered by `launch.log`, because on Windows the console closes with
the process and takes the traceback with it.

Keeping data out of the app folder is what makes the in-app updater safe: it
overwrites the app directory, and there is nothing of yours in it. An install
that predates this layout is migrated automatically on first run.

## Options

```powershell
# Install somewhere else
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -InstallDir D:\CaseSorter

# Pin a specific release (tags carry no `v` prefix - see the maintainer notes).
# Rarely needed: the default is the latest release.
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Version 1.0.0

# Install without launching
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -NoLaunch

# Install from a fork's own releases (development/testing only)
powershell -ExecutionPolicy Bypass -File install-windows.ps1 -Repo yourname/AI-Case-Sorter-Py
```

## Portable installs

Drop an empty file named `portable.txt` next to `main.py` and the app keeps
its data in `<app>\data` instead of `%LOCALAPPDATA%`, for USB-stick or
self-contained use. The updater still works — it just won't be able to rely
on your data being outside the app folder, so it leaves `data\` alone
explicitly.

## Linux and macOS: `install-unix.sh`

```bash
sh install-unix.sh [--prefix DIR] [--version TAG] [--no-bootstrap] [--force] [--repo OWNER/REPO]
```

POSIX `sh` (dash, and macOS's bash 3.2), needing only `curl`, `tar` and
`sha256sum` or `shasum`. It follows the Windows installer and the in-app
updater rule for rule:

| Step | Detail |
|---|---|
| Release | `/releases/latest`, or `--version TAG`. Tags are checked against `updater._TAG_RE`; the sdist is matched by its exact name, falling back to the tag's source archive as `_pick_asset` does. A missing release is an error, never a branch install. |
| Checksum | The same policy as `classify_digest` / `Get-DigestCheck`: `sha256` verifies and a mismatch aborts, absent or unsupported proceeds with a warning, malformed is refused. |
| Extract | Every entry is vetted first, as `_safe_members` does: no absolute paths, `..`, `:` or `\` in a name, and nothing but regular files and directories. The single top-level folder is stripped. |
| Install | `~/.local/opt/ai-case-sorter` by default. A non-empty folder that is not a previous install is refused without `--force`. Re-running upgrades in place: `src/sorter` and `sorter` are replaced (the `PRUNE_ROOTS` of `apply_update.py`), everything else is copied over, and `PROTECTED_TOP_LEVEL` entries (`.venv`, `.uv`, `.env`, `data`, `portable.txt`, ...) are never written. |
| Launcher | `~/.local/bin/ai-case-sorter`, which runs the installed `start.sh`. An existing file there that this script did not write is refused without `--force`. |
| First run | Runs `start.sh --setup-only` — `bootstrap.py`'s whole first launch (uv, Python, `uv sync`, any `sudo` prompt for system libraries) minus starting the app — in the installer's own terminal, so a later launch from a menu has nothing slow or interactive left to do. `--no-bootstrap` skips it. A release whose `bootstrap.py` predates the flag is detected and skipped, since it would pass the flag to the app. |
| Log | `<data root>/logs/install-<stamp>.log`, the data root resolved as `paths.app_data_dir()` does (`CASESORTER_DATA_DIR`, `portable.txt`, then the OS default). Best-effort. |

It installs no system packages itself and creates no menu entries.

Its tests need no network: `installer/tests/test-install-unix.sh` sources
the script for its functions (`CASESORTER_INSTALL_LIB=1`) and drives full
installs from a synthetic sdist through two testing hooks,
`CASESORTER_INSTALL_RELEASE_JSON` and `CASESORTER_INSTALL_ARCHIVE`. Run it
with `sh`, `dash` or `bash`; `tests/unit/test_installer_scripts.py` runs it
under each one present.

## Testing the Windows installer locally

### Archive-entry validation and digest checks — run on any OS

`tests/Test-ArchiveEntryValidation.ps1` and `tests/Test-DigestVerification.ps1`
need no Windows. It dot-sources this
parent directory's script for its functions only (the guard on the main block stops
it installing anything), so it runs under PowerShell on Linux or macOS:

```bash
docker run --rm -v "$PWD:/w" -w /w mcr.microsoft.com/powershell:7.4-ubuntu-22.04 \
  pwsh -File installer/tests/Test-ArchiveEntryValidation.ps1
```

(and the same with `Test-DigestVerification.ps1`)

Run it from the repo root; it takes a few seconds. There is no bare `7.4`
tag on that registry — the tags are OS-qualified.

Two things it does **not** prove. It runs PowerShell 7.4, whereas a real
double-click through `install-windows.bat` runs **Windows PowerShell 5.1** —
which is why CI uses `shell: powershell`, and why this file's header warns
about BOM/codepage decoding. And they cover the entry-name and checksum
logic only, not winget, `tar.exe`, the registry, or the python.org bundle.

### The three provisioning paths — need a real Windows machine

The installer has three Python-provisioning paths. CI exercises all three on
every change to `installer/**` (the `installer-smoke` matrix:
`preinstalled` / `winget` / `pythonorg`); this is how to run the same three by
hand.

For every case: run from a repo checkout, and **pass `-Repo` for your fork** —
without it the default installs the upstream repo's latest release over your
app folder. Each run writes a transcript to
`%LOCALAPPDATA%\CaseSorter\logs\install-<timestamp>.log`; when the Python
bundle actually runs, its own logs land in `%TEMP%\Python 3.13.14*.log`.

**Case 1 — Python already installed.** Just run it:

```powershell
.\installer\install-windows.bat -Repo yourname/AI-Case-Sorter-Py
```

Expect `Found C:\...\python.exe`; no provisioning happens.

**Case 2 — no Python, winget path.** Uninstall Python first (Settings > Apps —
the python.org bundle registers a working Uninstall), then run the same
command. Expect `Installing Python (none suitable was found)` then
`Using winget: Python.Python.3.13`. Note winget's package does **not** include
the `py` launcher — which is why `start.bat` probes `py -3` *and* `python`.

**Case 3 — no Python, python.org fallback.** Same no-Python starting point,
plus make winget unresolvable for just this shell — it lives in the
WindowsApps directory, which nothing else in the installer needs (`tar.exe`
is in System32):

```powershell
$env:PATH = (($env:PATH -split ';') | Where-Object { $_ -notlike '*\Microsoft\WindowsApps*' }) -join ';'
.\installer\install-windows.bat -Repo yourname/AI-Case-Sorter-Py
```

Expect `winget is not available; using python.org.` then
`Downloading https://www.python.org/ftp/...`. The PATH change dies with the
window.

One run at a time: an install and an uninstall interleaved on the same
machine can strip a fresh install's registration mid-flight, leaving a
Python that works but cannot be uninstalled from Settings.

## Notes for maintainers

- **The repository must be publicly readable.** Both the installer and the
  in-app updater download over HTTPS with no credentials, so a private repo
  makes every request 404 — the release check falls back to the branch
  archive, and that 404s too:

  ```
  No published release found (the repo may have none yet).
  Invoke-WebRequest : Not Found
  ```

  If you need the repo to stay private, distribution has to move off GitHub
  (host the sdist plus a version manifest on your own server and repoint
  `$Repo` / `updater.DEFAULT_REPO`) — a token is not a workable answer for
  the audience this installer targets.
- **Cut a release before relying on the update path.** With no releases,
  `/releases/latest` 404s: the installer falls back to the default branch
  and the in-app updater reports "up to date" forever. Cut the first one via
  the Release workflow (see [`../RELEASING.md`](../RELEASING.md)); tags are
  PEP 440 with **no `v` prefix** (`0.1.0`, not `v0.1.0`) — `check-release.yml`
  rejects the prefixed form.
- The installer is unsigned, so SmartScreen will warn on first run. Signing
  (Azure Trusted Signing, or an OV/EV certificate) is the fix; until then,
  expect a "More info → Run anyway" step.
- `casesorter.ico` in this folder is picked up as the shortcut icon if
  present. It isn't in the repo yet — drop one in and shortcuts will use it.
- The updater reads `/releases/latest`, which excludes drafts and
  pre-releases, so tagging a pre-release won't push it to stable users.
- There is no version string to bump. The version is derived from the git tag
  at build time by hatch-vcs and baked into the sdist (as `sorter/_version.py`),
  so tagging *is* the bump — see [`../RELEASING.md`](../RELEASING.md). This is
  why the installer prefers the sdist: a source archive carries neither that
  file nor `.git`, so an install made from one reports `0.0.0+unknown` and
  re-prompts for the same update on every launch.
