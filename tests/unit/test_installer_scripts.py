"""Guards on the installer scripts, and the runner for the Unix one's tests.

These can't be executed here (no Windows), so the suite enforces the two
properties that silently broke them once already. What the scripts actually
*do* is covered where it can be: installer/tests/Test-ArchiveEntryValidation.ps1
runs the real entry validation under Windows PowerShell in the Verify
Installer workflow.

The expensive one to relearn: Windows PowerShell 5.1 decodes a BOM-less file
using the system ANSI codepage, not UTF-8. A UTF-8 em-dash (``E2 80 94``)
therefore arrives as ``a``, ``EUR``, ``U+201D`` -- and PowerShell accepts
U+201D as a **closing double quote**. Inside a string literal that silently
truncates the string and misparses everything after it, so the reported error
lands on some unrelated line much further down.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
INSTALLER = ROOT / "installer"
# Paths relative to installer/. The two tests/ scripts below are held to the
# same rules: they are executed by the same Windows PowerShell 5.1 that the
# encoding rule below exists for.
SCRIPTS = (
    "install-windows.ps1",
    "install-windows.bat",
    "tests/Test-ArchiveEntryValidation.ps1",
    "tests/Test-DigestVerification.ps1",
)


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_exists(name: str) -> None:
    assert (INSTALLER / name).is_file(), f"installer/{name} is missing"


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_is_pure_ascii(name: str) -> None:
    """Non-ASCII in these files is a parse hazard, not a style preference."""
    raw = (INSTALLER / name).read_bytes()
    offenders = [(n, line) for n, line in enumerate(raw.split(b"\n"), 1) if any(b > 0x7F for b in line)]
    assert not offenders, (
        "Non-ASCII bytes in installer/"
        + name
        + " at line(s) "
        + ", ".join(str(n) for n, _ in offenders)
        + ". Use '-' for dashes and '...' for ellipses -- PowerShell 5.1 reads "
        "this file as ANSI and a UTF-8 em-dash becomes a closing quote."
    )


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_uses_crlf(name: str) -> None:
    """cmd.exe is unforgiving about bare LF in a .bat; keep both CRLF."""
    raw = (INSTALLER / name).read_bytes()
    bare_lf = raw.replace(b"\r\n", b"").count(b"\n")
    assert bare_lf == 0, f"installer/{name} has {bare_lf} bare LF line ending(s)"


def test_powershell_script_has_no_unpaired_quotes_per_line() -> None:
    """Cheap smoke check for the failure mode the em-dash caused.

    Not a parser -- it only flags an odd number of double quotes on a line
    that isn't a continuation or a here-string, which is what a truncated
    string literal looks like.
    """
    text = (INSTALLER / "install-windows.ps1").read_text(encoding="ascii")
    in_block_comment = False
    in_here_string = False
    bad: list[int] = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()

        # Here-strings (@"..."@) legitimately carry a lone quote on the
        # opening and closing lines, and arbitrary text between them.
        if in_here_string:
            if stripped == '"@':
                in_here_string = False
            continue
        if stripped.endswith('@"'):
            in_here_string = True
            continue

        if stripped.startswith("<#"):
            in_block_comment = True
        if in_block_comment:
            if stripped.endswith("#>"):
                in_block_comment = False
            continue
        if stripped.startswith("#") or line.rstrip().endswith("`"):
            continue
        if line.count('"') % 2 != 0:
            bad.append(n)
    assert not bad, f"odd number of double quotes on line(s): {bad}"


# --- install-unix.sh ------------------------------------------------------

UNIX_SCRIPTS = ("install-unix.sh", "tests/test-install-unix.sh")


@pytest.mark.parametrize("name", UNIX_SCRIPTS)
def test_unix_script_is_ascii_lf_posix_sh(name: str) -> None:
    """A CR breaks a shell script outright; non-ASCII has no reason to be there."""
    raw = (INSTALLER / name).read_bytes()
    assert raw.startswith(b"#!/bin/sh\n"), f"installer/{name} must start with #!/bin/sh"
    assert b"\r" not in raw, f"installer/{name} has CR line endings"
    assert all(b <= 0x7F for b in raw), f"installer/{name} has non-ASCII bytes"


# dash is Debian's /bin/sh and bash 3.2 is macOS's; each one present is run.
UNIX_SHELLS = ("sh", "dash", "bash")


@pytest.mark.skipif(sys.platform == "win32", reason="install-unix.sh targets Linux and macOS")
@pytest.mark.parametrize("shell", UNIX_SHELLS)
def test_unix_installer_suite(shell: str) -> None:
    """installer/tests/test-install-unix.sh: digests, entry names, tags, and
    offline installs from a synthetic sdist. No network."""
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    proc = subprocess.run(
        [exe, str(INSTALLER / "tests" / "test-install-unix.sh")],
        env={**os.environ, "TEST_SHELL": exe},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
