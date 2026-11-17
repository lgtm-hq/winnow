"""Report a failed tag release as a durable, deduplicated issue.

The tag publish workflow ends with a ``notify-failure`` job calling lgtm-ci's
``reusable-release-failure-notifier``. The reusable files one issue per tag
when any needed job ended other than success or skipped, comments on it for a
later failed attempt and closes it when an attempt is fully green. It decides
from ``toJson(needs)`` itself, so the caller runs it with ``always()`` on tag
refs and lists every other job of the workflow in ``needs``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_GATE = _WORKFLOWS / "all-checks-gate.yml"
_PUBLISH = _WORKFLOWS / "publish-pypi-on-tag.yml"
_NOTIFIER = "lgtm-hq/lgtm-ci/.github/workflows/reusable-release-failure-notifier.yml"
_JOB = "notify-failure"
_KEY = "publish-pypi-on-tag"
# Jobs of the publish workflow that must finish before the report. Kept here
# so a job added to the workflow without a matching need is caught twice: by
# this list and by the needs-every-other-job rule.
_PUBLISH_JOBS = {
    "sbom",
    "pypi-build",
    "pypi-smoke-test",
    "pypi-upload",
    "github-release",
    "homebrew-dispatch",
}
# always(): the reusable both files on failure and closes on success, and it
# classifies toJson(needs) itself, so any failed, timed-out (cancelled) or
# cancelled job is filed and a green run only closes. Tag refs only, and not
# the actions-v tags every other job in the workflow skips.
_CONDITION = (
    "always()"
    " && github.ref_type == 'tag'"
    " && !startsWith(github.ref_name, 'actions-v')"
)
_PERMISSIONS = {"actions": "read", "contents": "read", "issues": "write"}
# Every input the reusable declares at the pinned lgtm-ci release (v0.99.0).
_DECLARED_INPUTS = {
    "workflow-key",
    "tag",
    "channels",
    "max-reruns",
    "signatures",
    "failure-issue-labels",
    "runner-image",
    "timeout-minutes",
    "egress-policy",
    "tooling-ref",
    "tooling-ref-allow-divergence",
}
# The inputs winnow sets and their values. tooling-ref stays unset: the
# reusable tracks its own pin. max-reruns is 0 because no automatic re-run
# watches the publish workflow; signatures only matter when it is above 0.
_INPUTS = {
    "workflow-key": _KEY,
    "tag": "${{ github.ref_name }}",
    "channels": "${{ toJson(needs) }}",
    "max-reruns": 0,
    "egress-policy": "block",
}


def _workflow(path: Path) -> dict[str, Any]:
    """Load GitHub's event key, including YAML 1.1's unquoted on spelling."""
    data = yaml.safe_load(path.read_text())
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def _pin() -> str:
    """Return the lgtm-ci commit the aggregate gate is pinned to."""
    uses = _workflow(_GATE)["jobs"]["gate"]["uses"]
    return cast(str, uses).strip().rsplit("@", 1)[1]


def _assert_notifier(workflow: dict[str, Any], pin: str) -> None:
    """Require one complete, correctly gated release notifier job."""
    assert_that(workflow.get("permissions")).is_equal_to({})
    jobs = cast(dict[str, Any], workflow["jobs"])
    assert_that(jobs).contains_key(_JOB)
    job = cast(dict[str, Any], jobs[_JOB])
    assert_that(str(job.get("uses", "")).strip()).is_equal_to(f"{_NOTIFIER}@{pin}")
    needs = job.get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    assert_that(sorted(needs or [])).described_as("needs").is_equal_to(
        sorted(job_id for job_id in jobs if job_id != _JOB)
    )
    condition = " ".join(str(job.get("if", "")).split())
    assert_that(condition).described_as("if").is_equal_to(_CONDITION)
    assert_that(job.get("permissions")).is_equal_to(_PERMISSIONS)
    inputs = cast(dict[str, Any], job.get("with") or {})
    assert_that(set(inputs)).described_as("with").is_subset_of(_DECLARED_INPUTS)
    assert_that(inputs).described_as("with").is_equal_to(_INPUTS)
    assert_that(job).does_not_contain_key("secrets")


def test_publish_workflow_reports_release_failures() -> None:
    """Give the tag publish workflow one complete release notifier."""
    _assert_notifier(_workflow(_PUBLISH), _pin())


def test_notifier_needs_every_publish_job() -> None:
    """Wait for SBOM, build, smoke test, upload, release and tap dispatch."""
    jobs = _workflow(_PUBLISH)["jobs"]
    assert_that(set(jobs) - {_JOB}).is_equal_to(_PUBLISH_JOBS)
    assert_that(set(jobs[_JOB]["needs"])).is_equal_to(_PUBLISH_JOBS)


def test_notifier_is_the_only_release_notifier() -> None:
    """Call the release notifier once, and only from the publish workflow."""
    callers = [
        (path.name, job_id)
        for path in sorted(_WORKFLOWS.glob("*.y*ml"))
        for job_id, job in (_workflow(path).get("jobs") or {}).items()
        if str(job.get("uses", "")).strip().lower().startswith(_NOTIFIER.lower())
    ]
    assert_that(callers).is_equal_to([(_PUBLISH.name, _JOB)])


def test_condition_does_not_rely_on_status_functions() -> None:
    """failure() and cancelled() miss a job that hit its time limit."""
    job = _workflow(_PUBLISH)["jobs"][_JOB]
    condition = " ".join(str(job.get("if", "")).split())
    assert_that(condition).starts_with("always() && ")
    assert_that(condition).does_not_contain("failure()", "cancelled()", "success()")


def test_waiting_upload_holds_back_the_notifier() -> None:
    """The notifier needs the environment-gated upload, so it waits for it.

    A job does not start until every job in its needs has completed, with or
    without always(). While Upload to PyPI waits for the pypi environment
    approval, the notifier has not started and files nothing.
    """
    jobs = _workflow(_PUBLISH)["jobs"]
    gated = {job_id for job_id, job in jobs.items() if "environment" in job}
    assert_that(gated).is_equal_to({"pypi-upload"})
    assert_that(jobs[_JOB]["needs"]).contains(*gated)


def _good() -> dict[str, Any]:
    """Build a minimal workflow whose notifier passes every check."""
    return {
        "on": {"push": {"tags": ["v*"]}},
        "permissions": {},
        "jobs": {
            "build": {"runs-on": "ubuntu-latest", "steps": []},
            "upload": {"needs": "build", "runs-on": "ubuntu-latest", "steps": []},
            _JOB: {
                "needs": ["build", "upload"],
                "if": _CONDITION,
                "uses": f"{_NOTIFIER}@{'a' * 40}",
                "permissions": dict(_PERMISSIONS),
                "with": dict(_INPUTS),
            },
        },
    }


def test_a_complete_notifier_passes() -> None:
    """Accept the shape the publish workflow uses."""
    _assert_notifier(_good(), "a" * 40)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("needs", ["build"]),
        ("needs", "upload"),
        ("if", "failure() && github.ref_type == 'tag'"),
        (
            "if",
            "(failure() || cancelled())"
            " && github.ref_type == 'tag'"
            " && !startsWith(github.ref_name, 'actions-v')",
        ),
        (
            "if",
            "always()"
            " && contains(needs.*.result, 'failure')"
            " && github.ref_type == 'tag'"
            " && !startsWith(github.ref_name, 'actions-v')",
        ),
        (
            "if",
            "!cancelled()"
            " && github.ref_type == 'tag'"
            " && !startsWith(github.ref_name, 'actions-v')",
        ),
        ("if", "always() && !startsWith(github.ref_name, 'actions-v')"),
        ("if", "always() && github.ref_type == 'tag'"),
        ("uses", f"{_NOTIFIER}@{'b' * 40}"),
        ("uses", f"{_NOTIFIER}@main"),
        (
            "uses",
            "lgtm-hq/lgtm-ci/.github/workflows/"
            f"reusable-main-failure-notifier.yml@{'a' * 40}",
        ),
        ("permissions", {"actions": "read", "contents": "read"}),
        ("permissions", {**_PERMISSIONS, "pull-requests": "write"}),
        ("with", {**_INPUTS, "workflow-key": "other"}),
        ("with", {**_INPUTS, "egress-policy": "audit"}),
        ("with", {**_INPUTS, "channels": "[]"}),
        ("with", {**_INPUTS, "tag": "${{ github.sha }}"}),
        ("with", {**_INPUTS, "max-reruns": 3}),
        ("with", {**_INPUTS, "tooling-ref": "a" * 40}),
        ("with", {**_INPUTS, "allowed-endpoints": "github.com:443"}),
        ("with", {k: v for k, v in _INPUTS.items() if k != "channels"}),
        ("secrets", "inherit"),
    ],
    ids=[
        "missing-need",
        "single-need",
        "failure-only",
        "status-functions-miss-timeouts",
        "skips-cancelled-and-green-closes",
        "misses-cancelled-runs",
        "fires-on-branch-dispatch",
        "fires-on-actions-tags",
        "other-pin",
        "branch-pin",
        "main-notifier",
        "no-issues-write",
        "extra-scope",
        "wrong-key",
        "audit-egress",
        "no-channels",
        "wrong-tag",
        "unwatched-rerun-budget",
        "tooling-ref-override",
        "undeclared-input",
        "missing-input",
        "secrets",
    ],
)
def test_an_incomplete_notifier_fails(field: str, value: Any) -> None:
    """Reject each way the notifier can miss a failure or over-reach."""
    workflow = _good()
    workflow["jobs"][_JOB][field] = value
    with pytest.raises(AssertionError):
        _assert_notifier(workflow, "a" * 40)


def test_workflow_level_permissions_fail() -> None:
    """Keep the workflow token empty; the notifier's scopes live on the job."""
    workflow = _good()
    workflow["permissions"] = {"issues": "write"}
    with pytest.raises(AssertionError):
        _assert_notifier(workflow, "a" * 40)


def test_a_missing_notifier_fails() -> None:
    """Reject a publish workflow without the job."""
    workflow = _good()
    del workflow["jobs"][_JOB]
    with pytest.raises(AssertionError):
        _assert_notifier(workflow, "a" * 40)
