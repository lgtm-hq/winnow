"""Report failed main and scheduled runs as durable, deduplicated issues.

Each workflow that runs on a push to main or on a schedule ends with a
``notify-failure`` job calling lgtm-ci's ``reusable-main-failure-notifier``.
The reusable opens one issue per workflow key and comments on it for every
later failure, so the key must be stable and unique, and the job must wait for
every other job so a failure anywhere in the run is reported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_GATE = _WORKFLOWS / "all-checks-gate.yml"
_NOTIFIER = "lgtm-hq/lgtm-ci/.github/workflows/reusable-main-failure-notifier.yml"
_JOB = "notify-failure"
# Workflows with main-push or scheduled runs that must report failures, with
# the issue key each uses. The key is the file stem: stable across renames of
# the display name and unique per workflow. Tag publishing reports through
# its own release notifier, the release reusables report their own failures,
# and the aggregate gate is a summary of the producers listed here.
_COVERED = {
    "codeql.yml": "codeql",
    "docker-build-publish.yml": "docker-build-publish",
    "ghcr-cleanup.yml": "ghcr-cleanup",
    "quality-ci.yml": "quality-ci",
    "sbom-on-main.yml": "sbom-on-main",
    "scorecards.yml": "scorecards",
    "security-audit.yml": "security-audit",
    "test-ci.yml": "test-ci",
}
# Fire on failure or cancellation, only for main pushes and schedules: never
# for a pull request, merge queue, tag or manual dispatch, and never when
# every job succeeded or was skipped on purpose.
_CONDITION = (
    "(failure() || cancelled())"
    " && github.ref == 'refs/heads/main'"
    " && (github.event_name == 'push' || github.event_name == 'schedule')"
)
_PERMISSIONS = {"actions": "read", "contents": "read", "issues": "write"}
# Inputs the pinned reusable declares that winnow sets. tooling-ref is left
# unset on purpose: the reusable tracks its own pin and warns on an override.
_INPUTS = {"workflow-key", "egress-policy"}


def _workflow(path: Path) -> dict[str, Any]:
    """Load GitHub's event key, including YAML 1.1's unquoted on spelling."""
    data = yaml.safe_load(path.read_text())
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def _workflows() -> dict[str, dict[str, Any]]:
    """Parse every workflow file, keyed by file name."""
    paths = sorted(
        path for pattern in ("*.yml", "*.yaml") for path in _WORKFLOWS.glob(pattern)
    )
    return {path.name: _workflow(path) for path in paths}


def _pin() -> str:
    """Return the lgtm-ci commit the aggregate gate is pinned to."""
    uses = _workflow(_GATE)["jobs"]["gate"]["uses"]
    return cast(str, uses).strip().rsplit("@", 1)[1]


def _is_notifier(job: dict[str, Any]) -> bool:
    """Tell whether a job calls the main-failure notifier, at any pin."""
    uses = job.get("uses")
    if not isinstance(uses, str):
        return False
    return uses.strip().lower().startswith(_NOTIFIER.lower() + "@")


def _notifiers(workflows: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Return the notifier job ids each workflow defines."""
    found: dict[str, list[str]] = {}
    for name, workflow in workflows.items():
        jobs = [
            job_id
            for job_id, job in (workflow.get("jobs") or {}).items()
            if _is_notifier(job)
        ]
        if jobs:
            found[name] = jobs
    return found


def _assert_notifier(name: str, workflow: dict[str, Any], key: str, pin: str) -> None:
    """Require one complete, correctly gated notifier job in a workflow."""
    jobs = cast(dict[str, Any], workflow["jobs"])
    assert_that(jobs).described_as(name).contains_key(_JOB)
    job = cast(dict[str, Any], jobs[_JOB])
    assert_that(job["uses"].strip()).described_as(name).is_equal_to(
        f"{_NOTIFIER}@{pin}"
    )
    needs = job.get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    assert_that(sorted(needs or [])).described_as(f"{name} needs").is_equal_to(
        sorted(job_id for job_id in jobs if job_id != _JOB)
    )
    condition = " ".join(str(job.get("if", "")).split())
    assert_that(condition).described_as(f"{name} if").is_equal_to(_CONDITION)
    assert_that(job.get("permissions")).described_as(name).is_equal_to(_PERMISSIONS)
    inputs = cast(dict[str, Any], job.get("with") or {})
    assert_that(set(inputs)).described_as(f"{name} with").is_equal_to(_INPUTS)
    assert_that(inputs["workflow-key"]).described_as(name).is_equal_to(key)
    assert_that(inputs["egress-policy"]).described_as(name).is_equal_to("block")
    assert_that(job).described_as(name).does_not_contain_key("secrets")


def _runs_on_main_or_schedule(workflow: dict[str, Any]) -> bool:
    """Tell whether a workflow runs for main pushes or on a schedule."""
    events = workflow["on"]
    if "schedule" in events:
        return True
    push = events.get("push")
    return isinstance(push, dict) and "main" in push.get("branches", [])


def test_covered_workflows_each_report_failures() -> None:
    """Give every listed main or scheduled workflow one complete notifier."""
    workflows = _workflows()
    pin = _pin()
    for name, key in _COVERED.items():
        _assert_notifier(name=name, workflow=workflows[name], key=key, pin=pin)


def test_only_covered_workflows_call_the_notifier_once() -> None:
    """Keep the notifier out of PR-only, tag and release workflows."""
    notifiers = _notifiers(_workflows())
    assert_that(notifiers).is_equal_to({name: [_JOB] for name in _COVERED})


def test_covered_workflows_run_on_main_or_a_schedule() -> None:
    """A notifier in a workflow that never runs on main would never fire."""
    workflows = _workflows()
    for name in _COVERED:
        assert_that(_runs_on_main_or_schedule(workflows[name])).described_as(
            name
        ).is_true()


def test_workflow_keys_are_unique_and_match_the_file() -> None:
    """One issue per workflow: keys must not collide or drift from the file."""
    keys = list(_COVERED.values())
    assert_that(keys).does_not_contain_duplicates()
    for name, key in _COVERED.items():
        assert_that(Path(name).stem).is_equal_to(key)


def _good() -> dict[str, Any]:
    """Build a minimal workflow whose notifier passes every check."""
    return {
        "on": {"push": {"branches": ["main"]}},
        "permissions": {},
        "jobs": {
            "build": {"runs-on": "ubuntu-latest", "steps": []},
            "publish": {"needs": "build", "runs-on": "ubuntu-latest", "steps": []},
            _JOB: {
                "needs": ["build", "publish"],
                "if": _CONDITION,
                "uses": f"{_NOTIFIER}@{'a' * 40}",
                "permissions": dict(_PERMISSIONS),
                "with": {"workflow-key": "peer", "egress-policy": "block"},
            },
        },
    }


def test_a_complete_notifier_passes() -> None:
    """Accept the shape the checked-in workflows use."""
    _assert_notifier(name="peer.yml", workflow=_good(), key="peer", pin="a" * 40)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("needs", ["build"]),
        ("if", "failure() && github.ref == 'refs/heads/main'"),
        ("if", "always() && github.ref == 'refs/heads/main'"),
        ("if", "(failure() || cancelled())"),
        ("uses", f"{_NOTIFIER}@{'b' * 40}"),
        ("uses", f"{_NOTIFIER}@main"),
        ("permissions", {"actions": "read", "contents": "read"}),
        ("permissions", {**_PERMISSIONS, "pull-requests": "write"}),
        ("with", {"workflow-key": "other", "egress-policy": "block"}),
        ("with", {"workflow-key": "peer", "egress-policy": "audit"}),
        ("with", {"workflow-key": "peer", "egress-policy": "block", "x": "y"}),
        ("secrets", "inherit"),
    ],
    ids=[
        "missing-need",
        "misses-cancellation",
        "fires-on-success",
        "fires-on-pull-requests",
        "other-pin",
        "branch-pin",
        "no-issues-write",
        "extra-scope",
        "wrong-key",
        "audit-egress",
        "undeclared-input",
        "secrets",
    ],
)
def test_an_incomplete_notifier_fails(field: str, value: Any) -> None:
    """Reject each way a notifier can miss a failure or over-reach."""
    workflow = _good()
    workflow["jobs"][_JOB][field] = value
    with pytest.raises(AssertionError):
        _assert_notifier(name="peer.yml", workflow=workflow, key="peer", pin="a" * 40)


def test_a_missing_notifier_fails() -> None:
    """Reject a covered workflow without the job."""
    workflow = _good()
    del workflow["jobs"][_JOB]
    with pytest.raises(AssertionError):
        _assert_notifier(name="peer.yml", workflow=workflow, key="peer", pin="a" * 40)


def test_a_differently_cased_notifier_is_still_found() -> None:
    """Match owner and repository case-insensitively, as GitHub does."""
    job = {"uses": f"{_NOTIFIER.upper()}@{'a' * 40}"}
    found = _notifiers({"peer.yml": {"jobs": {"report": job, "other": {}}}})
    assert_that(found).is_equal_to({"peer.yml": ["report"]})


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ({"schedule": [{"cron": "0 0 * * 1"}]}, True),
        ({"push": {"branches": ["main"]}}, True),
        ({"push": {"tags": ["v*"]}}, False),
        ({"pull_request": None, "merge_group": None}, False),
    ],
    ids=["schedule", "main-push", "tag-push", "pull-request"],
)
def test_main_or_schedule_detection(events: dict[str, Any], expected: bool) -> None:
    """Recognise the triggers that make a notifier reachable."""
    assert_that(_runs_on_main_or_schedule({"on": events})).is_equal_to(expected)
