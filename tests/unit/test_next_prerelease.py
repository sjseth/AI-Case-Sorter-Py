"""Guards on .github/scripts/next-prerelease.sh -- the script that decides a
release candidate's tag.

Same reasoning as tests/integration/test_cliff_config.py: nothing else in the
repo encodes a version, so a wrong answer here ships a wrong release
(CLAUDE.md §8). The number it picks is also load-bearing in a way that fails
quietly -- a repeat of an existing rc number is caught by the workflow's "tag
already exists" check, but *skipping* one is not caught by anything.

The script itself only ever runs on ubuntu-latest, but these run wherever a
real bash can be found -- Git Bash included, which is what makes them mean
something on the Windows leg of the matrix.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "next-prerelease.sh"


def _find_bash() -> str | None:
    """The bash to run, resolved to a full path, or None if there isn't one.

    Resolving matters on Windows, where two different binaries answer to the
    name. `shutil.which` searches PATH and finds Git Bash; `subprocess.run(["bash",
    ...])` goes through CreateProcess, which looks in System32 *before* PATH and
    finds `bash.exe` -- the WSL launcher. On a GitHub runner that launcher has no
    distribution installed, so it ignores the script, prints "Use `wsl.exe --install
    <Distro>` to install." to **stdout in UTF-16**, and exits 1. Passing the
    resolved path is what keeps the thing probed and the thing run the same binary.

    The probe demands output rather than a zero exit, because the launcher
    exits 0 for `bash -c 'exit 0'` -- a returncode check calls it a working shell.
    """
    exe = shutil.which("bash")
    if exe is None:
        return None
    try:
        probe = subprocess.run([exe, "-c", "printf ok"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if probe.returncode == 0 and probe.stdout.strip() == b"ok" else None


BASH = _find_bash()

pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None,
    reason="needs a working bash and git",
)


def _repo(tmp_path: Path, tags: list[str]) -> Path:
    """A throwaway git repo carrying ``tags`` on a single commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main", ".")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    # Never inherit the developer's signing config -- it makes commits fail here.
    run("git", "config", "commit.gpgsign", "false")
    run("git", "config", "tag.gpgsign", "false")

    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "chore: base")
    for tag in tags:
        run("git", "tag", tag)
    return repo


def _next(repo: Path, base: str, kind: str) -> str:
    result = subprocess.run(
        [str(BASH), str(SCRIPT), base, kind],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    # check=True would report only the exit status. Both streams, because the
    # one failure this has actually had wrote its explanation to *stdout*.
    assert result.returncode == 0, (
        f"exit {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout.strip()


def test_first_candidate_is_rc1(tmp_path: Path) -> None:
    assert _next(_repo(tmp_path, []), "0.5.0", "rc") == "0.5.0rc1"


def test_it_continues_an_existing_series(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["0.4.0", "0.5.0rc1"])
    assert _next(repo, "0.5.0", "rc") == "0.5.0rc2"


def test_the_number_is_compared_numerically(tmp_path: Path) -> None:
    """The bug this exists to catch: a lexicographic max makes rc9 the highest
    of nine candidates, so rc10 would be issued as rc2 -- a tag that already
    exists, failing the release, or worse a gap in the series.
    """
    repo = _repo(tmp_path, [f"0.5.0rc{n}" for n in range(1, 10)])
    assert _next(repo, "0.5.0", "rc") == "0.5.0rc10"


@pytest.mark.parametrize(("kind", "expected"), [("rc", "0.5.0rc1"), ("b", "0.5.0b4"), ("a", "0.5.0a1")])
def test_each_kind_counts_its_own_series(tmp_path: Path, kind: str, expected: str) -> None:
    """Counted per kind: with 0.5.0b3 out there, another beta is b4, but
    promoting to a release candidate starts at rc1 rather than inheriting the
    beta's number.
    """
    assert _next(_repo(tmp_path, ["0.5.0b3"]), "0.5.0", kind) == expected


def test_a_longer_base_version_does_not_leak_in(tmp_path: Path) -> None:
    """`git tag --list "0.5.1rc*"` must not see 0.5.10rc3. It doesn't -- the
    glob requires a literal "rc" straight after the base -- but the failure
    would be a silently wrong version number, so pin it.
    """
    repo = _repo(tmp_path, ["0.5.10rc3"])
    assert _next(repo, "0.5.1", "rc") == "0.5.1rc1"


def test_a_non_numeric_suffix_is_ignored(tmp_path: Path) -> None:
    """A hand-made tag in the same namespace must not derail the count."""
    repo = _repo(tmp_path, ["0.5.0rc1", "0.5.0rc-broken"])
    assert _next(repo, "0.5.0", "rc") == "0.5.0rc2"


def test_it_refuses_to_guess_without_arguments(tmp_path: Path) -> None:
    """Both arguments are required. An empty base would print a bare "rc1",
    which check-version would then reject -- but failing here names the
    actual problem.
    """
    result = subprocess.run(
        [str(BASH), str(SCRIPT)],
        cwd=_repo(tmp_path, []),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()
