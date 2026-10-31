"""The release tags the commit that was checked and approved, and no other.

`release.yml` takes a branch name (`ref`). A branch moves, and the release job
sits behind the `stable` approval gate for as long as the reviewer takes, so
resolving the name again after approval could tag a commit pushed in the
meantime — one the summary never showed and the checks never ran on. And the
reusable lint/test workflows used to check out the *dispatch* commit, so
dispatching from `main` with `ref: release/x` tested `main`.

So `prepare` pins one SHA, the checks and the release job check out that SHA,
and the release job fails if the branch has since moved. These assertions pin
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


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow.get(True) or {}


def _checkouts(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps") or [] if str(step.get("uses", "")).startswith(CHECKOUT)]


RELEASE = _load("release.yml")


def test_prepare_pins_the_commit_it_summarises() -> None:
    prepare = RELEASE["jobs"]["prepare"]
    assert prepare["outputs"]["sha"] == "${{ steps.pin.outputs.sha }}"
    pin = next(step for step in prepare["steps"] if step.get("id") == "pin")
    assert "git rev-parse HEAD" in pin["run"]


@pytest.mark.parametrize(("job", "callee"), [("lint", "lint.yml"), ("test", "build.yml")])
def test_the_pre_approval_checks_run_on_the_pinned_commit(job: str, callee: str) -> None:
    caller = RELEASE["jobs"][job]
    assert caller["uses"] == f"./.github/workflows/{callee}"
    assert caller["with"]["ref"] == PINNED

    called = _load(callee)
    assert "ref" in _on(called)["workflow_call"]["inputs"]
    for name, body in called["jobs"].items():
        for step in _checkouts(body):
            assert step["with"].get("ref") == "${{ inputs.ref }}", f"{callee}:{name} checks out something else"


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
