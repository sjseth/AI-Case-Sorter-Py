# Download and Install

This page installs the **application** — the desktop program that drives the
sorter. The machine itself is a separate project with its own build
documentation; nothing here wires anything up.

Once it is installed, go on to [Getting Started](getting-started.md).

## What you need

- **Windows or Linux.** macOS runs from source.
- **A webcam** — the app photographs each case with it.
- **The CS7.2 sorter** on a USB serial port, for actual sorting. Without it
  the app still runs: pick the **Emulated** port and everything except moving
  brass works.
- **Disk space for a model.** A trained checkpoint and its training images are
  hundreds of megabytes.

PyTorch is **not** in this list. It is only needed to train or run a model
locally, it is large, and the app installs it for you the first time something
actually needs it — see [PyTorch](#pytorch) below.

## Windows

No git, no Python, no terminal.

1. Download **`install-windows.bat`** and **`install-windows.ps1`** from
   [`installer/`](https://github.com/sjseth/AI-Case-Sorter-Py/tree/main/installer)
   into the same folder.
2. Double-click `install-windows.bat`.

It installs Python if you don't have it, puts the app in
`%LOCALAPPDATA%\Programs\CaseSorter` (per-user — **no admin rights**), adds a
Start Menu entry, and launches. The first launch downloads the app's
dependencies and takes a few minutes; after that it starts immediately.

> **Windows will warn you the first time.** The installer is unsigned, so
> Microsoft Defender SmartScreen shows *"Windows protected your PC"*. Choose
> **More info → Run anyway**. Alternatively, right-click the downloaded file →
> **Properties** → tick **Unblock**, and it won't warn at all.

Re-running the installer is a safe way to repair a broken install. You do not
need it for updates — see [Updating](#updating).

Full details, options (`-InstallDir`, `-Version`, `-NoLaunch`) and the log
locations are in
[`installer/README.md`](https://github.com/sjseth/AI-Case-Sorter-Py/blob/main/installer/README.md).

## Linux and macOS

Run it from a checkout. The launch script installs
[uv](https://docs.astral.sh/uv/) if it isn't already there, uses it to fetch
the right Python and the dependencies, and starts the app — every time, in one
step.

```bash
git clone https://github.com/sjseth/AI-Case-Sorter-Py.git
cd AI-Case-Sorter-Py
./start.sh
```

The one thing uv can't provide is system libraries. On a minimal Linux install
the script offers to install them with `sudo`; pass `--auto` (or set
`AUTO_INSTALL=1`) to confirm automatically.

| Library | Needed by | Debian/Ubuntu | Fedora | Arch |
|---|---|---|---|---|
| libGL | OpenCV | `libgl1` | `mesa-libGL` | `libglvnd` |
| glib | OpenCV | `libglib2.0-0` | `glib2` | `glib2` |
| libxcb-cursor | Qt's X11 plugin | `libxcb-cursor0` | `xcb-util-cursor` | `xcb-util-cursor` |

The first two are required. The third is not — without it Qt falls back to
Wayland, where a floating [panel](guide/GUIDE.md#panels) can't be moved or
resized.

You need **some** Python 3 already on the machine, new enough to run
`bootstrap.py` — 3.12 or newer. That is not the Python the app itself runs on:
uv provisions that separately.

The same checkout works on Windows: `start.bat` instead of `./start.sh`.

## Starting the app

- **Windows, installed:** Start Menu → **AI Case Sorter**.
- **From a checkout:** `./start.sh` (Linux/macOS) or `start.bat` (Windows).

Every launch re-checks the dependencies against the lockfile, so pulling a
change that adds one needs nothing extra from you.

The window opens on the [Sort dashboard](guide/GUIDE.md#sort-dashboard). A
fresh install has nothing connected and nothing trained yet, so it shows a
short setup panel rather than an empty slot grid —
[Getting Started](getting-started.md) picks up from there.

## Where your data lives

Everything the app writes — models, training images, settings, logs — lives in
one folder, **outside** the app directory:

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\CaseSorter` |
| Linux | `~/.local/share/CaseSorter` (or `$XDG_DATA_HOME/CaseSorter`) |
| macOS | `~/Library/Application Support/CaseSorter` |

**File → Open data folder** opens it. Keeping it separate is what makes
updating safe: the updater replaces the app folder, and nothing of yours is in
it. Deleting the folder resets the app to a fresh install.

Two overrides: set `CASESORTER_DATA_DIR` to put the data anywhere you like, or
create an empty `portable.txt` next to `bootstrap.py` to keep it in
`<app>/data` — for USB-stick installs.

## Updating

**Updates happen inside the app.** It checks shortly after starting and offers
what it finds in the status bar; **Help → Check for updates…** asks
immediately.

Downloading only *stages* the update — nothing is replaced until you restart,
and the button becomes **Restart now** when it is ready. The dialog's **Choose
a different version…** lists every published release, older ones included, and
optionally pre-releases. See [Updates](guide/GUIDE.md#updates) for the whole
dialog.

- **Installed on Windows:** never re-run the installer for an update.
- **Running from a checkout:** `git pull` and launch again. The in-app updater
  still works, but a checkout is normally managed with git.
- Turn the automatic check off in the dialog, or set
  `CASESORTER_UPDATE_DISABLED=1`.

> **Installs older than 1.1.0 need one step in between.** Versions 1.0.0 and
> 1.0.1 can't update straight to 2.x — their updater rejects the newer archive
> layout, and that check can't be patched remotely. Update to 1.1.0 first with
> **Choose a different version…**, then to the current release.

## PyTorch

Training a model, or running one locally, needs PyTorch. The app asks the
first time you do something that requires it and installs it into its own
environment — an AI Config user is never prompted.

- **GPU:** an NVIDIA card with compute capability ≥ 8.0 (RTX 30-series or
  newer) is used automatically. Anything else falls back to CPU, which works
  and is slower.
- **AI Config mode needs no PyTorch at all** — the server does the
  classifying.

If you would rather install it yourself, `uv sync --extra ml` from a checkout
does it, but read the CUDA-build note in the
[README](https://github.com/sjseth/AI-Case-Sorter-Py#optional-pytorch) first:
the two routes ship different builds, and PyPI's Windows wheel is CPU-only.

## When something goes wrong

Both halves of the launch leave a log under the data folder, in `logs/`:
`install-<timestamp>.log` from the Windows installer, and `launch.log` from
every start of the app (the run before is kept as `launch.prev.log`).

On Windows the console closes with the process and takes any traceback with
it, so *"nothing happened when I clicked it"* is nearly always answered by
`launch.log`. Attach both when reporting a problem — see
[Troubleshooting](troubleshooting.md).
