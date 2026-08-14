"""Guards on bootstrap.py and the launcher shims that call it.

bootstrap.py has to run on whatever old Python the user's system ships --
its whole job is to provision a newer one via uv -- so the properties worth
guarding are "doesn't import anything that isn't in the box" and "does what
the CLI contract promises", not the actual uv install/sync (that needs a
real network and a real uv release; exercised in CI's launcher-smoke job on
real runners, and manually against a real uv install while this was built).
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP_PATH = ROOT / "bootstrap.py"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap", BOOTSTRAP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_popen(returncode: int = 0, output: list[str] | None = None) -> MagicMock:
    """Stand-in for the piped app process run_app() drives.

    stdout has to be iterable rather than None for the tee loop to be
    exercised at all, which is the half of run_app worth testing, and
    closable because run_app releases the pipe before it waits.
    """
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.__iter__.return_value = iter(output or [])
    proc.wait.return_value = returncode
    return proc


@pytest.fixture
def bootstrap(monkeypatch):
    # Never let a test accidentally shell out to a real uv/sudo/network call.
    module = _load_bootstrap()
    monkeypatch.setattr(module.subprocess, "run", MagicMock())
    monkeypatch.setattr(module.subprocess, "call", MagicMock(return_value=0))
    # The app launch is a Popen now, not a run: run_app() reads its output
    # line by line so the launch log captures a traceback that would
    # otherwise die with the console window.
    monkeypatch.setattr(module.subprocess, "Popen", MagicMock(return_value=_fake_popen()))
    # Keep the launch log out of the developer's real data directory.
    monkeypatch.setattr(module, "open_log", lambda: None)
    return module


def test_bootstrap_py_is_stdlib_only_at_module_level() -> None:
    """Deferred, function-scoped imports (e.g. sorter.update.apply_update) are fine
    -- sorter itself is stdlib-only by design (see CLAUDE.md). What must
    never happen is a *module-level* import of anything third-party, since
    that would break before this script gets a chance to provision uv."""
    tree = ast.parse(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    scoped = {
        n
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for n in ast.walk(node)
    }

    top_level_modules: set[str] = set()
    # Walk rather than iterate tree.body: an import inside a module-level
    # `if`/`try` still runs at import time, and `try: import requests /
    # except ImportError:` is exactly the shape an optional-dependency probe
    # takes. Only imports nested in a def/class are deferred, so those are
    # what gets excluded -- not everything below the top statement level.
    for node in ast.walk(tree):
        if node in scoped:
            continue
        if isinstance(node, ast.Import):
            top_level_modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_modules.add(node.module.split(".")[0])

    allowed = set(sys.stdlib_module_names) | {"__future__"}
    offenders = top_level_modules - allowed
    assert not offenders, f"bootstrap.py imports non-stdlib module(s) at module level: {offenders}"


# Every subpackage under src/sorter/. Listed rather than discovered so that
# adding one is a deliberate decision about whether it may import anything.
SORTER_SUBPACKAGES = (
    "sorter.community",
    "sorter.control",
    "sorter.data",
    "sorter.hardware",
    "sorter.ml",
    "sorter.ui",
    "sorter.training",
    "sorter.update",
)


def test_prelaunch_sorter_modules_import_without_third_party_packages() -> None:
    """``sorter.paths`` and ``sorter.update.apply_update`` are imported by
    ``bootstrap.py`` *before* ``uv sync`` runs (see its module docstring),
    against a venv that may hold zero third-party packages. Neither must ever
    pull one in -- not even transitively through a subpackage's
    ``__init__.py``, which is exactly why every subpackage ``__init__.py``
    under ``src/sorter/`` is deliberately empty: a re-export added to one of
    them would silently break every end user's first launch, and nothing in
    the normal test suite would catch it, because the suite always runs in a
    fully-synced venv where the extra import just succeeds.

    Every subpackage is imported here, not just the two on the pre-launch
    path. ``sorter.update`` alone is what ``apply_update`` drags in, so a
    ``requests`` re-export in ``community/__init__.py`` would sail past a
    narrower check -- and then break the launch as soon as anything imported
    it before the sync.

    ``-S`` drops site-packages from ``sys.path`` (stdlib itself is found via
    the interpreter's own path configuration, not `site`), which is the same
    view of the world this code has at the point ``bootstrap.py`` actually
    imports it.
    """
    src = ROOT / "src"
    imports = ", ".join(("sorter.paths", "sorter.update.apply_update") + SORTER_SUBPACKAGES)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import sys; sys.path.insert(0, sys.argv[1]); import {imports}",
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_no_sorter_subpackage_init_imports_anything() -> None:
    """The reason the import test above can pass: nothing to re-export.

    Asserted structurally as well, because the ``-S`` run only proves the
    imports resolve in a stripped environment. A re-export of a *stdlib-only*
    sibling passes that while still making ``import sorter.data`` drag in the
    whole persistence layer -- and the next re-export added beside it is the
    one that reaches ``sqlite_utils``. A module docstring is fine; an import
    of any kind is not.
    """
    offenders = {}
    for dotted in SORTER_SUBPACKAGES:
        init = ROOT / "src" / Path(*dotted.split(".")) / "__init__.py"
        tree = ast.parse(init.read_text(encoding="utf-8"))
        found = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        if found:
            offenders[dotted] = found
    assert not offenders, f"subpackage __init__.py files must not import anything: {offenders}"


def test_bootstrap_py_compiles() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(BOOTSTRAP_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_main_consumes_auto_flags_and_forwards_the_rest(bootstrap, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=False))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)

    bootstrap.main(["--auto", "--some-app-flag", "value"])

    launched_argv = bootstrap.subprocess.Popen.call_args.args[0]
    assert launched_argv[:2] == ["/fake/uv", "run"]
    assert "--auto" not in launched_argv
    assert "--some-app-flag" in launched_argv
    assert "value" in launched_argv


def test_main_dash_y_is_equivalent_to_auto(bootstrap, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=False))
    seen = {}

    def fake_ensure(_uv, auto_install):
        seen["auto_install"] = auto_install

    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", fake_ensure)
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)
    monkeypatch.setattr(bootstrap.subprocess, "run", MagicMock(returncode=0))

    bootstrap.main(["-y"])
    assert seen["auto_install"] is True


def test_apply_update_runs_before_sync(bootstrap, monkeypatch) -> None:
    """The one ordering invariant carried over from start.sh/start.bat: a
    staged update must be applied before dependencies sync, so an update
    that changes pyproject.toml/uv.lock gets its new dependencies installed
    on this same launch."""
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)
    monkeypatch.setattr(bootstrap.subprocess, "run", MagicMock(returncode=0))

    order = []
    monkeypatch.setattr(bootstrap, "apply_pending_update", lambda: order.append("apply_update"))
    real_run = bootstrap.subprocess.run

    def tracking_run(cmd, *a, **kw):
        if cmd[1:2] == ["sync"]:
            order.append("uv_sync")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(bootstrap.subprocess, "run", tracking_run)

    bootstrap.main([])
    assert order == ["apply_update", "uv_sync"]


def test_sync_is_inexact_so_the_ml_extra_survives(bootstrap, monkeypatch) -> None:
    """`uv sync` is exact by default and prunes anything absent from the
    lockfile. torch/torchvision are the [ml] extra -- installed on demand by
    dialog_install_torch.py into this same venv, outside the lock -- so
    without --inexact every launch silently uninstalls PyTorch."""
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=False))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)

    sync_cmd = []

    def tracking_run(cmd, *a, **kw):
        if cmd[1:2] == ["sync"]:
            sync_cmd.extend(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", tracking_run)

    bootstrap.main([])
    assert "--inexact" in sync_cmd, f"launcher sync would prune the [ml] extra: {sync_cmd}"


def test_sync_skips_the_dev_group(bootstrap, monkeypatch) -> None:
    """uv installs the `dev` dependency group by default. That group is CI
    tooling (pytest, ruff) and has no business in an end user's venv -- the
    launcher must ask for it to be left out."""
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=False))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)

    sync_cmd = []

    def tracking_run(cmd, *a, **kw):
        if cmd[1:2] == ["sync"]:
            sync_cmd.extend(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", tracking_run)

    bootstrap.main([])
    assert "--no-dev" in sync_cmd, f"launcher sync would install pytest and ruff: {sync_cmd}"


def test_sync_failure_exits_with_a_readable_message(bootstrap, monkeypatch) -> None:
    """The audience for this script is a non-developer who downloaded a ZIP;
    a raw CalledProcessError traceback is not an actionable failure."""
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=False))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)

    def failing_run(cmd, *a, **kw):
        if cmd[1:2] == ["sync"]:
            raise bootstrap.subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", failing_run)

    with pytest.raises(SystemExit) as excinfo:
        bootstrap.main([])
    assert "Dependency sync failed" in str(excinfo.value)


def test_run_app_mirrors_output_into_the_log(bootstrap, monkeypatch, capsys) -> None:
    """Why the launch is piped at all: on Windows the console window closes
    with the process, so a traceback out of main.py is on screen for an
    instant and then gone. It has to reach the log as well -- and still reach
    the screen, or a user watching a first launch sees nothing at all."""
    recorded: list[str] = []
    monkeypatch.setattr(bootstrap, "_record", recorded.append)
    monkeypatch.setattr(
        bootstrap.subprocess,
        "Popen",
        MagicMock(return_value=_fake_popen(3, ["Traceback (most recent call last):\n", "  ImportError: boom\n"])),
    )

    assert bootstrap.run_app("/fake/uv", []) == 3
    assert "Traceback (most recent call last):" in recorded
    assert "  ImportError: boom" in recorded
    assert "ImportError: boom" in capsys.readouterr().out


def test_run_app_reaps_the_child_when_the_echo_dies(bootstrap, monkeypatch) -> None:
    """The echo is best-effort; reaping the child is not.

    Redirected stdout defaults to the locale encoding with errors='strict',
    so a single non-ASCII line -- apply_update.py logs one on every in-app
    update -- used to raise straight out of the loop, skip wait(), and leave
    the app running with nothing attached to it. That is the silent-launch
    failure this whole path exists to report, produced by the reporting code.
    """
    proc = _fake_popen(0, ["applying 1.0.0 → 1.1.0\n"])
    monkeypatch.setattr(bootstrap.subprocess, "Popen", MagicMock(return_value=proc))
    monkeypatch.setattr(
        bootstrap,
        "_record",
        MagicMock(side_effect=UnicodeEncodeError("cp1252", "→", 0, 1, "boom")),
    )

    assert bootstrap.run_app("/fake/uv", []) == 0
    proc.wait.assert_called_once()


def test_main_makes_its_streams_tolerate_non_ascii(bootstrap, monkeypatch) -> None:
    """Fixing the reaping alone would still lose the rest of the output."""
    seen: list[dict] = []
    for name in ("stdout", "stderr"):
        stream = MagicMock()
        stream.reconfigure = lambda **kw: seen.append(kw)
        monkeypatch.setattr(bootstrap.sys, name, stream)
    monkeypatch.setattr(bootstrap, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(bootstrap, "apply_pending_update", lambda: False)
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", lambda *a, **kw: None)
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)
    monkeypatch.setattr(bootstrap, "run_app", lambda *a, **kw: 0)

    bootstrap.main([])

    assert seen == [{"errors": "replace"}, {"errors": "replace"}]


def test_run_app_unbuffers_the_child(bootstrap, monkeypatch) -> None:
    """The child's stdout is a pipe now, so it would be block-buffered by
    default -- output would arrive in lumps, out of order with the stderr
    merged into it, and be lost entirely if the process is killed."""
    monkeypatch.setattr(bootstrap.subprocess, "Popen", MagicMock(return_value=_fake_popen()))

    bootstrap.run_app("/fake/uv", [])

    kwargs = bootstrap.subprocess.Popen.call_args.kwargs
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert kwargs["stderr"] == bootstrap.subprocess.STDOUT


def test_run_app_launches_the_entry_point_that_exists(bootstrap, monkeypatch) -> None:
    """Nothing else pins this, and getting it wrong kills every launch.

    CI can't cover for it: launcher-smoke declares success on the
    "Starting the app" log line, which is emitted *before* this Popen, and
    then only checks the venv it built. So a typo or a later relocation of
    ``__main__.py`` would ship a release that dies instantly with the whole
    matrix green -- which is not hypothetical, since this PR is itself the
    relocation. Assert the argv, and assert the file is really there.
    """
    monkeypatch.setattr(bootstrap.subprocess, "Popen", MagicMock(return_value=_fake_popen()))

    bootstrap.run_app("/fake/uv", ["--flag"])

    argv = bootstrap.subprocess.Popen.call_args.args[0]
    assert argv == ["/fake/uv", "run", "--no-sync", "python", "-m", "sorter", "--flag"]
    # `-m` resolves through PYTHONPATH, not an install, so the two are one fact.
    assert bootstrap.subprocess.Popen.call_args.kwargs["env"]["PYTHONPATH"] == str(ROOT / "src")
    assert (ROOT / "src" / "sorter" / "__main__.py").is_file()


def test_main_relaunches_itself_after_applying_an_update(bootstrap, monkeypatch) -> None:
    """An update replaces bootstrap.py and can move the entry point.

    Both were resolved by this process before that happened, so carrying on
    would sync and then launch with the *previous* release's path. That is
    what breaks a layout change in either direction -- most visibly a
    downgrade, where the update replaces the whole tree under a
    ``bootstrap.py`` this process already loaded.
    """
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=True))
    monkeypatch.setattr(bootstrap.subprocess, "call", MagicMock(return_value=7))
    monkeypatch.setattr(bootstrap, "run_app", MagicMock(return_value=0))

    assert bootstrap.main(["--some-app-flag"]) == 7

    relaunch_argv = bootstrap.subprocess.call.call_args.args[0]
    assert relaunch_argv == [sys.executable, str(BOOTSTRAP_PATH), "--some-app-flag"]
    assert bootstrap.subprocess.call.call_args.kwargs["env"][bootstrap.RELAUNCH_ENV] == "1"
    bootstrap.run_app.assert_not_called()
    bootstrap.subprocess.run.assert_not_called()  # the child does the sync


def test_relaunch_happens_at_most_once(bootstrap, monkeypatch) -> None:
    """The guard the re-launch needs, or a permanently-pending update loops.

    ``apply_pending`` discards the staged tree whether or not it applied
    cleanly, so this shouldn't trigger -- but "shouldn't" is not a reason to
    let the failure mode be an unbounded chain of processes.
    """
    monkeypatch.setattr(bootstrap, "find_uv", MagicMock(return_value="/fake/uv"))
    monkeypatch.setattr(bootstrap, "ensure_linux_runtime_libs", MagicMock())
    monkeypatch.setattr(bootstrap, "ensure_qt_platform_libs", lambda *a, **kw: None)
    monkeypatch.setattr(bootstrap, "apply_pending_update", MagicMock(return_value=True))
    monkeypatch.setattr(bootstrap.subprocess, "call", MagicMock(return_value=0))
    monkeypatch.setattr(bootstrap, "run_app", MagicMock(return_value=0))
    monkeypatch.setenv(bootstrap.RELAUNCH_ENV, "1")

    assert bootstrap.main([]) == 0

    bootstrap.subprocess.call.assert_not_called()
    bootstrap.run_app.assert_called_once()


def test_open_log_keeps_the_previous_launch(monkeypatch, tmp_path) -> None:
    """A failing launch is usually reported after the user has already tried
    again, so the log of the run that broke has to survive one more start."""
    module = _load_bootstrap()
    from sorter import paths

    logs = tmp_path / "logs"
    monkeypatch.setattr(paths, "logs_dir", lambda: logs)

    assert module.open_log() == logs / "launch.log"
    module.log("first launch")
    module._log_file.close()

    module.open_log()
    module.log("second launch")
    module._log_file.close()

    assert "first launch" in (logs / "launch.prev.log").read_text(encoding="utf-8")
    assert "second launch" in (logs / "launch.log").read_text(encoding="utf-8")


def test_open_log_failure_never_blocks_the_launch(monkeypatch, tmp_path) -> None:
    """An unwritable data directory is a reason to lose the log, never a
    reason to refuse to start the app."""
    module = _load_bootstrap()
    from sorter import paths

    def unwritable():
        raise OSError("read-only file system")

    monkeypatch.setattr(paths, "logs_dir", unwritable)

    assert module.open_log() is None
    module.log("still prints")  # must not raise


def test_the_xcb_probe_offers_the_install_when_a_display_is_present(bootstrap, monkeypatch) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)

    installed = []
    monkeypatch.setattr(
        bootstrap,
        "_try_install_system_pkg",
        lambda feature, auto_install: installed.append(feature) or True,
    )

    bootstrap.ensure_qt_platform_libs(auto_install=True)
    assert installed == ["xcb-cursor"]


def test_the_xcb_probe_stays_out_of_a_headless_launch(bootstrap, monkeypatch) -> None:
    """No display means the offscreen platform, which needs no xcb libraries —
    and CI's launcher smoke test must never be asked for sudo."""
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    monkeypatch.setattr(
        bootstrap,
        "_try_install_system_pkg",
        lambda feature, auto_install: pytest.fail("probed without a display"),
    )

    bootstrap.ensure_qt_platform_libs(auto_install=True)


def test_a_declined_xcb_install_still_lets_the_app_start(bootstrap, monkeypatch) -> None:
    """Qt asks for `xcb;wayland`, so the missing library costs the Wayland
    fallback's limitations, not the launch."""
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    monkeypatch.setattr(bootstrap, "_try_install_system_pkg", lambda feature, auto_install: False)

    bootstrap.ensure_qt_platform_libs(auto_install=False)  # must not raise


def test_runtime_lib_probe_retries_for_each_known_library(bootstrap, monkeypatch) -> None:
    """The cv2 import reports only the first missing library, so a box short
    of both libGL and glib needs a second pass -- otherwise bootstrap installs
    libGL, declares success, and the app dies on glib with no guidance."""
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")

    installed = []
    monkeypatch.setattr(
        bootstrap,
        "_try_install_system_pkg",
        lambda feature, auto_install: installed.append(feature) or True,
    )

    errors = [
        "ImportError: libGL.so.1: cannot open shared object file",
        "ImportError: libgthread-2.0.so.0: cannot open shared object file",
    ]

    def fake_run(cmd, *a, **kw):
        if errors:
            return MagicMock(returncode=1, stderr=errors.pop(0))
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    bootstrap.ensure_linux_runtime_libs("/fake/uv", auto_install=True)
    assert installed == ["gl", "glib"]


def test_find_uv_delegates_to_sorter_paths(bootstrap, monkeypatch) -> None:
    """The actual lookup logic is tested in tests/test_paths.py -- it lives
    in sorter/paths.py so dialog_install_torch.py can reuse it after launch.
    This only checks bootstrap.py calls through to it correctly."""
    from sorter import paths

    monkeypatch.setattr(paths, "find_uv", MagicMock(return_value="/sentinel/uv"))
    assert bootstrap.find_uv() == "/sentinel/uv"


def test_requirements_txt_is_gone() -> None:
    """Superseded by pyproject.toml + uv.lock; keeping both would let them
    drift the way requirements.txt and pyproject.toml already had (pygrabber
    was declared in one but not the other)."""
    assert not (ROOT / "requirements.txt").exists()


def test_uv_lock_is_committed() -> None:
    assert (ROOT / "uv.lock").is_file()


def test_python_version_pin_exists() -> None:
    assert (ROOT / ".python-version").is_file()


def test_start_bat_does_not_trust_a_bare_python_on_path() -> None:
    """A presence check on `python` is not enough on Windows, and the machine
    this installer *creates* is the one it fails on.

    Stock Windows ships an App Execution Alias at
    ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe`` -- a zero-byte
    reparse point with no Python behind it. ``where python`` finds it, so a
    presence check passes; running it prints "Python was not found..." and
    exits 9009. A winget-provisioned Python puts only its Launcher directory
    on PATH, never Python's own, so on a freshly installed machine the stub
    is the *only* thing that answers to `python`.

    That combination shipped: the installer reported success, start.bat hit
    the stub, and the console closed on the error before it could be read.
    Verified by A/B-ing both shims under a PATH of System32 + the stub + the
    Launcher: the old one printed the stub's message and never reached
    bootstrap.py; this one ran it.

    So the guard is: probe by *executing* an interpreter (the only thing that
    distinguishes the stub), and try the `py` launcher, which is what is
    actually on PATH after a winget install.
    """
    text = (ROOT / "start.bat").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().lower().startswith(("rem ", "@rem ")))

    assert "py -3" in code, "start.bat must try the py launcher; a winget install leaves only it on PATH"
    assert '-c "pass"' in code, (
        "start.bat must probe by running an interpreter -- executing it is the only way to tell a "
        "real Python from the Microsoft Store alias stub, which satisfies `where python` and then "
        "exits 9009"
    )
    assert "where python" not in code, (
        "`where python` is satisfied by the Store alias stub, which is exactly the failure this probe replaced"
    )
    assert "exit /b %RC%" in code, (
        "start.bat must propagate the app's exit code; the version that ended in a bare `endlocal` "
        "swallowed the stub's 9009, so nothing upstream could detect the failure either"
    )
    for invocation in ("py -3 ", "python -c", "%PY% "):
        assert f"call {invocation}" in code, (
            f"start.bat must `call {invocation.strip()}`: cmd invoking a .bat/.cmd shim without `call` "
            "transfers control and never returns, so a shimmed interpreter (pyenv-win, which also "
            "ships no py.exe) would end start.bat mid-probe with no output at all"
        )
    assert "pause" not in code, (
        "the Start Menu shortcut opens this console minimised, so `pause` waits out of sight "
        "forever; the failure notice has to time out on its own"
    )


@pytest.mark.parametrize("shim", ["start.sh", "start.bat"])
def test_shims_are_thin_and_delegate_to_bootstrap(shim: str) -> None:
    """The invariant is *what* the shim does, not how many lines it takes.

    A shim may do only the two things bootstrap.py cannot do for itself:
    find an interpreter to run it with, and report a failure to whoever is
    watching. Provisioning -- venv creation, dependency install, uv -- is
    bootstrap.py's job, and duplicating it per-platform is the mess this
    file exists to prevent. The old check was a line count, which flagged
    the interpreter probe start.bat needs to survive the Windows Store stub
    while it would happily have passed a compact `pip install` one-liner.
    """
    text = (ROOT / shim).read_text(encoding="utf-8")
    assert "bootstrap.py" in text

    body = "\n".join(
        line for line in text.splitlines() if line.strip() and not line.strip().startswith(("#", "REM ", "rem "))
    ).lower()
    for forbidden in ("uv sync", "uv run", "pip install", "venv", "requirements"):
        assert forbidden not in body, (
            f"{shim} contains '{forbidden}' -- provisioning belongs in bootstrap.py, not duplicated per-platform again."
        )

    non_blank_lines = [line for line in text.splitlines() if line.strip()]
    assert len(non_blank_lines) < 45, (
        f"{shim} has grown to {len(non_blank_lines)} non-blank lines; it should be doing "
        "nothing but locating an interpreter and reporting failure."
    )
