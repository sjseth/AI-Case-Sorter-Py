# Windows installer

For people who just want to run the app — no git, no Python, no terminal.

## For users

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
| App | Downloads the latest release's sdist (`ai_case_sorter-<version>.tar.gz`) over HTTPS and extracts it with `tar.exe`. Falls back to the source archive if that asset is absent. **No git.** |
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

## Testing the installer locally

### Archive-entry validation — runs on any OS

`tests/Test-ArchiveEntryValidation.ps1` needs no Windows. It dot-sources this
parent directory's script for its functions only (the guard on the main block stops
it installing anything), so it runs under PowerShell on Linux or macOS:

```bash
docker run --rm -v "$PWD:/w" -w /w mcr.microsoft.com/powershell:7.4-ubuntu-22.04 \
  pwsh -File installer/tests/Test-ArchiveEntryValidation.ps1
```

Run it from the repo root; it takes a few seconds. There is no bare `7.4`
tag on that registry — the tags are OS-qualified.

Two things it does **not** prove. It runs PowerShell 7.4, whereas a real
double-click through `install-windows.bat` runs **Windows PowerShell 5.1** —
which is why CI uses `shell: powershell`, and why this file's header warns
about BOM/codepage decoding. And it covers the entry-name checks only, not
winget, `tar.exe`, the registry, or the python.org bundle.

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
- `casesorter.ico` in this folder is the Start Menu shortcut's icon. It is
  **generated, not drawn**: `tools/make_app_icons.py` renders it from the
  launcher artwork in `src/sorter/ui/icons.py`, so the shortcut, the Linux menu
  entry and the running window are all the same mark. Re-run that tool and
  commit the result if the artwork changes; the shortcut code still guards on
  `Test-Path`, so a missing file costs the icon and not the install.
- The running app's taskbar button does **not** merge with the pinned
  shortcut. Windows groups by AppUserModelID: the app sets its own (see
  `sorter/ui/desktop_integration.py`), but writing the matching ID into the
  `.lnk` needs `IPropertyStore`, which `WScript.Shell` cannot reach. Fixing it
  means hand-rolled COM interop in PowerShell; the cost of not fixing it is two
  taskbar entries carrying the same icon.
- The updater reads `/releases/latest`, which excludes drafts and
  pre-releases, so tagging a pre-release won't push it to stable users.
- There is no version string to bump. The version is derived from the git tag
  at build time by hatch-vcs and baked into the sdist (as `sorter/_version.py`),
  so tagging *is* the bump — see [`../RELEASING.md`](../RELEASING.md). This is
  why the installer prefers the sdist: a source archive carries neither that
  file nor `.git`, so an install made from one reports `0.0.0+unknown` and
  re-prompts for the same update on every launch.
