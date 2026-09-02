"""The desktop entry, the icon set, and the identity that ties them to a window.

Nothing here asserts a desktop's *behavior* — that belongs to GNOME and KDE and
can't be pinned from a test. What is pinned is the contract those desktops read:
the freedesktop paths, the three keys that decide "which app is this window",
that every icon rung is a real image at exactly the size its path claims, and
that a second launch is a no-op. ``tests/integration/test_desktop_entry.py``
takes the same file to the real ``desktop-file-validate``.

The macOS block at the foot runs only on the matrix's darwin legs, where it
writes a real bundle into a temp home. The line it stops at is the same one:
a runner has no GUI session, so whether the *Dock* shows this icon and this
name is the one claim here that still needs a human.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from sorter.ui import desktop_integration as di
from sorter.ui import icons


@pytest.fixture
def linux_session(qapp, tmp_path, monkeypatch):
    """A writable XDG data home, on a machine that looks like a Linux desktop.

    ``Path.home`` is redirected as well as ``XDG_DATA_HOME``, and not for
    belt-and-braces: it is the fallback these tests must never reach. A bug in
    the env handling otherwise sends every one of them into the *real* home
    directory, where they pass — that is exactly how the first Windows run of
    this file failed, having written a live menu entry onto the runner.
    """
    data_home = tmp_path / "share"
    monkeypatch.setattr(di.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv(di.DISABLE_ENV, raising=False)
    return data_home


def _entry(data_home: Path) -> Path:
    return data_home / "applications" / f"{di.APP_ID}.desktop"


def _keys(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


# ---------------------------------------------------------------------------
# The Linux install
# ---------------------------------------------------------------------------


def test_it_installs_the_entry_and_every_icon_rung(linux_session) -> None:
    written = di.ensure()

    assert written == _entry(linux_session)
    assert written is not None and written.is_file()
    for target in di.icon_targets(linux_session / "icons" / "hicolor"):
        assert target.is_file(), f"{target} missing"


def test_the_icon_rungs_hold_the_pixels_their_paths_promise(linux_session) -> None:
    """A 48x48 directory holding a 512 px image is a spec violation a desktop
    silently mis-scales."""
    from PySide6.QtGui import QImage

    di.ensure()

    for size in icons.LAUNCHER_SIZES:
        path = linux_session / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{di.APP_ID}.png"
        image = QImage(str(path))
        assert (image.width(), image.height()) == (size, size)
        assert not image.isNull()


def test_the_scalable_rung_is_the_source_svg(linux_session) -> None:
    di.ensure()

    scalable = linux_session / "icons" / "hicolor" / "scalable" / "apps" / f"{di.APP_ID}.svg"

    assert scalable.read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_the_entry_names_the_launcher_the_icon_and_the_window(linux_session) -> None:
    di.ensure()

    keys = _keys(_entry(linux_session).read_text(encoding="utf-8"))

    assert keys["Type"] == "Application"
    assert keys["Exec"].strip('"').endswith("start.sh")
    # By theme name, never a path: that is what lets each surface pick its rung.
    assert keys["Icon"] == di.APP_ID
    # The key the docks that don't read _GTK_APPLICATION_ID match on.
    assert keys["StartupWMClass"] == di.APP_ID
    assert keys["Terminal"] == "false"


def test_a_second_launch_rewrites_nothing(linux_session) -> None:
    di.ensure()
    entry = _entry(linux_session)
    before = entry.stat().st_mtime_ns
    icon = linux_session / "icons" / "hicolor" / "128x128" / "apps" / f"{di.APP_ID}.png"
    icon_before = icon.stat().st_mtime_ns

    di.ensure()

    assert entry.stat().st_mtime_ns == before
    assert icon.stat().st_mtime_ns == icon_before


def test_a_moved_install_is_rewritten(linux_session, monkeypatch, tmp_path) -> None:
    """The Exec path is the install's, so an app folder that moved must not
    leave a menu entry pointing at where it used to be."""
    di.ensure()
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (moved / "start.sh").write_text("#!/bin/sh\n")
    (moved / "start.sh").chmod(0o755)
    monkeypatch.setattr(di.paths, "app_root", lambda: moved)

    di.ensure()

    # Against the escaped form, not a substring of the raw path: Exec escapes a
    # backslash, so on the Windows leg of the matrix every separator in the path
    # is doubled and the raw string appears nowhere in the value.
    assert _keys(_entry(linux_session).read_text(encoding="utf-8"))["Exec"] == di.exec_value(moved / "start.sh")


def test_a_deleted_icon_is_restored(linux_session) -> None:
    di.ensure()
    icon = linux_session / "icons" / "hicolor" / "48x48" / "apps" / f"{di.APP_ID}.png"
    icon.unlink()

    di.ensure()

    assert icon.is_file()


# ---------------------------------------------------------------------------
# When it must do nothing
# ---------------------------------------------------------------------------


def test_the_opt_out_is_honoured(linux_session, monkeypatch) -> None:
    monkeypatch.setenv(di.DISABLE_ENV, "1")

    assert di.ensure() is None
    assert not _entry(linux_session).exists()


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_a_falsey_opt_out_is_not_an_opt_out(linux_session, monkeypatch, value: str) -> None:
    monkeypatch.setenv(di.DISABLE_ENV, value)

    assert di.ensure() is not None


def test_a_headless_session_gets_no_menu_entry(linux_session, monkeypatch) -> None:
    """Over SSH with no display there is no menu to appear in."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert di.ensure() is None


def test_wayland_alone_is_a_session(linux_session, monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    assert di.ensure() is not None


def test_an_install_with_no_launcher_script_is_skipped(linux_session, monkeypatch, tmp_path) -> None:
    """A wheel install has no start.sh, so there is nothing honest for Exec."""
    monkeypatch.setattr(di.paths, "app_root", lambda: tmp_path / "no-such-tree")

    assert di.ensure() is None


def test_an_unwritable_home_costs_the_entry_and_nothing_else(linux_session, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(di, "_write_icons", explode)

    assert di.ensure() is None


def test_windows_and_macos_never_take_the_linux_path(qapp, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(di.sys, "platform", "win32")

    assert di.ensure() is None
    assert not (tmp_path / "applications").exists()


# ---------------------------------------------------------------------------
# XDG resolution and Exec quoting
# ---------------------------------------------------------------------------


def test_the_default_data_home_is_the_specs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert di.xdg_data_home() == tmp_path / ".local" / "share"


def test_an_absolute_data_home_is_honoured(monkeypatch, tmp_path) -> None:
    """Absolute by *this platform's* rules, not by a leading slash — under the
    Windows leg of the matrix every temp path would read as relative, and the
    whole file would silently exercise the real home instead."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert di.xdg_data_home() == tmp_path


def test_a_relative_data_home_is_invalid_not_resolved(monkeypatch, tmp_path) -> None:
    """The Base Directory spec says to ignore a relative value, not to make one
    absolute against the cwd — which is wherever the launcher happened to be."""
    monkeypatch.setenv("XDG_DATA_HOME", "share")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert di.xdg_data_home() == tmp_path / ".local" / "share"


def test_a_path_with_spaces_survives_exec_quoting(tmp_path) -> None:
    script = tmp_path / "My Apps" / "start.sh"

    value = di.exec_value(script)

    assert value.startswith('"') and value.endswith('"')
    assert "My Apps" in value


@pytest.mark.parametrize("char", ['"', "`", "$", "\\"])
def test_exec_escapes_every_character_the_spec_names(tmp_path, char: str) -> None:
    value = di.exec_value(Path(f"/tmp/od{char}d/start.sh"))

    assert "\\" + char in value


# ---------------------------------------------------------------------------
# Window identity — the half that makes the entry light up
# ---------------------------------------------------------------------------


def test_the_resource_name_is_set_before_the_application_exists(monkeypatch) -> None:
    """Qt reads it once, when the first window's WM_CLASS is built; a default of
    ``__main__`` (argv[0] under ``python -m sorter``) matches nothing."""
    monkeypatch.setattr(di.sys, "platform", "linux")
    monkeypatch.delenv("RESOURCE_NAME", raising=False)

    di.prepare_process()

    assert os.environ["RESOURCE_NAME"] == di.APP_ID


def test_an_explicit_resource_name_wins(monkeypatch) -> None:
    monkeypatch.setattr(di.sys, "platform", "linux")
    monkeypatch.setenv("RESOURCE_NAME", "chosen-by-the-user")

    di.prepare_process()

    assert os.environ["RESOURCE_NAME"] == "chosen-by-the-user"


def test_the_application_carries_the_desktop_file_name(qapp) -> None:
    """GNOME and KDE match on this, not on WM_CLASS."""
    from PySide6.QtGui import QGuiApplication

    try:
        di.apply_identity()

        assert QGuiApplication.desktopFileName() == di.APP_ID
        assert QGuiApplication.applicationName() == di.APP_NAME
    finally:
        QGuiApplication.setDesktopFileName("")


def test_the_id_is_one_value_everywhere_it_appears(linux_session) -> None:
    """Entry basename, icon name, StartupWMClass and RESOURCE_NAME are the same
    string by construction — a mismatch is exactly what leaves a generic icon."""
    di.prepare_process()
    di.ensure()

    keys = _keys(_entry(linux_session).read_text(encoding="utf-8"))

    assert _entry(linux_session).stem == di.APP_ID
    assert keys["Icon"] == keys["StartupWMClass"] == os.environ["RESOURCE_NAME"] == di.APP_ID


# ---------------------------------------------------------------------------
# macOS bundle metadata (the bundle itself can only be written on a Mac)
# ---------------------------------------------------------------------------


def test_the_bundle_plist_names_the_icon_and_the_executable() -> None:
    plist = di.bundle_plist("1.2.3")

    # CFBundleIconFile is the only thing that gives a Dock tile its icon, and
    # it names Resources/AppIcon.icns without the extension.
    assert plist["CFBundleIconFile"] == "AppIcon"
    assert plist["CFBundleExecutable"] == "launch"
    assert plist["CFBundleIdentifier"] == di.APP_ID
    assert plist["CFBundleShortVersionString"] == "1.2.3"


def test_the_icns_fallback_writes_a_real_icns(qapp, tmp_path) -> None:
    """The path taken where ``iconutil`` is absent — which is everywhere that
    isn't a Mac, including ``tools/make_app_icons.py``. Runs on every leg of the
    matrix precisely because it is the half that doesn't need one."""
    Image = pytest.importorskip("PIL.Image")
    target = tmp_path / "AppIcon.icns"

    di.write_icns_with_pillow(target)

    with Image.open(target) as icon:
        assert icon.format == "ICNS"
        # Big enough to be the real artwork rather than an empty container.
        assert max(icon.size) >= 512


# ---------------------------------------------------------------------------
# The macOS bundle, on a Mac
# ---------------------------------------------------------------------------
#
# The matrix has three macOS legs, so the bundle does not have to ship
# unexercised. What they cannot judge is the only thing that matters to a user
# — whether the Dock shows this icon and this name — because a runner has no
# GUI session. Everything up to that point is checked here.

macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="writes a real .app bundle")


@pytest.fixture
def mac_home(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(di.DISABLE_ENV, raising=False)
    return tmp_path


@macos_only
def test_the_bundle_is_laid_out_the_way_launchservices_reads_it(mac_home) -> None:
    import plistlib

    bundle = di._install_macos()

    assert bundle == mac_home / "Applications" / f"{di.APP_NAME}.app"
    assert bundle is not None
    contents = bundle / "Contents"
    plist = plistlib.loads((contents / "Info.plist").read_bytes())
    executable = contents / "MacOS" / plist["CFBundleExecutable"]
    icns = contents / "Resources" / f"{plist['CFBundleIconFile']}.icns"

    # Each of the three is named by the plist and has to actually be there:
    # a bundle missing any one of them launches as an unnamed, iconless tile.
    assert executable.is_file()
    assert icns.is_file() and icns.stat().st_size > 0
    assert plist["CFBundleIdentifier"] == di.APP_ID


@macos_only
def test_the_bundle_stub_is_executable_and_starts_this_checkout(mac_home) -> None:
    from sorter import paths

    bundle = di._install_macos()
    assert bundle is not None
    stub = bundle / "Contents" / "MacOS" / "launch"

    assert os.access(stub, os.X_OK), "LaunchServices runs this directly; without +x the app does nothing"
    body = stub.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh")
    assert str(paths.app_root() / "start.sh") in body


@macos_only
def test_a_second_launch_rewrites_no_bundle(mac_home) -> None:
    bundle = di._install_macos()
    assert bundle is not None
    icns = bundle / "Contents" / "Resources" / "AppIcon.icns"
    before = icns.stat().st_mtime_ns

    di._install_macos()

    assert icns.stat().st_mtime_ns == before


@macos_only
def test_ensure_takes_the_bundle_path_on_a_mac(mac_home) -> None:
    """`ensure` dispatches by platform; on darwin that is the bundle, and the
    Linux entry must not appear."""
    assert di.ensure() == di.bundle_path()
    assert not (mac_home / ".local" / "share" / "applications").exists()
