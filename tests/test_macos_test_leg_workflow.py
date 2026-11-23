"""Keep the macOS test leg (#375) complete and without standing.

The leg is a direct job on a GitHub-hosted macOS runner. lgtm-ci's Python test
reusable cannot run there at v0.116.3 (its aggregate job needs coreutils
``timeout``), and lgtm-ci's harden-runner verify step does not enforce on
macOS, so egress is not enforced on this job. It therefore holds no secrets
and only ``contents: read``, persists no checkout credentials, uploads
nothing, and feeds no publish or release job: only the failure notifier needs
it. That the all-checks gate sees a failed leg is checked in
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

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_DOCKERFILE = _ROOT / "Dockerfile"
_TEST_CI = "test-ci.yml"
_JOB = "test-macos"
_COMPAT_JOB = "test"
_NOTIFIER_JOB = "notify-failure"
# Oldest and newest supported Python, as the issue asks.
_PYTHON_VERSIONS = ["3.11", "3.15"]
_CHECKOUT = "actions/checkout"
_SETUP_UV = "astral-sh/setup-uv"
_PINNED = re.compile(r"^(?P<action>[^@]+)@(?P<sha>[0-9a-f]{40})$")
_DOCKER_UV = re.compile(r"ghcr\.io/astral-sh/uv:(?P<version>[^@\s]+)@")


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


def _checkout_refs(workflows: dict[str, dict[str, Any]]) -> set[str]:
    """Return every ``actions/checkout`` reference outside the macOS job.

    Args:
        workflows: Parsed workflows keyed by file name.

    Returns:
        The distinct references, each ``actions/checkout@<sha>``.
    """
    refs = set()
    for name, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            if (name, job_id) == (_TEST_CI, _JOB):
                continue
            for step in job.get("steps") or []:
                uses = str(step.get("uses", ""))
                if uses.startswith(f"{_CHECKOUT}@"):
                    refs.add(uses)
    return refs


def _docker_uv_version() -> str:
    """Return the uv version the Dockerfile copies in.

    Returns:
        The image tag of ``ghcr.io/astral-sh/uv``.
    """
    match = _DOCKER_UV.search(_DOCKERFILE.read_text())
    assert_that(match).described_as("uv image in the Dockerfile").is_not_none()
    return cast(re.Match[str], match).group("version")


def _assert_macos_leg(workflows: dict[str, dict[str, Any]]) -> None:
    """Check the macOS job's runner, scopes, steps and downstream jobs.

    Args:
        workflows: Parsed workflows keyed by file name.
    """
    jobs = cast(dict[str, Any], workflows[_TEST_CI]["jobs"])
    assert_that(jobs).contains_key(_JOB)
    job = cast(dict[str, Any], jobs[_JOB])
    compat = cast(dict[str, Any], jobs[_COMPAT_JOB]["with"])

    # A pinned hosted macOS label, both supported ends, every leg finishing.
    assert_that(str(job["runs-on"])).matches(r"^macos-[0-9]+$")
    strategy = cast(dict[str, Any], job["strategy"])
    assert_that(strategy["fail-fast"]).is_false()
    assert_that(strategy["matrix"]).is_equal_to({"python-version": _PYTHON_VERSIONS})
    assert_that(job["timeout-minutes"]).is_instance_of(int).is_between(10, 30)

    # No standing: read-only contents, no secrets, no environment.
    assert_that(job.get("permissions")).is_equal_to({"contents": "read"})
    for key in ("secrets", "environment", "uses", "services", "container"):
        assert_that(job).described_as(key).does_not_contain_key(key)
    assert_that(yaml.safe_dump(job)).does_not_contain("secrets.")

    # Exactly: checkout without credentials, setup-uv on the Dockerfile's uv
    # and the matrix Python, sync, then the compat call's test command.
    steps = cast(list[dict[str, Any]], job["steps"])
    assert_that(steps).is_length(4)
    checkout, setup_uv, sync, run = steps
    for step, action in ((checkout, _CHECKOUT), (setup_uv, _SETUP_UV)):
        match = _PINNED.match(str(step.get("uses", "")))
        assert_that(match).described_as(str(step.get("uses"))).is_not_none()
        assert_that(cast(re.Match[str], match).group("action")).is_equal_to(action)
    assert_that({str(checkout["uses"])} | _checkout_refs(workflows)).is_length(1)
    assert_that(checkout["with"]).is_equal_to({"persist-credentials": False})
    assert_that(setup_uv["with"]).is_equal_to(
        {
            "version": _docker_uv_version(),
            "python-version": "${{ matrix.python-version }}",
            "enable-cache": False,
        }
    )
    assert_that(sync).does_not_contain_key("uses")
    assert_that(sync["run"]).is_equal_to("uv sync --frozen --dev")
    assert_that(run).does_not_contain_key("uses")
    assert_that(run["run"]).is_equal_to(
        f"uv run --frozen pytest {compat['test-path']} {compat['extra-args']}"
    )
    for step in (sync, run):
        assert_that(step).does_not_contain_key("env")

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
    """Keep the leg complete, read-only and off every publish path."""
    _assert_macos_leg(_workflows())


def test_the_failure_notifier_needs_the_macos_leg() -> None:
    """A red macOS leg on main files the usual failure issue."""
    notifier = _workflows()[_TEST_CI]["jobs"][_NOTIFIER_JOB]
    assert_that(_needs(notifier)).contains(_JOB)


def test_the_egress_exception_is_stated_at_the_job() -> None:
    """Keep the plain statement that egress is not enforced on this job."""
    text = (_WORKFLOWS / _TEST_CI).read_text()
    head, _, _ = text.partition(f"\n  {_JOB}:\n")
    comment = head.rsplit("\n\n", 1)[1]
    assert_that(comment).contains(
        "EGRESS EXCEPTION", "NOT enforced", "harden-runner", "contents: read"
    )


def _set(job: dict[str, Any], path: tuple[Any, ...], value: Any) -> None:
    """Set a nested key or list item in a parsed job, in place.

    Args:
        job: One parsed job, changed in place.
        path: Keys and list indexes from the job down to the value.
        value: The new value.
    """
    node: Any = job
    for key in path[:-1]:
        node = node.setdefault(key, {}) if isinstance(node, dict) else node[key]
    node[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("permissions", "id-token"), "write"),
        (("permissions", "contents"), "write"),
        (("permissions",), "read-all"),
        (("secrets",), "inherit"),
        (("steps", 2, "env"), {"NOTIFY_URL": "${{ secrets.WEBHOOK_URL }}"}),
        (("environment",), "pypi"),
        (("runs-on",), "ubuntu-26.04"),
        (("runs-on",), "macos-latest"),
        (("strategy", "fail-fast"), True),
        (("strategy", "matrix", "python-version"), ["3.13"]),
        (("timeout-minutes",), 360),
        (("steps", 0, "with", "persist-credentials"), True),
        (("steps", 0, "uses"), "actions/checkout@v7"),
        (("steps", 0, "uses"), f"{_CHECKOUT}@{'a' * 40}"),
        (("steps", 1, "uses"), f"{_SETUP_UV}@v10"),
        (("steps", 1, "with", "version"), "latest"),
        (("steps", 1, "with", "enable-cache"), True),
        (("steps", 2, "run"), "uv sync"),
        (("steps", 3, "run"), "uv run --frozen pytest tests"),
        (("steps", 3, "run"), "uv run --frozen pytest tests -m 'not integration'"),
    ],
    ids=[
        "id-token",
        "contents-write",
        "read-all",
        "secrets",
        "secret-in-env",
        "environment",
        "linux",
        "moving-label",
        "fail-fast",
        "one-version",
        "no-timeout-bound",
        "persisted-credentials",
        "unpinned-checkout",
        "other-checkout-pin",
        "unpinned-setup-uv",
        "moving-uv",
        "cache",
        "unfrozen-sync",
        "no-integration",
        "integration-excluded",
    ],
)
def test_a_macos_leg_with_more_standing_or_less_suite_fails(
    path: tuple[Any, ...], value: Any
) -> None:
    """Reject each widening of the leg's standing and each cut to its suite.

    Args:
        path: Keys and indexes from the job down to the changed value.
        value: The value that must be rejected.
    """
    workflows = copy.deepcopy(_workflows())
    _set(workflows[_TEST_CI]["jobs"][_JOB], path, value)
    with pytest.raises(AssertionError):
        _assert_macos_leg(workflows)


@pytest.mark.parametrize(
    "step",
    [
        {"uses": "actions/upload-artifact@" + "a" * 40, "with": {"path": "dist"}},
        {"uses": "step-security/harden-runner@" + "a" * 40},
        {"run": "curl -fsS https://example.org"},
    ],
    ids=["upload", "harden-runner", "extra-run"],
)
def test_an_extra_step_fails(step: dict[str, Any]) -> None:
    """Keep the job to its four steps: nothing published, nothing added.

    Args:
        step: A step appended to the job.
    """
    workflows = copy.deepcopy(_workflows())
    workflows[_TEST_CI]["jobs"][_JOB]["steps"].append(step)
    with pytest.raises(AssertionError):
        _assert_macos_leg(workflows)


@pytest.mark.parametrize(
    ("job_id", "change"),
    [
        ("publish-coverage", {"needs": ["test", "test-coverage", _JOB]}),
        ("test-coverage", {"needs": _JOB}),
        ("publish-coverage", {"if": f"needs.{_JOB}.result == 'success'"}),
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


def test_the_dockerfile_uv_version_is_read() -> None:
    """Guard the reader: the Dockerfile names a concrete uv release."""
    assert_that(_docker_uv_version()).matches(r"^[0-9]+\.[0-9]+\.[0-9]+$")
