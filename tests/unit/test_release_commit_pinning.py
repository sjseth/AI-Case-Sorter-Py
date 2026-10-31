"""The release tags the commit that was checked and approved, and no other.

`release.yml` takes a branch name (`ref`). A branch moves, and the release job
sits behind the `stable` approval gate for as long as the reviewer takes, so
resolving the name again after approval could tag a commit pushed in the
meantime — one the summary never showed and the checks never ran on. And the
reusable lint/test workflows check out the *dispatch* commit (`github.sha`), so
dispatching from `main` with `ref: release/x` tested `main`.

So `prepare` pins one SHA and refuses one that isn't the dispatch commit, the
release job checks out that SHA, and fails if the branch has since moved. The
reusable workflows deliberately take no `ref` input: CodeQL rates a
`workflow_dispatch`-able workflow that checks out a caller-chosen ref and then
runs its local actions as high-severity cache poisoning. These assertions pin
the plumbing; the behaviour itself only runs on a real dispatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CHECKOUT = "actions/checkout@"
PINNED = "${{ needs.prepare.outputs.sha }}"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8")) or {}


def _checkouts(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps") or [] if str(step.get("uses", "")).startswith(CHECKOUT)]


RELEASE = _load("release.yml")


def test_prepare_pins_the_dispatch_commit() -> None:
    prepare = RELEASE["jobs"]["prepare"]
    assert prepare["outputs"]["sha"] == "${{ steps.pin.outputs.sha }}"
    pin = next(step for step in prepare["steps"] if step.get("id") == "pin")
    assert "git rev-parse HEAD" in pin["run"]
    # The commit the reusable checks will run on; anything else is refused.
    assert '"$GITHUB_SHA"' in pin["run"]
    assert "exit 1" in pin["run"]


@pytest.mark.parametrize(("job", "callee"), [("lint", "lint.yml"), ("test", "build.yml")])
def test_the_pre_approval_checks_run_on_the_dispatch_commit(job: str, callee: str) -> None:
    caller = RELEASE["jobs"][job]
    assert caller["uses"] == f"./.github/workflows/{callee}"
    assert "with" not in caller, "the checks must not be pointed at a caller-chosen ref"

    for name, body in _load(callee)["jobs"].items():
        for step in _checkouts(body):
            assert "ref" not in (step.get("with") or {}), f"{callee}:{name} checks out something other than github.sha"


def test_the_release_job_tags_the_pinned_commit_and_refuses_a_moved_branch() -> None:
    release = RELEASE["jobs"]["release"]
    (checkout,) = _checkouts(release)
    assert checkout["with"]["ref"] == PINNED

    steps = release["steps"]
    guard_at = next(
        i for i, step in enumerate(steps) if step.get("name") == "Refuse a branch that moved since it was checked"
    )
    tag_at = next(i for i, step in enumerate(steps) if step.get("name") == "Create the tag locally")
    assert guard_at < tag_at, "the moved-branch check must run before anything is tagged"
    guard = steps[guard_at]
    assert guard["env"]["SHA"] == PINNED
    assert "git ls-remote origin" in guard["run"]
