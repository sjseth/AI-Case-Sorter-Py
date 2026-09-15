"""The app as the desktop knows it: menu entry, launcher icon, window identity.

Three separate mechanisms wearing one name, because each platform answers
"which application is this window?" its own way and none of them is
``setWindowIcon``:

* **Linux** — a freedesktop **desktop entry** in ``$XDG_DATA_HOME/applications``
  plus PNGs on the ``hicolor`` icon-theme rungs. Written by :func:`ensure` on
  launch, because there is no Linux installer to write them: ``start.sh`` is
  the whole distribution story, and an app the user has to find in a terminal
  every time isn't installed in any sense they recognise.
* **macOS** — a minimal ``.app`` **bundle** in ``~/Applications`` whose
  executable is a stub that ``exec``s ``start.sh``. The Dock reads the icon
  from ``CFBundleIconFile``; that is a bundle property, unreachable from a
  running process, so nothing but a bundle can fix it.
* **Windows** — nothing here writes anything. ``install-windows.ps1`` already
  creates the Start Menu shortcut and points it at ``installer/casesorter.ico``
  (built by ``tools/make_app_icons.py`` from the same artwork). What this module
  contributes is :func:`prepare_process`'s AppUserModelID, without which the
  taskbar button belongs to ``python.exe``.

**Everything here is best-effort and silent on failure.** A read-only home
directory, an exotic session, a desktop that does none of this — none of it
may cost the user a launch, so :func:`ensure` swallows what it catches into a
debug log and returns None. ``CASESORTER_NO_DESKTOP_ENTRY=1`` opts out.

Portability across distributions is not a matter of detecting them: GNOME, KDE
Plasma, Xfce, Cinnamon, MATE, LXQt and Budgie all read the *same* two
freedesktop specifications, so the only distro-specific code here is the
best-effort ``update-desktop-database`` nudge — and that is a cache refresh,
not a requirement. ``tests/integration/test_desktop_entry.py`` runs the real
``desktop-file-validate`` over what this writes.

**Window identity is the subtle half.** A menu entry only lights up as "this
running window" if the two can be matched, and the matching key differs by
desktop: GNOME and KDE read ``_GTK_APPLICATION_ID`` / ``_KDE_NET_WM_DESKTOP_FILE``,
which Qt sets from ``QGuiApplication::desktopFileName()``; the Xfce/MATE/LXQt
docks match ``StartupWMClass`` against ``WM_CLASS``, whose *instance* half Qt
takes from the ``RESOURCE_NAME`` environment variable and otherwise from
``argv[0]``'s basename — which for ``python -m sorter`` is the word
``__main__``. So :func:`prepare_process` sets ``RESOURCE_NAME`` before the
``QApplication`` exists (Qt reads it once, at window creation) and
:func:`apply_identity` sets the desktop file name after. Both keys are
:data:`APP_ID`, and so is ``StartupWMClass``.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtGui import QGuiApplication

from .. import paths
from . import icons

log = logging.getLogger(__name__)

# The reverse-DNS application ID: the desktop entry's basename, the icon
# filename, the Wayland app_id, the WM_CLASS instance and the macOS bundle
# identifier, all at once. Derived from the canonical upstream repository
# (github.com/sjseth/AI-Case-Sorter-Py) rather than from reloadingrecipes.com,
# which names the community backend service and not this app. Changing it
# orphans every entry already installed, so it changes only deliberately.
APP_ID = "io.github.sjseth.AICaseSorter"

APP_NAME = "AI Case Sorter"
GENERIC_NAME = "Cartridge case sorter"
COMMENT = "Sort spent brass cartridge cases by headstamp"
KEYWORDS = ("brass", "casing", "headstamp", "reloading", "sorter")

DISABLE_ENV = "CASESORTER_NO_DESKTOP_ENTRY"

# Qt reads this once, when it builds WM_CLASS for the first window.
_RESOURCE_NAME_ENV = "RESOURCE_NAME"


# ---------------------------------------------------------------------------
# Process and application identity
# ---------------------------------------------------------------------------


def prepare_process() -> None:
    """Identity that has to be set **before** the ``QApplication`` exists."""
    if sys.platform.startswith("linux"):
        # setdefault: a launcher or a user who set this deliberately outranks us.
        os.environ.setdefault(_RESOURCE_NAME_ENV, APP_ID)
    elif sys.platform == "win32":  # pragma: no cover - Windows only
        _set_windows_app_id()


def apply_identity() -> None:
    """Identity the application object carries; call it once one exists.

    Static setters throughout, deliberately: ``QApplication.instance()`` is
    typed as the ``QCoreApplication`` base, so taking the instance as an
    argument would buy a cast and nothing else.
    """
    QGuiApplication.setApplicationName(APP_NAME)
    QGuiApplication.setApplicationDisplayName(APP_NAME)
    # X11: _GTK_APPLICATION_ID and _KDE_NET_WM_DESKTOP_FILE. Wayland: app_id.
    QGuiApplication.setDesktopFileName(APP_ID)


def _set_windows_app_id() -> None:  # pragma: no cover - Windows only
    """Give the taskbar button an identity of its own.

    Without this the button belongs to ``python.exe``, so it carries Python's
    icon and groups with any other Python window. It still does not *unify*
    with the Start Menu shortcut — that needs the same ID written into the
    ``.lnk``'s property store, which ``WScript.Shell`` cannot do — but the
    running window at least becomes this app rather than the interpreter.
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # ty: ignore[unresolved-attribute]
    except Exception:
        log.debug("could not set the Windows AppUserModelID", exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ensure() -> Path | None:
    """Install or refresh this install's launcher entry; return what it wrote.

    Needs a live ``QGuiApplication`` (it rasterises icons through Qt). Returns
    None whenever there is nothing to do — opted out, no graphical session, no
    ``start.sh`` to point at, an unsupported platform, or any failure at all.
    """
    if _opted_out():
        return None
    try:
        if sys.platform.startswith("linux"):
            return _install_linux()
        if sys.platform == "darwin":
            return _install_macos()
    except Exception:
        # Never fatal: an unwritable home costs the menu entry, not the launch.
        log.debug("desktop integration skipped", exc_info=True)
    return None


def _opted_out() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def launcher_script() -> Path | None:
    """``start.sh``, if this install has one to point a launcher at.

    A wheel install has no launcher script and no app folder worth naming, so
    there is nothing honest to put in ``Exec=`` — better no menu entry than one
    that fails to start.
    """
    script = paths.app_root() / "start.sh"
    if not script.is_file():
        return None
    if not os.access(script, os.X_OK):
        try:
            script.chmod(script.stat().st_mode | 0o111)
        except OSError:
            return None
    return script


# ---------------------------------------------------------------------------
# Linux: desktop entry + hicolor icons
# ---------------------------------------------------------------------------


def xdg_data_home() -> Path:
    """``$XDG_DATA_HOME``, or the spec's default.

    A relative value is *invalid* per the Base Directory spec, not merely
    unusual — it is ignored rather than resolved against the cwd.

    ``is_absolute()`` rather than a leading-slash test, which is the same
    question on the only platform this runs on and a different one under the
    Windows leg of the test matrix — where every temp path the suite hands it
    would be "relative", and the tests would quietly write to the real home.
    """
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / ".local" / "share"


def _has_graphical_session() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def exec_value(script: Path) -> str:
    """``script`` as a desktop entry ``Exec=`` value.

    Always quoted, which is legal for any value and correct for the one that
    actually happens: an app folder with a space in its path. Inside the
    quotes the spec escapes exactly four characters.
    """
    escaped = str(script)
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return f'"{escaped}"'


def desktop_entry(script: Path, workdir: Path) -> str:
    """The desktop entry text for a launcher at ``script``."""
    lines = [
        "[Desktop Entry]",
        # The spec version this entry conforms to -- not the app's version.
        "Version=1.0",
        "Type=Application",
        f"Name={APP_NAME}",
        f"GenericName={GENERIC_NAME}",
        f"Comment={COMMENT}",
        f"Exec={exec_value(script)}",
        f"Path={workdir}",
        # By name, not by path: the icon theme resolves it to whichever rung
        # fits the surface asking, which is the whole point of installing a set.
        f"Icon={APP_ID}",
        "Terminal=false",
        # One main category, deliberately: two puts the app in two menus on the
        # desktops that build theirs from this, which desktop-file-validate
        # warns about by name.
        "Categories=Utility;",
        f"Keywords={';'.join(KEYWORDS)};",
        "StartupNotify=true",
        # See the module docstring: the docks that don't read the GTK/KDE
        # window properties match this against WM_CLASS instead.
        f"StartupWMClass={APP_ID}",
    ]
    return "\n".join(lines) + "\n"


def icon_targets(icon_root: Path) -> list[Path]:
    """Every icon file the Linux install owns, PNG rungs then the scalable SVG."""
    targets = [icon_root / f"{size}x{size}" / "apps" / f"{APP_ID}.png" for size in icons.LAUNCHER_SIZES]
    targets.append(icon_root / "scalable" / "apps" / f"{APP_ID}.svg")
    return targets


def _write_icons(icon_root: Path) -> None:
    for size in icons.LAUNCHER_SIZES:
        target = icon_root / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not icons.launcher_pixmap(size).save(str(target), "PNG"):
            raise OSError(f"could not write {target}")
    scalable = icon_root / "scalable" / "apps" / f"{APP_ID}.svg"
    scalable.parent.mkdir(parents=True, exist_ok=True)
    _write_text(scalable, icons.launcher_svg(icons.LAUNCHER_DETAIL_MIN))


def _install_linux() -> Path | None:
    if not _has_graphical_session():
        return None
    script = launcher_script()
    if script is None:
        return None

    data_home = xdg_data_home()
    entry = data_home / "applications" / f"{APP_ID}.desktop"
    icon_root = data_home / "icons" / "hicolor"
    content = desktop_entry(script, paths.app_root())

    if _is_current(entry, content) and all(target.is_file() for target in icon_targets(icon_root)):
        return entry

    _write_icons(icon_root)
    entry.parent.mkdir(parents=True, exist_ok=True)
    _write_text(entry, content)
    _refresh_desktop_database(data_home / "applications")
    log.info("desktop entry installed: %s", entry)
    return entry


def _refresh_desktop_database(applications: Path) -> None:
    """Nudge the desktop's own cache; every desktop also notices on its own.

    Deliberately *not* ``gtk-update-icon-cache``: a per-user ``hicolor`` tree
    has no ``index.theme``, and GTK reads the directory directly when there is
    no cache — creating one only introduces something that can go stale.
    """
    binary = shutil.which("update-desktop-database")
    if binary is None:
        return
    try:
        subprocess.run([binary, str(applications)], check=False, capture_output=True, timeout=30)
    except Exception:
        log.debug("update-desktop-database failed", exc_info=True)


# ---------------------------------------------------------------------------
# macOS: a stub .app bundle
# ---------------------------------------------------------------------------

# What `iconutil` expects in an .iconset: (points, scale).
_ICNS_RUNGS = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))

_BUNDLE_STUB = """\
#!/bin/sh
# Generated by sorter.ui.desktop_integration -- rewritten on launch, so edits
# here are lost. The bundle exists to give the Dock an icon and a name; the app
# itself lives at the path below.
exec {launcher}
"""


def bundle_path() -> Path:
    """Where the generated ``.app`` lives — per user, never ``/Applications``."""
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def bundle_plist(version: str) -> dict[str, object]:
    """``Info.plist`` contents for the stub bundle."""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_ID,
        "CFBundleExecutable": "launch",
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "NSHighResolutionCapable": True,
    }


def _install_macos() -> Path | None:  # pragma: no cover - macOS only
    script = launcher_script()
    if script is None:
        return None

    from .. import __version__

    bundle = bundle_path()
    contents = bundle / "Contents"
    executable = contents / "MacOS" / "launch"
    icns = contents / "Resources" / "AppIcon.icns"
    plist = plistlib.dumps(bundle_plist(__version__))
    stub = _BUNDLE_STUB.format(launcher=_shell_quote(str(script)))

    if _is_current(contents / "Info.plist", plist) and _is_current(executable, stub) and icns.is_file():
        return bundle

    executable.parent.mkdir(parents=True, exist_ok=True)
    icns.parent.mkdir(parents=True, exist_ok=True)
    _write_icns(icns)
    _write_bytes(contents / "Info.plist", plist)
    _write_text(executable, stub)
    executable.chmod(0o755)
    # LaunchServices reads a bundle when its directory mtime changes; without
    # this a rewritten icon can keep showing the previous one for hours.
    os.utime(bundle, None)
    log.info("application bundle installed: %s", bundle)
    return bundle


def _write_icns(target: Path) -> None:  # pragma: no cover - macOS only
    """Build the ``.icns``, preferring the tool every Mac already has."""
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for points, scale in _ICNS_RUNGS:
            suffix = "@2x" if scale == 2 else ""
            name = f"icon_{points}x{points}{suffix}.png"
            if not icons.launcher_pixmap(points * scale).save(str(iconset / name), "PNG"):
                raise OSError(f"could not write {name}")
        iconutil = shutil.which("iconutil")
        if iconutil is not None:
            subprocess.run(
                [iconutil, "-c", "icns", str(iconset), "-o", str(target)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            return
        write_icns_with_pillow(target)


def write_icns_with_pillow(target: Path) -> None:
    """``.icns`` without ``iconutil``.

    The fallback path on a Mac missing the Xcode tools, and the only path for
    ``tools/make_app_icons.py`` run anywhere else. Pillow derives the smaller
    members itself, so it gets one large master rather than an iconset.
    """
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        master = Path(tmp) / "master.png"
        if not icons.launcher_pixmap(1024).save(str(master), "PNG"):
            raise OSError("could not rasterise the launcher mark")
        with Image.open(master) as image:
            image.save(target, format="ICNS")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _is_current(path: Path, content: str | bytes) -> bool:
    """Whether ``path`` already holds exactly ``content``."""
    try:
        existing: str | bytes = path.read_bytes() if isinstance(content, bytes) else path.read_text(encoding="utf-8")
    except OSError:
        return False
    return existing == content


def _write_text(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))


def _write_bytes(path: Path, content: bytes) -> None:
    """Write via a sibling temp file, so a half-written entry never exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
