"""Keep the macOS test leg (#375) complete and without standing.

The leg calls lgtm-ci's Python test reusable on a GitHub-hosted macOS runner.
There the reusable skips harden-runner and its verify step passes with a
notice, so ``egress-policy: block`` is not enforced. The leg therefore gets
no secrets and no ``id-token``, writes only under its own artifact prefix, and
feeds no publish or release job: only the failure notifier needs it. That
the all-checks gate sees a failed leg is checked in
``test_all_checks_gate_workflow.py``.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_TEST_CI = "test-ci.yml"
_JOB = "test-macos"
_COMPAT_JOB = "test"
_NOTIFIER_JOB = "notify-failure"
_REUSABLE = re.compile(
    r"^lgtm-hq/lgtm-ci/\.github/workflows/reusable-test-python\.yml@",
    re.IGNORECASE,
)
# Scopes the caller must grant, each because a job in the pinned reusable asks
# for it: GitHub refuses to start the run otherwise. No macOS job receives the
# write scope: only the summary job asks for it, and it is off for this call.
_PERMISSIONS = {"actions": "read", "contents": "read", "pull-requests": "write"}
# Oldest and newest supported Python, as the issue asks.
_PYTHON_VERSIONS = "3.11,3.15"
_PREFIX = "macos"


def _workflows() -> dict[str, dict[str, Any]]:
    """Parse every workflow file, keyed by file name.

    Returns:
        Each workflow's parsed YAML.
    """
    paths = sorted(
        path for pattern in ("*.yml", "*.yaml") for path in _WORKFLOWS.glob(pattern)
    )
    return {
        path.name: cast(dict[str, Any], yaml.safe_load(path.read_text()))
        for path in paths
    }


def _needs(job: dict[str, Any]) -> list[str]:
    """Return a job's ``needs`` as a list.

    Args:
        job: One parsed job.

    Returns:
        The job ids it needs.
    """
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _assert_macos_leg(workflows: dict[str, dict[str, Any]]) -> None:
    """Check the macOS leg's inputs, scopes and downstream jobs.

    Args:
        workflows: Parsed workflows keyed by file name.
    """
    jobs = cast(dict[str, Any], workflows[_TEST_CI]["jobs"])
    assert_that(jobs).contains_key(_JOB)
    job = cast(dict[str, Any], jobs[_JOB])
    assert_that(_REUSABLE.match(str(job["uses"]).strip())).is_not_none()
    inputs = cast(dict[str, Any], job["with"])
    compat = cast(dict[str, Any], jobs[_COMPAT_JOB]["with"])

    # The full suite, integration tests included, on macOS.
    assert_that(str(inputs["runner-image"])).matches(r"^macos-[0-9]+$")
    assert_that(inputs["python-versions"]).is_equal_to(_PYTHON_VERSIONS)
    for key in ("test-path", "extra-args"):
        assert_that(inputs[key]).described_as(key).is_equal_to(compat[key])
    assert_that(inputs).does_not_contain_key("markers")
    assert_that(inputs["egress-policy"]).is_equal_to("block")

    # No standing: read-only scopes on every macOS job, no secrets, nothing
    # published, no comment on the pull request.
    assert_that(job.get("permissions")).is_equal_to(_PERMISSIONS)
    assert_that(job).does_not_contain_key("secrets")
    assert_that(inputs["coverage"]).is_false()
    assert_that(inputs.get("upload-coverage", False)).is_false()
    assert_that(inputs["publish-test-summary"]).is_false()

    # Its own artifact namespace, used by no other test call anywhere.
    assert_that(inputs["artifact-prefix"]).is_equal_to(_PREFIX)
    for name, workflow in workflows.items():
        for job_id, other in (workflow.get("jobs") or {}).items():
            if (name, job_id) == (_TEST_CI, _JOB):
                continue
            prefix = (other.get("with") or {}).get("artifact-prefix")
            assert_that(prefix).described_as(f"{name}:{job_id}").is_not_equal_to(
                _PREFIX
            )

    # Nothing but the failure notifier waits on it or reads its outputs, so
    # it reaches neither the coverage publish nor any release path.
    for name, workflow in workflows.items():
        for job_id, other in (workflow.get("jobs") or {}).items():
            where = f"{name}:{job_id}"
            if name == _TEST_CI and job_id == _NOTIFIER_JOB:
                continue
            if name == _TEST_CI:
                assert_that(_needs(other)).described_as(where).does_not_contain(_JOB)
            assert_that(yaml.safe_dump(other)).described_as(where).does_not_contain(
                f"needs.{_JOB}"
            )


def test_the_macos_leg_runs_the_full_suite_without_standing() -> None:
    """Keep the leg complete, read-only, separate and off every publish path."""
    _assert_macos_leg(_workflows())


def test_the_egress_exception_is_stated_at_the_call() -> None:
    """Keep the plain statement that egress is not enforced on this job."""
    text = (_WORKFLOWS / _TEST_CI).read_text()
    head, _, _ = text.partition(f"\n  {_JOB}:\n")
    comment = head.rsplit("\n\n", 1)[1]
    assert_that(comment).contains("EGRESS EXCEPTION", "NOT enforced", "macOS")


def _set(job: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Set a nested key in a parsed job, creating mappings on the way.

    Args:
        job: One parsed job, changed in place.
        path: Keys from the job down to the value.
        value: The new value.
    """
    node = job
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("permissions", "id-token"), "write"),
        (("permissions", "contents"), "write"),
        (("permissions", "actions"), "write"),
        (("secrets",), "inherit"),
        (("with", "artifact-prefix"), "compat"),
        (("with", "coverage"), True),
        (("with", "upload-coverage"), True),
        (("with", "publish-test-summary"), True),
        (("with", "runner-image"), "ubuntu-26.04"),
        (("with", "runner-image"), "macos-latest"),
        (("with", "python-versions"), "3.13"),
        (("with", "extra-args"), ""),
        (("with", "markers"), "not integration"),
        (("with", "egress-policy"), "audit"),
    ],
    ids=[
        "id-token",
        "contents-write",
        "actions-write",
        "secrets",
        "shared-prefix",
        "coverage",
        "upload-coverage",
        "pr-comment",
        "linux",
        "moving-label",
        "one-version",
        "no-integration",
        "markers",
        "audit",
    ],
)
def test_a_macos_leg_with_more_standing_or_less_suite_fails(
    path: tuple[str, ...], value: Any
) -> None:
    """Reject each widening of the leg's standing and each cut to its suite.

    Args:
        path: Keys from the job down to the changed value.
        value: The value that must be rejected.
    """
    workflows = copy.deepcopy(_workflows())
    _set(workflows[_TEST_CI]["jobs"][_JOB], path, value)
    with pytest.raises(AssertionError):
        _assert_macos_leg(workflows)


@pytest.mark.parametrize(
    ("job_id", "change"),
    [
        ("publish-coverage", {"needs": ["test", "test-coverage", _JOB]}),
        ("test-coverage", {"needs": _JOB}),
        ("publish-coverage", {"if": f"needs.{_JOB}.outputs.passed == 'true'"}),
    ],
    ids=["publish-needs-it", "coverage-needs-it", "publish-reads-it"],
)
def test_a_job_that_depends_on_the_macos_leg_fails(
    job_id: str, change: dict[str, Any]
) -> None:
    """Keep the coverage publish and other jobs independent of the leg.

    Args:
        job_id: A job in the test workflow.
        change: Keys to set on it.
    """
    workflows = copy.deepcopy(_workflows())
    workflows[_TEST_CI]["jobs"][job_id].update(change)
    with pytest.raises(AssertionError):
        _assert_macos_leg(workflows)


def test_a_release_job_that_reads_the_macos_leg_fails() -> None:
    """Catch a dependency from another workflow too."""
    workflows = copy.deepcopy(_workflows())
    workflows["peer.yml"] = {
        "jobs": {"release": {"if": f"needs.{_JOB}.result == 'success'"}},
    }
    with pytest.raises(AssertionError):
        _assert_macos_leg(workflows)


def test_another_call_on_the_macos_prefix_fails() -> None:
    """Two calls on one prefix would overwrite each other's results."""
    workflows = copy.deepcopy(_workflows())
    workflows[_TEST_CI]["jobs"]["test"]["with"]["artifact-prefix"] = _PREFIX
    with pytest.raises(AssertionError):
        _assert_macos_leg(workflows)
