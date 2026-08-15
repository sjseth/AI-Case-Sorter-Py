"""Guard on the permissions a caller grants a reusable workflow.

A job that calls another workflow (``uses: ./.github/workflows/x.yml``) caps
that workflow's ``GITHUB_TOKEN`` at what the *calling job* grants. A called job
asking for more is not a runtime failure that shows up in a log -- GitHub
rejects the caller as an **invalid workflow file** before any job runs:

    Error calling workflow '.../docs.yml@<sha>'. The nested job 'deploy' is
    requesting 'pages: write', but is only allowed 'pages: none'.

Three things make that expensive to learn the normal way. The error names the
*caller*, but the mismatch is usually introduced in the *callee*. actionlint
does not check across the call. And release.yml -- the workflow this actually
broke -- only ever runs on a manual dispatch, so the first signal was a release
that refused to start.

So the coupling is pinned here instead: every ``uses:`` call in
``.github/workflows`` is resolved and its grants compared against what the
called workflow's jobs request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# GitHub's own ordering: a grant satisfies a request when it ranks at least as
# high. A scope absent from a permissions block is `none`.
RANK = {"none": 0, "read": 1, "write": 2}

# `permissions: read-all` / `write-all` set every scope at once. Neither is
# used in this tree today, but reading one as "grants nothing" would turn this
# test into a false failure the day someone does.
SHORTHAND = {"read-all": "read", "write-all": "write"}


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _block(node: dict[str, Any], workflow: dict[str, Any]) -> Any:
    """A job's effective permissions block, falling back to its workflow's."""
    return node["permissions"] if "permissions" in node else workflow.get("permissions")


def _level(block: Any, scope: str) -> str:
    """The level `block` sets for `scope`."""
    if block is None:
        return "none"
    if isinstance(block, str):
        assert block in SHORTHAND, f"unrecognized permissions shorthand: {block!r}"
        return SHORTHAND[block]
    return str(block.get(scope, "none"))


def _scopes(block: Any) -> list[str]:
    """The scopes `block` asks for. A shorthand asks for all of them."""
    if isinstance(block, str):
        assert block in SHORTHAND, f"unrecognized permissions shorthand: {block!r}"
        # Every scope GitHub defines; only the ones a caller under-grants are
        # ever reported, so listing them is cheap and keeps a shorthand honest.
        return [
            "actions", "attestations", "checks", "contents", "deployments", "discussions",
            "id-token", "issues", "models", "packages", "pages", "pull-requests",
            "repository-projects", "security-events", "statuses",
        ]  # fmt: skip
    return sorted(block or {})


def _calls() -> list[tuple[Path, str, Path, str]]:
    """Every (caller file, caller job, callee file, callee job) in the tree."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in (_load(path).get("jobs") or {}).items():
            uses = str(job.get("uses", ""))
            if not uses.startswith("./.github/workflows/"):
                continue
            callee_path = ROOT / uses[2:].split("@")[0]
            for callee_job in _load(callee_path).get("jobs") or {}:
                found.append((path, job_name, callee_path, callee_job))
    return found


CALLS = _calls()


def test_there_are_reusable_workflow_calls() -> None:
    """The check below is vacuous if discovery ever stops finding calls."""
    assert CALLS, "no `uses: ./.github/workflows/...` calls found -- has the path convention changed?"


@pytest.mark.parametrize(
    ("caller_path", "caller_job", "callee_path", "callee_job"),
    CALLS,
    ids=[f"{c.name}:{cj}->{e.name}:{ej}" for c, cj, e, ej in CALLS],
)
def test_caller_grants_what_the_called_job_requests(
    caller_path: Path, caller_job: str, callee_path: Path, callee_job: str
) -> None:
    caller, callee = _load(caller_path), _load(callee_path)
    granted = _block((caller.get("jobs") or {})[caller_job], caller)
    requested = _block((callee.get("jobs") or {})[callee_job], callee)

    short = [
        f"{scope}: {_level(requested, scope)} (granted {_level(granted, scope)})"
        for scope in _scopes(requested)
        if RANK[_level(requested, scope)] > RANK[_level(granted, scope)]
    ]
    assert not short, (
        f"{callee_path.name}'s '{callee_job}' job requests more than "
        f"{caller_path.name}'s '{caller_job}' job grants it: " + "; ".join(short) + ". "
        f"GitHub rejects {caller_path.name} as an invalid workflow file for this, before any job runs. "
        f"Add the missing scope(s) to the `permissions:` block of '{caller_job}' in {caller_path.name}."
    )
