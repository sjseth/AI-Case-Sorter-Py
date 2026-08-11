"""Guards on cliff.toml -- the file that decides the released version number.

Nothing else in the repo encodes a version, so a wrong answer here ships a
wrong release (CLAUDE.md §8). These tests run the *real* git-cliff binary
against throwaway repos, because the only thing worth asserting is what the
tool actually does -- the failure mode being guarded against is documentation
that describes git-cliff's behaviour correctly-in-theory and wrongly in fact.

Skipped when git-cliff isn't installed; CI's release workflows install it, and
a contributor without it still gets the rest of the suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
CLIFF_CONFIG = ROOT / "cliff.toml"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("git-cliff") is None, reason="git-cliff not installed"),
]


def _repo(tmp_path: Path, base_tag: str, commits: list[str]) -> Path:
    """A throwaway git repo tagged ``base_tag`` with ``commits`` on top."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main", ".")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    # Never inherit the developer's signing config -- it makes commits fail here.
    run("git", "config", "commit.gpgsign", "false")
    run("git", "config", "tag.gpgsign", "false")
    shutil.copy(CLIFF_CONFIG, repo / "cliff.toml")

    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "chore: base")
    run("git", "tag", base_tag)

    for i, subject in enumerate(commits):
        (repo / f"f{i}.txt").write_text("x\n", encoding="utf-8")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", subject)
    return repo


def _bumped_version(repo: Path) -> str:
    result = subprocess.run(
        ["git-cliff", "--config", "cliff.toml", "--bumped-version"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _changelog(repo: Path) -> str:
    result = subprocess.run(
        ["git-cliff", "--config", "cliff.toml", "--unreleased", "--offline"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize(
    ("base", "subject", "expected"),
    [
        # --- pre-1.0: a breaking change must NOT leave 0.x ---
        # cliff.toml sets breaking_always_bump_major = false for this. Without
        # it git-cliff's default sends 0.1.0 + `feat!:` straight to 1.0.0,
        # i.e. one commit message silently declares the API stable.
        ("0.0.1", "fix: a bug", "0.0.2"),
        ("0.0.1", "feat: a feature", "0.1.0"),
        ("0.0.1", "feat!: a breaking feature", "0.1.0"),
        ("0.1.0", "fix: a bug", "0.1.1"),
        ("0.1.0", "feat: a feature", "0.2.0"),
        ("0.1.0", "feat!: a breaking feature", "0.2.0"),
        ("0.9.3", "feat: a feature", "0.10.0"),
        ("0.9.3", "feat!: a breaking feature", "0.10.0"),
        # --- 1.0.0 and above: ordinary semver, unaffected by that setting ---
        ("1.2.3", "fix: a bug", "1.2.4"),
        ("1.2.3", "feat: a feature", "1.3.0"),
        ("1.2.3", "feat!: a breaking feature", "2.0.0"),
        ("2.0.0", "feat!: a breaking feature", "3.0.0"),
    ],
)
def test_bump_mapping_is_what_the_docs_claim(tmp_path: Path, base: str, subject: str, expected: str) -> None:
    """CONTRIBUTING.md publishes this table and RELEASING.md repeats it; a
    contributor picks their commit type from it.

    The rows that matter most are the pre-1.0 `feat!:` ones: they assert that
    no commit message can promote the project to 1.0.0 on its own. Getting to
    1.0.0 requires passing `version` explicitly to the Release workflow.
    """
    assert _bumped_version(_repo(tmp_path, base, [subject])) == expected


def test_no_commit_can_reach_1_0_0_on_its_own(tmp_path: Path) -> None:
    """The invariant behind the table above, stated directly: from any 0.x
    tag, no combination of conventional commits auto-detects as 1.0.0.
    """
    repo = _repo(
        tmp_path,
        "0.9.9",
        ["feat!: breaking one", "feat: a feature", "fix: a bug", "feat!: breaking two"],
    )
    version = _bumped_version(repo)
    assert version.startswith("0."), f"a breaking change escaped 0.x: {version}"


def test_tag_pattern_ignores_a_v_prefixed_tag(tmp_path: Path) -> None:
    """tag_pattern is an unanchored regex in git-cliff, so the obvious
    "[0-9].*" also matches any tag merely *containing* a digit -- including
    the `v1.0.0` form check-release.yml rejects. With that pattern a stray
    v-tag becomes the latest release and the next version is computed from
    it: `0.1.0` + a feat would resolve to `v1.0.1` rather than `0.2.0` -- both
    the wrong number and a `v` prefix the release workflow would then reject.
    """
    repo = _repo(tmp_path, "0.1.0", ["feat: a feature"])
    subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, check=True, capture_output=True)

    assert _bumped_version(repo) == "0.2.0"


def test_a_release_candidate_is_not_a_release_boundary(tmp_path: Path) -> None:
    """tag_pattern is anchored at *both* ends so `0.5.0rc1` is invisible to
    git-cliff. Two things follow, and both are the reason the `$` is there.

    The bump is computed from the last stable tag, so cutting rc1, rc2 and
    then the release all resolve the same 0.5.0. Without the anchor git-cliff
    2.13.1 hands back `0.5.0rc1` verbatim -- not the rc's successor, the rc
    itself -- so the release would try to tag a name that already exists, and
    every further candidate would too.

    And the changelog window spans back past the rc, so the release ships
    notes covering everything the candidates were cut to test instead of only
    what changed after the last one.
    """
    repo = _repo(tmp_path, "0.4.0", ["feat: a feature"])
    subprocess.run(["git", "tag", "0.5.0rc1"], cwd=repo, check=True, capture_output=True)

    assert _bumped_version(repo) == "0.5.0"
    assert "A feature" in _changelog(repo)


def test_changelog_line_degrades_gracefully_without_a_github_remote(tmp_path: Path) -> None:
    """The `by @user in #N` attribution suffix reads commit.remote.username /
    commit.remote.pr_number, both populated only by git-cliff's GitHub
    integration (a real token, network access, and -- for pr_number
    specifically -- a squash merge; see the comment above `body` in
    cliff.toml). None of that is available to these throwaway repos, which
    have no GitHub remote at all. The line must still render cleanly with
    neither piece of attribution, not error out or leave a dangling "by @"
    or "in #" with nothing after it -- that's what the {% if %} guards around
    each field are for.
    """
    changelog = _changelog(_repo(tmp_path, "0.1.0", ["fix: a bug fix"]))
    assert "- A bug fix" in changelog
    assert "by @" not in changelog
    assert " in #" not in changelog


def test_an_unrelated_parenthesized_number_in_the_message_is_left_alone(tmp_path: Path) -> None:
    """The "(#N)" strip is guarded behind commit.remote.pr_number so it can't
    fire on a coincidence -- a subject that legitimately ends in "(#N)" for
    an unrelated reason must render untouched without a real PR association.
    """
    changelog = _changelog(_repo(tmp_path, "0.1.0", ["fix: close issue (#5) properly"]))
    assert "Close issue (#5) properly" in changelog
