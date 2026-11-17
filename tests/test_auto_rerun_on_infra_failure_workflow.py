"""Re-run failed jobs once after a recognised infrastructure failure.

``auto-rerun-on-infra-failure.yml`` reacts to completed runs of a fixed list of
workflows and calls lgtm-ci's ``reusable-auto-rerun-on-infra-failure``, which
re-runs the failed jobs only when their logs match a known transient
infrastructure signature, and only while the attempt is within ``max-reruns``.
``workflow_run`` uses the default branch's definition with a privileged token,
so the caller must keep fork runs away from it and never run triggering code.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_GATE = _WORKFLOWS / "all-checks-gate.yml"
_RERUN = _WORKFLOWS / "auto-rerun-on-infra-failure.yml"
_REUSABLE = "lgtm-hq/lgtm-ci/.github/workflows/reusable-auto-rerun-on-infra-failure.yml"
_JOB = "rerun"
# Workflow names watched, each with the file that defines it. Producers that
# run on pull requests, merge queues, main pushes or schedules and only read or
# build. Release, tag, publishing, registry-cleanup, gate and PR-writing
# workflows are deliberately absent (the workflow file says why).
_WATCHED = {
    "Build - Docker Image & Registry": "docker-build-publish.yml",
    "CI - Tests": "test-ci.yml",
    "PR - Title Validation": "semantic-pr-title.yml",
    "Quality - Lint & Format": "quality-ci.yml",
    "Quality Check - Action Pinning Validation": "validate-action-pinning.yml",
    "Quality Check - Lintro Version Sync": "validate-lintro-version.yml",
    "Security - CodeQL Code Scanning": "codeql.yml",
    "Security - Dependency Review": "dependency-review.yml",
    "Security - OSV Audit": "security-audit.yml",
    "Security - OpenSSF Scorecard Analysis": "scorecards.yml",
    "Security - SBOM Generation": "sbom-on-main.yml",
}
# Never watched: tag release pipeline (#271), state-changing release and
# cleanup workflows, the aggregate gate (#352), and this workflow itself.
_NEVER_WATCHED = {
    "auto-rerun-on-infra-failure.yml",
    "all-checks-gate.yml",
    "ghcr-cleanup.yml",
    "publish-pypi-on-tag.yml",
    "publish-testpypi.yml",
    "release-auto-tag.yml",
    "release-version-pr.yml",
}
# Failed runs only, from this repository, for pull requests, merge queues,
# schedules and main pushes. Tag pushes and manual runs are not re-run.
_CONDITION = (
    "github.event.workflow_run.conclusion == 'failure'"
    " && github.event.workflow_run.head_repository.full_name == github.repository"
    " && (github.event.workflow_run.event == 'pull_request'"
    " || github.event.workflow_run.event == 'merge_group'"
    " || github.event.workflow_run.event == 'schedule'"
    " || (github.event.workflow_run.event == 'push'"
    " && github.event.workflow_run.head_branch == 'main'))"
)
# Exactly the scopes the pinned reusable's job requests.
_PERMISSIONS = {"actions": "write", "checks": "read", "contents": "read"}
# Inputs the pinned reusable (lgtm-ci v0.99.0) declares. Kept here so a typo
# or an input removed upstream fails a test rather than the run at startup.
_DECLARED_INPUTS = frozenset(
    {
        "run-id",
        "run-attempt",
        "max-reruns",
        "signatures",
        "protected-workflows",
        "protected-job-pattern",
        "tooling-ref",
        "tooling-ref-allow-divergence",
        "egress-policy",
        "allowed-endpoints",
        "allowed-endpoints-mode",
        "egress-preset",
        "runner-image",
        "timeout-minutes",
    }
)
# Inputs winnow sets. tooling-ref is left unset on purpose: the reusable
# tracks its own pin and fails on a diverging override.
_INPUTS = {
    "run-id": "${{ format('{0}', github.event.workflow_run.id) }}",
    "run-attempt": "${{ format('{0}', github.event.workflow_run.run_attempt) }}",
    "max-reruns": "1",
    "signatures": "Docker info timed out after harden-runner initialized",
    "egress-policy": "block",
    "egress-preset": "github-results",
}


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


def _names(workflows: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Map each workflow ``name:`` to the files that declare it."""
    found: dict[str, list[str]] = {}
    for file_name, workflow in workflows.items():
        found.setdefault(str(workflow.get("name")), []).append(file_name)
    return found


def _assert_rerun(workflow: dict[str, Any], pin: str) -> None:
    """Require the trigger, guards, scopes, pin and inputs of the retry job."""
    assert_that(workflow.get("permissions")).is_equal_to({})
    assert_that(set(workflow["on"])).is_equal_to({"workflow_run"})
    trigger = cast(dict[str, Any], workflow["on"]["workflow_run"])
    assert_that(set(trigger)).is_equal_to({"workflows", "types"})
    assert_that(trigger["types"]).is_equal_to(["completed"])
    assert_that(sorted(trigger["workflows"])).is_equal_to(sorted(_WATCHED))
    jobs = cast(dict[str, Any], workflow["jobs"])
    assert_that(set(jobs)).is_equal_to({_JOB})
    job = cast(dict[str, Any], jobs[_JOB])
    assert_that(job["uses"].strip()).is_equal_to(f"{_REUSABLE}@{pin}")
    condition = " ".join(str(job.get("if", "")).split())
    assert_that(condition).is_equal_to(_CONDITION)
    assert_that(job.get("permissions")).is_equal_to(_PERMISSIONS)
    inputs = cast(dict[str, Any], job.get("with") or {})
    assert_that(_DECLARED_INPUTS.issuperset(inputs)).described_as(
        f"undeclared inputs: {sorted(set(inputs) - _DECLARED_INPUTS)}"
    ).is_true()
    normalised = {key: str(value).strip() for key, value in inputs.items()}
    assert_that(normalised).is_equal_to(_INPUTS)
    assert_that(job).does_not_contain_key("secrets")
    assert_that(job).does_not_contain_key("steps")


def test_the_retry_workflow_is_complete() -> None:
    """Check the checked-in workflow against every rule at once."""
    _assert_rerun(_workflow(_RERUN), _pin())


def test_the_retry_job_uses_the_shared_pin_and_release_comment() -> None:
    """Pin the reusable to the gate's commit with the same release comment."""
    pin = _pin()
    gate_line = next(
        line for line in _GATE.read_text().splitlines() if f"@{pin}" in line
    )
    comment = gate_line.split("#", 1)[1].strip()
    lines = [line for line in _RERUN.read_text().splitlines() if _REUSABLE in line]
    assert_that(lines).is_length(1)
    assert_that(lines[0].strip()).is_equal_to(f"uses: {_REUSABLE}@{pin} # {comment}")


def test_every_watched_name_is_a_workflow_name() -> None:
    """A name GitHub cannot match would silently never trigger a retry."""
    names = _names(_workflows())
    listed = _workflow(_RERUN)["on"]["workflow_run"]["workflows"]
    assert_that(listed).does_not_contain_duplicates()
    for name in listed:
        assert_that(names).described_as("workflow name").contains_key(name)
        assert_that(names[name]).described_as(name).is_equal_to([_WATCHED[name]])


def test_workflow_names_are_unique() -> None:
    """Two files with one name would make the watch list ambiguous."""
    for name, files in _names(_workflows()).items():
        assert_that(files).described_as(name).is_length(1)


def test_release_gate_and_writing_workflows_are_not_watched() -> None:
    """Keep tag releases (#271), the gate (#352) and itself out of the list."""
    workflows = _workflows()
    watched_files = set(_WATCHED.values())
    for file_name in _NEVER_WATCHED:
        assert_that(workflows).contains_key(file_name)
        assert_that(watched_files).does_not_contain(file_name)


def test_the_budget_is_one_rerun() -> None:
    """Attempt 1 may be re-run; the reusable leaves attempt 2 to a human."""
    inputs = _workflow(_RERUN)["jobs"][_JOB]["with"]
    assert_that(str(inputs["max-reruns"]).strip()).is_equal_to("1")


def test_no_code_from_the_triggering_run_is_checked_out() -> None:
    """The caller has no steps, so it cannot check out or run the head."""
    text = _RERUN.read_text()
    assert_that(text).does_not_contain("actions/checkout")
    assert_that(text).does_not_contain("head_sha")
    assert_that(text).does_not_contain("head_commit")
    for job in _workflow(_RERUN)["jobs"].values():
        assert_that(job).does_not_contain_key("steps")
        assert_that(job).does_not_contain_key("runs-on")


def _good() -> dict[str, Any]:
    """Build a workflow that passes every check, as checked in."""
    return copy.deepcopy(_workflow(_RERUN))


def test_a_complete_workflow_passes() -> None:
    """Accept the shape the checked-in workflow uses, at any pin."""
    workflow = _good()
    workflow["jobs"][_JOB]["uses"] = f"{_REUSABLE}@{'a' * 40}"
    _assert_rerun(workflow, "a" * 40)


def _set(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Return the good workflow with one value replaced or removed."""
    workflow = _good()
    node = workflow
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return workflow


_DELETE = object()
_JOB_PATH = ("jobs", _JOB)
_WITH_PATH = (*_JOB_PATH, "with")


def _with(**changes: Any) -> dict[str, Any]:
    """Return the good inputs with some replaced, added or removed."""
    inputs = dict(_good()["jobs"][_JOB]["with"])
    for key, value in changes.items():
        key = key.replace("_", "-")
        if value is _DELETE:
            del inputs[key]
        else:
            inputs[key] = value
    return inputs


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("permissions",), {"actions": "read"}),
        (("permissions",), _DELETE),
        (("on",), {"workflow_run": {"workflows": ["CI - Tests"]}}),
        (
            ("on", "workflow_run"),
            {"workflows": sorted(_WATCHED), "types": ["completed", "requested"]},
        ),
        (
            ("on", "workflow_run"),
            {"workflows": [*sorted(_WATCHED), "CI - Test"], "types": ["completed"]},
        ),
        (
            ("on", "workflow_run"),
            {"workflows": sorted(_WATCHED)[1:], "types": ["completed"]},
        ),
        (
            ("on", "workflow_run"),
            {
                "workflows": sorted(_WATCHED),
                "types": ["completed"],
                "branches": ["main"],
            },
        ),
        (("on",), {"workflow_run": None, "push": None}),
        ((*_JOB_PATH, "uses"), f"{_REUSABLE}@{'b' * 40}"),
        ((*_JOB_PATH, "uses"), f"{_REUSABLE}@main"),
        ((*_JOB_PATH, "if"), "github.event.workflow_run.conclusion == 'failure'"),
        (
            (*_JOB_PATH, "if"),
            _CONDITION.replace(
                " && github.event.workflow_run.head_repository.full_name"
                " == github.repository",
                "",
            ),
        ),
        (
            (*_JOB_PATH, "if"),
            _CONDITION.replace("'failure'", "'cancelled'"),
        ),
        (
            (*_JOB_PATH, "if"),
            _CONDITION.replace(
                " && github.event.workflow_run.head_branch == 'main'", ""
            ),
        ),
        ((*_JOB_PATH, "if"), _DELETE),
        ((*_JOB_PATH, "permissions"), {**_PERMISSIONS, "issues": "write"}),
        ((*_JOB_PATH, "permissions"), {"actions": "write", "contents": "read"}),
        ((*_JOB_PATH, "permissions"), {**_PERMISSIONS, "contents": "write"}),
        ((*_JOB_PATH, "permissions"), "write-all"),
        ((*_JOB_PATH, "secrets"), "inherit"),
        (
            (*_JOB_PATH, "steps"),
            [{"uses": "actions/checkout@" + "a" * 40}],
        ),
        (_WITH_PATH, _with(max_reruns="2")),
        (_WITH_PATH, _with(max_reruns=_DELETE)),
        (_WITH_PATH, _with(max_reruns="0")),
        (_WITH_PATH, _with(egress_policy="audit")),
        (_WITH_PATH, _with(tooling_ref="a" * 40)),
        (_WITH_PATH, _with(reruns="1")),
        (_WITH_PATH, _with(signatures="failed")),
        (_WITH_PATH, _with(protected_job_pattern="")),
        (_WITH_PATH, _with(run_id="${{ github.event.workflow_run.id }}")),
    ],
    ids=[
        "workflow-permissions",
        "workflow-permissions-missing",
        "no-completed-type",
        "extra-type",
        "unknown-name",
        "missing-name",
        "branch-filter-drops-schedules",
        "extra-trigger",
        "other-pin",
        "branch-pin",
        "no-fork-or-event-guard",
        "no-fork-guard",
        "reruns-cancellations",
        "reruns-tag-pushes",
        "no-condition",
        "extra-scope",
        "no-checks-read",
        "contents-write",
        "write-all",
        "secrets",
        "checks-out-code",
        "budget-two",
        "default-budget",
        "budget-zero",
        "audit-egress",
        "tooling-ref",
        "undeclared-input",
        "broad-signature",
        "protection-disabled",
        "numeric-run-id",
    ],
)
def test_an_unsafe_or_incomplete_workflow_fails(
    path: tuple[str, ...], value: Any
) -> None:
    """Reject each way the retry can loop, over-reach or never fire."""
    with pytest.raises(AssertionError):
        _assert_rerun(_set(path, value), _pin())
