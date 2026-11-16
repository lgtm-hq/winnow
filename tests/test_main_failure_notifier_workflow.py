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
# the display name and unique per workflow. Tag-only workflows never run on a
# main push, so they appear in neither mapping.
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
# Workflows with a schedule or a branch push that carry no notifier, each with
# the reason. A new scheduled or branch-push workflow must either get a
# notifier (and a _COVERED entry) or be listed here, and only such workflows
# may be.
_EXEMPT = {
    "all-checks-gate.yml": (
        "aggregate gate: it summarises producers that report their own"
        " failures, and its retry-aware behaviour belongs to #352"
    ),
    "release-auto-tag.yml": (
        "its lgtm-ci reusable reports its own failures (report-failures)"
    ),
    "release-version-pr.yml": (
        "its lgtm-ci reusable reports its own failures (report-failures)"
    ),
    "validate-lintro-version.yml": (
        "path-filtered main pushes cancel the in-progress run when a newer"
        " push arrives, so a notifier would file an issue for every"
        " superseded run; it is also not on #356's list"
    ),
}
# Fire when any needed job failed or was cancelled (a timed-out job concludes
# cancelled, which failure() and cancelled() both miss), only for main pushes
# and schedules: never for a pull request, merge queue, tag or manual
# dispatch, and never when every job succeeded or was skipped on purpose.
_CONDITION = (
    "always()"
    " && (contains(needs.*.result, 'failure')"
    " || contains(needs.*.result, 'cancelled'))"
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


def _events(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a workflow's triggers as a mapping, whichever form ``on`` has."""
    events = workflow["on"]
    if isinstance(events, str):
        return {events: None}
    if isinstance(events, list):
        return {event: None for event in events}
    return cast(dict[str, Any], events)


def _has_branch_push_or_schedule(workflow: dict[str, Any]) -> bool:
    """Tell whether a workflow has a schedule or a push trigger for branches.

    Deliberately not a judgement about ``main``: branch filter patterns are
    never evaluated here. Any push trigger that is not limited to tags counts,
    whatever its ``branches`` or ``branches-ignore`` say, so a workflow that
    really does skip main needs an explicit exemption with its reason instead
    of an inference about what a pattern matches.
    """
    events = _events(workflow)
    if "schedule" in events:
        return True
    if "push" not in events:
        return False
    push = events["push"] or {}
    if "branches" in push or "branches-ignore" in push:
        return True
    return "tags" not in push and "tags-ignore" not in push


def _unreported(
    workflows: dict[str, dict[str, Any]], exempt: dict[str, str]
) -> list[str]:
    """Return scheduled or branch-push workflows with no notifier or exemption."""
    notifiers = _notifiers(workflows)
    return sorted(
        name
        for name, workflow in workflows.items()
        if _has_branch_push_or_schedule(workflow)
        and name not in notifiers
        and name not in exempt
    )


# cancel-in-progress expressions that are false for every main push and every
# schedule (schedules always run on the default branch).
_QUEUES_ON_MAIN = {
    "${{ github.ref != 'refs/heads/main' }}",
    "${{ github.event_name == 'pull_request' }}",
}


def _cancels_on_main(concurrency: Any) -> bool:
    """Tell whether a concurrency setting may cancel a main or scheduled run.

    Anything but an absent or false setting, or an expression known to be false
    on main, counts as cancelling: a superseded run would otherwise be reported
    as a failure.
    """
    if not isinstance(concurrency, dict):
        return False
    cancel = concurrency.get("cancel-in-progress", False)
    if isinstance(cancel, bool):
        return cancel
    return " ".join(str(cancel).split()) not in _QUEUES_ON_MAIN


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


def test_covered_workflows_have_a_schedule_or_a_branch_push() -> None:
    """A notifier needs a schedule or a branch push, or it would never fire."""
    workflows = _workflows()
    for name in _COVERED:
        assert_that(_has_branch_push_or_schedule(workflows[name])).described_as(
            name
        ).is_true()


def test_every_scheduled_or_branch_push_workflow_reports_or_is_exempt() -> None:
    """A new scheduled or branch-push workflow must get a notifier or a reason.

    Scope: workflows with a schedule or a push trigger that is not limited to
    tags, because main pushes and schedules are the only runs the notifier's
    condition reports. Branch filters are not evaluated, so such a workflow
    that does not run on main is exempted by name with its reason. Workflows
    triggered only by ``workflow_run``, ``release``, ``workflow_dispatch`` or
    pull request events are outside this check.
    """
    assert_that(_unreported(_workflows(), _EXEMPT)).is_empty()


def _needless_exemptions(
    workflows: dict[str, dict[str, Any]], exempt: dict[str, str]
) -> list[str]:
    """Return exemptions that name a missing file or one that needs none."""
    return sorted(
        name
        for name in exempt
        if name not in workflows or not _has_branch_push_or_schedule(workflows[name])
    )


def test_exemptions_name_existing_uncovered_files_with_a_reason() -> None:
    """Keep the exemption list from going stale or overlapping coverage."""
    for name, reason in _EXEMPT.items():
        assert_that(_COVERED).described_as("exempt file").does_not_contain_key(name)
        assert_that(reason.strip()).described_as(name).is_not_empty()


def test_every_exemption_is_needed() -> None:
    """Exempt only files that exist and have a schedule or a branch push."""
    assert_that(_needless_exemptions(_workflows(), _EXEMPT)).is_empty()


def test_a_needless_exemption_is_found() -> None:
    """Flag a tag-only or missing workflow listed as exempt."""
    workflows: dict[str, dict[str, Any]] = {
        "nightly.yml": {"on": {"schedule": [{"cron": "0 0 * * *"}]}},
        "tag.yml": {"on": {"push": {"tags": ["v*"]}, "workflow_dispatch": None}},
    }
    exempt = {"nightly.yml": "why", "tag.yml": "why", "gone.yml": "why"}
    assert_that(_needless_exemptions(workflows, exempt)).is_equal_to(
        ["gone.yml", "tag.yml"]
    )


def test_the_condition_reports_failed_cancelled_and_timed_out_jobs() -> None:
    """Decide from needs.*.result: failure() and cancelled() miss timeouts."""
    workflows = _workflows()
    for name in _COVERED:
        job = workflows[name]["jobs"][_JOB]
        condition = " ".join(str(job.get("if", "")).split())
        assert_that(condition).described_as(name).starts_with("always() && ")
        assert_that(condition).described_as(name).contains(
            "contains(needs.*.result, 'failure')",
            "contains(needs.*.result, 'cancelled')",
        )
        bare = condition.replace("needs.*.result", "")
        assert_that(bare).described_as(name).does_not_contain(
            "failure()", "cancelled()"
        )


def test_covered_workflows_never_cancel_main_or_scheduled_runs() -> None:
    """A run superseded on main would otherwise be filed as a failure."""
    workflows = _workflows()
    for name in _COVERED:
        workflow = workflows[name]
        settings = [("workflow", workflow.get("concurrency"))] + [
            (job_id, job.get("concurrency"))
            for job_id, job in (workflow.get("jobs") or {}).items()
        ]
        for where, concurrency in settings:
            assert_that(_cancels_on_main(concurrency)).described_as(
                f"{name} {where}"
            ).is_false()


def test_workflow_keys_are_unique_and_match_the_file() -> None:
    """One issue per workflow: keys read from the files must not collide."""
    workflows = _workflows()
    keys = {
        name: workflow["jobs"][jobs[0]].get("with", {}).get("workflow-key")
        for name, workflow in workflows.items()
        if (jobs := _notifiers({name: workflow}).get(name))
    }
    assert_that(set(keys)).is_equal_to(set(_COVERED))
    assert_that(list(keys.values())).does_not_contain_duplicates()
    for name, key in keys.items():
        assert_that(key).described_as(name).is_equal_to(Path(name).stem)


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
        (
            "if",
            "failure()"
            " && github.ref == 'refs/heads/main'"
            " && (github.event_name == 'push' || github.event_name == 'schedule')",
        ),
        (
            "if",
            "(failure() || cancelled())"
            " && github.ref == 'refs/heads/main'"
            " && (github.event_name == 'push' || github.event_name == 'schedule')",
        ),
        (
            "if",
            "always()"
            " && contains(needs.*.result, 'failure')"
            " && github.ref == 'refs/heads/main'"
            " && (github.event_name == 'push' || github.event_name == 'schedule')",
        ),
        ("if", "always() && github.ref == 'refs/heads/main'"),
        ("if", "always() && contains(needs.*.result, 'failure')"),
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
        "failure-only-with-gates",
        "status-functions-miss-timeouts",
        "misses-cancelled-needs",
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


_DETECTION_CASES: list[tuple[str, Any, bool]] = [
    # Schedules, and every push that is not limited to tags.
    ("schedule", {"schedule": [{"cron": "0 0 * * 1"}]}, True),
    ("schedule-list", ["schedule"], True),
    ("push-string", "push", True),
    ("push-list", ["push", "pull_request"], True),
    ("push-empty", {"push": None}, True),
    ("push-main", {"push": {"branches": ["main"]}}, True),
    ("push-paths-only", {"push": {"paths": ["src/**"]}}, True),
    # Branch filters are never evaluated: each of these still needs a notifier
    # or an exemption that says why it does not.
    ("push-glob", {"push": {"branches": ["**"]}}, True),
    ("push-globstar-slash", {"push": {"branches": ["**/main"]}}, True),
    ("push-other-branch", {"push": {"branches": ["develop"]}}, True),
    ("push-negation", {"push": {"branches": ["**", "!main"]}}, True),
    ("push-ignore-main", {"push": {"branches-ignore": ["main"]}}, True),
    ("push-tags-and-branches", {"push": {"tags": ["v*"], "branches": ["main"]}}, True),
    # Not a branch push and not a schedule.
    ("push-tags", {"push": {"tags": ["v*"]}}, False),
    ("push-tags-ignore", {"push": {"tags-ignore": ["v0.*"]}}, False),
    ("pull-request", {"pull_request": None, "merge_group": None}, False),
    ("pull-request-target", {"pull_request_target": {"types": ["opened"]}}, False),
    ("dispatch", "workflow_dispatch", False),
    ("workflow-run", {"workflow_run": {"workflows": ["CI"]}}, False),
    ("release", {"release": {"types": ["published"]}}, False),
]


@pytest.mark.parametrize(
    ("events", "expected"),
    [(events, expected) for _, events, expected in _DETECTION_CASES],
    ids=[case_id for case_id, _, _ in _DETECTION_CASES],
)
def test_branch_push_or_schedule_detection(events: Any, expected: bool) -> None:
    """Count schedules and every push not limited to tags, without patterns."""
    assert_that(_has_branch_push_or_schedule({"on": events})).is_equal_to(expected)


def test_an_unreported_scheduled_workflow_is_found() -> None:
    """Flag a scheduled workflow with neither notifier nor exemption."""
    workflows: dict[str, dict[str, Any]] = {
        "nightly.yml": {"on": {"schedule": [{"cron": "0 0 * * *"}]}, "jobs": {}},
        "pr.yml": {"on": {"pull_request": None}, "jobs": {}},
    }
    assert_that(_unreported(workflows, {})).is_equal_to(["nightly.yml"])
    assert_that(_unreported(workflows, {"nightly.yml": "why"})).is_empty()


@pytest.mark.parametrize(
    ("concurrency", "expected"),
    [
        (None, False),
        ("group-only", False),
        ({"group": "g"}, False),
        ({"group": "g", "cancel-in-progress": False}, False),
        ({"group": "g", "cancel-in-progress": True}, True),
        (
            {
                "group": "g",
                "cancel-in-progress": "${{ github.ref != 'refs/heads/main' }}",
            },
            False,
        ),
        (
            {
                "group": "g",
                "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
            },
            False,
        ),
        (
            {
                "group": "g",
                "cancel-in-progress": "${{ github.event_name != 'schedule' }}",
            },
            True,
        ),
    ],
    ids=[
        "none",
        "string",
        "no-cancel-key",
        "false",
        "true",
        "not-main",
        "pull-request-only",
        "unknown-expression",
    ],
)
def test_cancelling_concurrency_is_detected(concurrency: Any, expected: bool) -> None:
    """Treat any setting not known to queue on main as cancelling."""
    assert_that(_cancels_on_main(concurrency)).is_equal_to(expected)
