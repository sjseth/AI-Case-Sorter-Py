#!/usr/bin/env python3
"""Cross-platform launcher bootstrap.

Replaces the platform-duplicated logic that used to live separately in
start.sh and start.bat -- both are now three-line shims that just call this
file. Having one implementation in one language means the bootstrap logic
is actually unit-testable, which the bash half never was.

This file has to run on whatever old Python ships with the user's system,
since its whole job is to provision a newer one via uv -- so it targets a
low floor deliberately (see the ruff per-file-ignore for "bootstrap.py" in
pyproject.toml: pyupgrade rewrites would happily rewrite this into
3.12-only syntax and silently break it on exactly the systems it exists to
serve) and imports nothing beyond the standard library.

What it does, in order:
  1. On Linux, offer to install libGL/glib via the system package manager
     if the app's dependencies need them and they're missing. Graphics
     libraries like libGL aren't part of a Python build -- they're system
     X11/GPU libraries opencv dlopens at runtime.
  2. Ensure uv is available, installing it via the official installer
     (pinned to UV_VERSION, not "latest") into a project-local .uv/ if it
     isn't already on PATH. The installer itself verifies a sha256 baked in
     at release time for each platform's binary; this script doesn't
     re-invent that.
  3. Apply any staged in-app update BEFORE syncing dependencies, so an
     update that changes pyproject.toml/uv.lock gets its new dependencies
     installed on this same launch. Mirrors the ordering start.sh used to
     encode around --apply-update and the old requirements.txt hash --
     only now there's no hash-based marker at all, because `uv sync`
     reconciles against the venv's actual contents rather than trusting a
     proxy for them.
  4. `uv sync --frozen --no-dev --no-install-project` -- --frozen takes the committed
     uv.lock as-is and never re-resolves on a user's machine, so launches
     stay deterministic and offline-safe (CI uses `--locked` instead, which
     fails if the lock has drifted from pyproject.toml -- see
     .github/workflows/build.yml). --no-install-project skips building the
     sorter package itself: step 5 puts src/ on PYTHONPATH instead, so it
     was never needed for that, and building it is actively harmful when
     there's no .git to derive a version from (see pyproject.toml's
     [tool.hatch.version] and src/sorter/__init__.py).
     --no-dev keeps the `dev` group (pytest, ruff) out of a user's venv --
     uv installs it by default, and it is pure CI tooling.
  5. Launch the app: `uv run --no-sync python -m sorter <forwarded args>`,
     with PYTHONPATH=src in the child's environment. --no-sync, not
     --frozen: `uv run` syncs implicitly by default even with --frozen,
     which would redo the very build step 4 skipped.

     PYTHONPATH is what makes `-m` work at all here -- the package is never
     installed into the venv (step 4), so there is otherwise nothing for it
     to resolve. Doing it this way keeps sys.path surgery out of the
     application entirely: `-m` also leaves `src/sorter` itself off the
     path, where subpackages like `data` and `update` would shadow
     same-named third-party ones.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

# Bump deliberately, not automatically -- re-verify the libGL behavior (see
# the module docstring) after bumping, the same way this version was chosen:
# by actually running `uv python install` and importing cv2, not by assuming.
UV_VERSION = "0.12.1"
UV_INSTALL_DIR = ROOT / ".uv" / "bin"


# Launch log. Started detached, and on Windows the console closes with the
# process, so a traceback is gone before it can be read. Best-effort: an
# unwritable data dir must never stop the app starting.

_log_file = None  # type: ignore[var-annotated]  # open file, or None if unavailable
_log_path = None  # type: ignore[var-annotated]


def open_log():
    """Start a fresh launch log, keeping the previous one beside it.

    Two files, so no rotation policy or cleanup that could itself fail.
    """
    global _log_file, _log_path
    try:
        sys.path.insert(0, str(SRC))
        from sorter.paths import logs_dir

        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        current = directory / "launch.log"
        if current.exists():
            # os.replace, not rename: on Windows rename onto an existing file
            # raises, and the previous log is exactly what we mean to replace.
            os.replace(str(current), str(directory / "launch.prev.log"))
        _log_file = open(str(current), "w", encoding="utf-8", errors="replace")
        _log_path = current
    except Exception:
        _log_file = None
        _log_path = None
    return _log_path


def close_log():
    """Release the launch log so another process can rotate it.

    Only the re-launch after an update needs this: the child calls open_log()
    too, and on Windows os.replace() on a file this process still holds open
    fails -- silently, since open_log swallows everything -- leaving that
    launch unlogged.
    """
    global _log_file
    if _log_file is None:
        return
    try:
        _log_file.close()
    except Exception:
        pass
    _log_file = None


def _record(line: str) -> None:
    if _log_file is None:
        return
    try:
        _log_file.write(line + "\n")
        _log_file.flush()  # the process is routinely killed, never closed cleanly
    except Exception:
        pass


# flush=True because stdout is block-buffered whenever it isn't a terminal,
# and this process ends by being killed while still inside main.py's Tk loop
# -- so an unflushed buffer is never written at all. That is not theoretical:
# build.yml's launcher-smoke redirects to a file and dumped it afterwards,
# and the file came back empty every run, which is why its comment used to
# say no [bootstrap] lines ever appeared. It also matters to a user watching
# a multi-minute first-launch sync: block-buffered progress messages arrive
# in 4 KB lumps, which reads as a hang.
def log(msg: str) -> None:
    line = f"[bootstrap] {msg}"
    print(line, flush=True)
    _record(line)


def warn(msg: str) -> None:
    line = f"[bootstrap] {msg}"
    print(line, file=sys.stderr, flush=True)
    _record(line)


# ---------------------------------------------------------------------------
# uv itself
# ---------------------------------------------------------------------------


def find_uv() -> str | None:
    # Deferred: sorter is stdlib-only-compatible (see CLAUDE.md), so this is
    # safe to import here, but doing it at module level would be one more
    # thing that could break before this script gets a chance to matter.
    # The logic lives in sorter/paths.py, not duplicated here, because
    # dialog_install_torch.py needs the same lookup after launch (a
    # uv-managed venv doesn't ship pip, so it uses `uv pip install` instead).
    sys.path.insert(0, str(SRC))
    from sorter.paths import find_uv as _find_uv

    return _find_uv()


def install_uv() -> str:
    log(f"uv not found; installing uv {UV_VERSION} into {UV_INSTALL_DIR} ...")
    UV_INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["UV_INSTALL_DIR"] = str(UV_INSTALL_DIR)
    env["UV_NO_MODIFY_PATH"] = "1"
    # Skip the receipt/self-update machinery the official installer sets up
    # for a normal user install -- this copy is project-local and managed by
    # this script, not by `uv self update`.
    env["UV_UNMANAGED_INSTALL"] = str(UV_INSTALL_DIR)

    base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}"
    if os.name == "nt":
        script_url = f"{base}/uv-installer.ps1"
        with urllib.request.urlopen(script_url, timeout=30) as resp:
            script = resp.read().decode("utf-8")
        # A temp file + -File, not piping the script via -Command - on stdin:
        # real Windows PowerShell (powershell.exe, not pwsh) is far less
        # reliable at reading a full multi-line script that way -- confirmed
        # the hard way in CI: the piped version exited 0 with no error at all
        # and simply never created the binary. -File is also what
        # installer/install-windows.bat already uses successfully for its
        # own .ps1, so it's a known-working pattern in this repo already.
        script_path = UV_INSTALL_DIR / "_uv-installer.ps1"
        script_path.write_text(script, encoding="utf-8")
        # Prefer pwsh (PowerShell 7) over the legacy powershell.exe (5.1) if
        # it's available: real CI failure, not a guess -- the installer
        # script itself failed with "the 'Get-ExecutionPolicy' command was
        # found ... but the module could not be loaded", a known symptom of
        # powershell.exe inheriting a $env:PSModulePath that doesn't include
        # 5.1's built-in module locations when spawned from a pwsh session
        # (which this script always is, directly or via bootstrap.py's own
        # caller). pwsh doesn't have that cross-version mismatch with itself.
        # Not all end-user machines have pwsh, though, so fall back to
        # powershell.exe -- it's what the official docs recommend and it
        # works fine outside a nested-pwsh context.
        ps_exe = "pwsh" if shutil.which("pwsh") else "powershell"
        try:
            subprocess.run(
                [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                env=env,
                check=True,
            )
        finally:
            script_path.unlink(missing_ok=True)
    else:
        script_url = f"{base}/uv-installer.sh"
        with urllib.request.urlopen(script_url, timeout=30) as resp:
            script = resp.read().decode("utf-8")
        subprocess.run(["sh"], input=script, text=True, env=env, check=True)

    uv = find_uv()
    if uv is None:
        raise SystemExit(
            "[bootstrap] The uv installer ran but uv still isn't where it should be "
            f"({UV_INSTALL_DIR}). Install uv yourself from https://docs.astral.sh/uv/ "
            "and re-run."
        )
    return uv


# ---------------------------------------------------------------------------
# Linux system packages -- not something uv or pip can install
# ---------------------------------------------------------------------------

_PKG_INSTALL = {
    "apt": ["sudo", "apt-get", "install", "-y"],
    "dnf": ["sudo", "dnf", "install", "-y"],
    "pacman": ["sudo", "pacman", "-S", "--noconfirm"],
}
_PKG_NAMES = {
    "apt": {"gl": "libgl1", "glib": "libglib2.0-0"},
    "dnf": {"gl": "mesa-libGL", "glib": "glib2"},
    "pacman": {"gl": "libglvnd", "glib": "glib2"},
}


def _detect_pkg_mgr() -> str | None:
    for binary, name in (("apt-get", "apt"), ("dnf", "dnf"), ("pacman", "pacman")):
        if shutil.which(binary):
            return name
    return None


def _try_install_system_pkg(feature: str, auto_install: bool) -> bool:
    mgr = _detect_pkg_mgr()
    if mgr is None:
        warn(f"Could not detect apt/dnf/pacman -- install the system {feature} library yourself.")
        return False
    pkg = _PKG_NAMES[mgr][feature]
    if not auto_install:
        if not sys.stdin.isatty():
            warn(f"Not running interactively; re-run with --auto to install '{pkg}' via sudo {mgr} automatically.")
            return False
        reply = input(f"[bootstrap] Install '{pkg}' via sudo {mgr}? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            return False
    cmd = _PKG_INSTALL[mgr] + [pkg]
    log(f"Running: {' '.join(cmd)}")
    return subprocess.call(cmd) == 0


def ensure_linux_runtime_libs(uv: str, auto_install: bool) -> None:
    """opencv dlopens libGL/glib at runtime -- system X11/graphics libraries,
    not part of a Python build. Probed the same way start.sh did: try the
    import for real, in the app's actual environment, and read the failure
    instead of guessing.

    The probe loops because the import reports only the *first* library it
    fails to find: on a minimal container or a fresh WSL image both libGL and
    glib are typically missing, and installing one just reveals the other. One
    pass would install libGL, return happy, and let the app die on glib with
    none of this guidance. The loop is bounded by the number of features we
    know how to install, so an unrecognised failure still exits rather than
    spinning."""
    if not sys.platform.startswith("linux"):
        return

    # (substring to match in stderr, _PKG_NAMES feature, message, hint)
    known = (
        ("libgl", "gl", "OpenCV needs libGL.so.1 from the system.", "Install the system OpenGL library and re-run."),
        (
            ("libgthread", "libglib"),
            "glib",
            "OpenCV needs glib from the system.",
            "Install glib2 (apt: libglib2.0-0) and re-run.",
        ),
    )

    # len(known) + 1 passes: one probe per library we might have to install,
    # plus a final probe to confirm the last install actually fixed it.
    for _ in range(len(known) + 1):
        probe = subprocess.run(
            [uv, "run", "--no-sync", "python", "-c", "import cv2"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return

        stderr = probe.stderr or ""
        haystack = stderr.lower()
        for needles, feature, message, hint in known:
            if isinstance(needles, str):
                needles = (needles,)
            if any(n in haystack for n in needles):
                log(message)
                if not _try_install_system_pkg(feature, auto_install):
                    raise SystemExit(f"[bootstrap] {hint}")
                break
        else:
            raise SystemExit(f"[bootstrap] OpenCV failed to import:\n{stderr}")

    # Every library we know about has been installed and it still won't import.
    raise SystemExit("[bootstrap] OpenCV still fails to import after installing its system libraries.")


# ---------------------------------------------------------------------------
# Staged self-update -- must run before `uv sync` so a staged update's own
# pyproject.toml/uv.lock is what gets synced. sorter.update.apply_update is
# stdlib-only by design (see its module docstring) specifically so it's
# importable here, before uv has put anything in a venv yet.
# ---------------------------------------------------------------------------


# Set on the process a re-launch starts, so an update that somehow stays
# pending can cost one extra launch and not an infinite chain of them.
RELAUNCH_ENV = "CASESORTER_BOOTSTRAP_RELAUNCHED"


def apply_pending_update() -> bool:
    """Apply a staged update. True if one was applied, i.e. this file changed."""
    sys.path.insert(0, str(SRC))
    from sorter.update.apply_update import apply_pre_launch

    # Never raises internally; a broken updater must never block launch.
    return apply_pre_launch()


def relaunch_after_update(forwarded: list[str]):
    """Re-run this file from disk after an update replaced it. None to carry on.

    An update rewrites bootstrap.py and moves the entry point it launches,
    but this process resolved both before that happened -- the module in
    memory and the path in run_app() are the previous release's. That is what
    breaks a layout change in either direction: an update that moves the
    entry point makes the in-memory path stale by definition, and the process
    holding it is the one that just installed the move.

    subprocess rather than os.execv: on Windows exec replaces the process
    image and cmd.exe treats the original as finished, returning to the
    prompt while the app is still starting -- which would defeat start.bat's
    pause-on-failure, the thing that keeps a launch error on screen.
    """
    if os.environ.get(RELAUNCH_ENV) == "1":
        warn("an update was applied again after a re-launch; continuing rather than looping.")
        return None

    env = dict(os.environ)
    env[RELAUNCH_ENV] = "1"
    log("update applied; re-launching with the new bootstrap ...")
    close_log()  # the child rotates the log this process is holding open
    return subprocess.call([sys.executable, str(Path(__file__).resolve())] + forwarded, cwd=str(ROOT), env=env)


# ---------------------------------------------------------------------------


def run_app(uv: str, forward_args: list[str]) -> int:
    """Launch the app, mirroring its output into the launch log.

    Reading the pipe and echoing each line keeps the output live *and* on
    disk. stderr is merged so the log reads in order; PYTHONUNBUFFERED keeps
    it honest now that stdout is a pipe. `uv sync` stays on inherited stdio --
    its progress bars would become junk log lines.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # What makes `-m sorter` resolve without installing the package. Setting it
    # here means no file in the tree has to rewrite sys.path to launch itself:
    # the launcher owns the environment, which is what a launcher is for.
    # Replaces rather than prepends, deliberately -- an inherited PYTHONPATH is
    # a classic source of "why did it import the other sorter".
    env["PYTHONPATH"] = str(SRC)

    # --no-sync, not --frozen: `uv run` syncs implicitly by default even with
    # --frozen (frozen only constrains *how* it syncs, not whether), which
    # would silently redo the project build main() went out of its way to
    # skip. --no-sync trusts that sync and skips its own.
    proc = subprocess.Popen(
        [uv, "run", "--no-sync", "python", "-m", "sorter"] + forward_args,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                print(line, flush=True)
                _record(line)
    except Exception:
        pass  # echoing is best-effort; a dead pipe must not orphan the app
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    return proc.wait()


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    auto_install = os.environ.get("AUTO_INSTALL", "0") in ("1", "true", "yes")
    forward_args = []
    for arg in args:
        if arg in ("--auto", "-y"):
            auto_install = True
        else:
            forward_args.append(arg)

    # Redirected stdout defaults to the locale encoding with errors='strict',
    # so one non-ASCII line from the app would kill the echo in run_app().
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass

    log_path = open_log()

    # Before anything can fail: the launch that needs these is the one that
    # dies too fast to ask about.
    log(f"app folder: {ROOT}")
    log(f"bootstrap python: {sys.version.split()[0]} ({sys.executable})")
    log(f"platform: {sys.platform} / {os.name}")
    if log_path is not None:
        log(f"logging this launch to {log_path}")

    uv = find_uv() or install_uv()
    log(f"uv: {uv}")

    from sorter.paths import is_installed_package

    log(f"installed package: {is_installed_package()}")

    if apply_pending_update():
        code = relaunch_after_update(args)
        if code is not None:
            return code

    log("Syncing dependencies with uv ...")
    # --no-install-project: don't build/install the sorter package itself as
    # part of the sync. main.py imports it straight from the source tree
    # (sys.path.insert), so it was never needed for that -- and building it
    # is actively harmful in exactly the context this matters most: a
    # downloaded release has no .git, and hatch-vcs's build hook (see
    # pyproject.toml) errors out without one unless a fallback is
    # configured, in which case it *overwrites* sorter/_version.py with that
    # fallback -- clobbering the real version the release workflow baked in.
    # Verified this exact failure mode against a real git-less copy of this
    # repo before adding the flag, not assumed.
    #
    # --inexact: `uv sync` is *exact* by default and removes anything in the
    # venv that isn't in the lockfile. torch/torchvision are the [ml] extra --
    # deliberately outside the default sync set, installed on demand by
    # dialog_install_torch.py straight into this same venv -- so an exact sync
    # here uninstalls PyTorch on every single launch. The user would train,
    # restart, and find it gone, with a multi-GB re-download to get back.
    # CI still syncs exactly (--locked in build.yml); being strict is the
    # whole point there. Here, a user-installed extra has to survive.
    #
    # --no-dev: uv installs the `dev` dependency group by default, so without
    # this every end user was getting pytest and ruff -- CI tooling, useless
    # on a sorting machine. CI asks for the group explicitly where it wants
    # it (`--only-group dev` in lint.yml, the default sync in build.yml), so
    # nothing there depends on the launcher's behavior. Note that --inexact
    # means this doesn't *remove* the group from installs that already have
    # it; it only stops new ones from acquiring it.
    try:
        subprocess.run(
            [uv, "sync", "--frozen", "--inexact", "--no-dev", "--no-install-project"],
            cwd=ROOT,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        # Every other failure path in this file exits with something a
        # non-developer can act on; a raw traceback here would be the odd one
        # out, and this is the step most likely to fail on a flaky network.
        message = (
            f"[bootstrap] Dependency sync failed (uv exited {exc.returncode}).\n"
            "[bootstrap] Check your network connection and re-run. If it persists, "
            "delete the .venv folder and try again."
        )
        # Copied into the log explicitly: the interpreter prints SystemExit's
        # message on the way out, so it never passes back through log() and
        # would otherwise be the one failure the log doesn't record.
        _record(message)
        raise SystemExit(message) from exc

    ensure_linux_runtime_libs(uv, auto_install)

    # Everything this file is responsible for is done at this point; from
    # here on the process is just the app. Worth saying out loud on a first
    # launch, where the sync above can run for minutes and the Tk window
    # takes a few more seconds to appear -- and build.yml's launcher-smoke
    # waits for exactly this line rather than a fixed timeout, since the
    # app never exits on its own.
    log("Starting the app ...")

    code = run_app(uv, forward_args)
    if code != 0:
        warn(f"The app exited with code {code}.")
        if _log_path is not None:
            warn(f"Full log of this launch: {_log_path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
