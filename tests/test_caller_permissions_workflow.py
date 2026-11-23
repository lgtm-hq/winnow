"""Grant lgtm-ci reusable callers every scope their nested jobs request.

GitHub refuses to start a workflow (``startup_failure``) when a job inside a
called reusable workflow requests a permission its caller job does not grant.
Newer lgtm-ci releases add ``actions: read`` to the SARIF-uploading jobs
nested in CodeQL, Docker, SBOM and Scorecard, so every caller of those
reusables must grant it, including callers that only run on main, on a tag
or on a schedule and therefore are never exercised by a pull request.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_REUSABLE = re.compile(
    r"^lgtm-hq/lgtm-ci/\.github/workflows/(?P<name>[^@]+)@", re.IGNORECASE
)
# Reusable workflows winnow calls whose own jobs, or nested reusable jobs,
# request the ``actions`` scope in lgtm-ci v0.116.3. Read from the released
# workflow files, not from a caller, so a caller that drops the scope is
# caught. The list is fixed: when winnow starts calling another reusable, or
# an lgtm-ci release adds the scope to one, it has to be extended.
_NEED_ACTIONS_READ = frozenset(
    {
        "reusable-ai-review.yml",
        "reusable-all-checks-gate.yml",
        "reusable-auto-rerun-on-infra-failure.yml",
        "reusable-codeql.yml",
        "reusable-docker.yml",
        "reusable-main-failure-notifier.yml",
        "reusable-release-auto-tag.yml",
        "reusable-release-failure-notifier.yml",
        "reusable-release-version-pr.yml",
        "reusable-sbom.yml",
        "reusable-scorecards.yml",
        "reusable-test-python-publish.yml",
        "reusable-test-python.yml",
    }
)
# Callers that lacked the scope before lgtm-ci added it to nested SARIF jobs,
# with the exact scopes each must keep: the new grant is the only change.
_SARIF_CALLERS = {
    ("codeql.yml", "codeql"): {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    },
    ("docker-build-publish.yml", "docker"): {
        "actions": "read",
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
        "packages": "write",
        "security-events": "write",
    },
    ("sbom-on-main.yml", "sbom"): {
        "actions": "read",
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
        "security-events": "write",
    },
    ("publish-pypi-on-tag.yml", "sbom"): {
        "actions": "read",
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
        "security-events": "write",
    },
    ("scorecards.yml", "scorecards"): {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
        "security-events": "write",
    },
}


def _workflows() -> dict[str, dict[str, Any]]:
    """Parse every workflow file, keyed by file name."""
    paths = sorted(
        path for pattern in ("*.yml", "*.yaml") for path in _WORKFLOWS.glob(pattern)
    )
    return {
        path.name: cast(dict[str, Any], yaml.safe_load(path.read_text()))
        for path in paths
    }


def _reusable_name(job: dict[str, Any]) -> str | None:
    """Return the lgtm-ci reusable workflow file a job calls, if any."""
    uses = job.get("uses")
    if not isinstance(uses, str):
        return None
    match = _REUSABLE.match(uses.strip())
    return match.group("name") if match else None


def _callers(
    workflows: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return every job that calls a reusable needing ``actions: read``."""
    return {
        (name, job_id): job
        for name, workflow in workflows.items()
        for job_id, job in (workflow.get("jobs") or {}).items()
        if _reusable_name(job) in _NEED_ACTIONS_READ
    }


def _assert_callers_grant_actions_read(workflows: dict[str, dict[str, Any]]) -> None:
    """Require each caller to grant ``actions: read`` on the job itself."""
    for (name, job_id), job in _callers(workflows).items():
        permissions = job.get("permissions")
        assert_that(permissions).described_as(f"{name}:{job_id}").is_instance_of(dict)
        granted = cast(dict[str, Any], permissions)
        assert_that(granted.get("actions")).described_as(
            f"{name}:{job_id} actions"
        ).is_in("read", "write")


def _assert_workflow_permissions_empty(workflows: dict[str, dict[str, Any]]) -> None:
    """Require ``permissions: {}`` at workflow level so jobs opt in explicitly."""
    for name, workflow in workflows.items():
        assert_that(workflow).described_as(name).contains_key("permissions")
        assert_that(workflow["permissions"]).described_as(name).is_equal_to({})


def test_every_workflow_grants_nothing_at_workflow_level() -> None:
    """Keep the default token empty; scopes belong on the jobs that use them."""
    _assert_workflow_permissions_empty(_workflows())


def test_callers_of_reusables_needing_actions_read_grant_it() -> None:
    """Grant the scope a nested job requests, or GitHub will not start the run."""
    _assert_callers_grant_actions_read(_workflows())


def test_sarif_callers_are_found_and_keep_only_their_scopes() -> None:
    """Cover the PR, main, tag and scheduled callers with their exact scopes."""
    callers = _callers(_workflows())
    assert_that(callers).contains_key(*_SARIF_CALLERS)
    for key, expected in _SARIF_CALLERS.items():
        assert_that(callers[key]["permissions"]).described_as(
            ":".join(key)
        ).is_equal_to(expected)


@pytest.mark.parametrize(
    "permissions",
    [None, {"contents": "read"}, {"actions": "none"}, "read-all"],
    ids=["inherited", "missing", "none", "shorthand"],
)
def test_a_caller_without_actions_read_fails(permissions: Any) -> None:
    """Reject a caller that omits, denies or does not spell out the scope."""
    job: dict[str, Any] = {
        "uses": "lgtm-hq/lgtm-ci/.github/workflows/reusable-sbom.yml@" + "a" * 40,
    }
    if permissions is not None:
        job["permissions"] = permissions
    workflows = {"peer.yml": {"permissions": {}, "jobs": {"sbom": job}}}
    with pytest.raises(AssertionError):
        _assert_callers_grant_actions_read(workflows)


def test_a_differently_cased_owner_is_still_checked() -> None:
    """Match the owner and repository case-insensitively, as GitHub does."""
    job = {"uses": "LGTM-HQ/LGTM-CI/.github/workflows/reusable-codeql.yml@main"}
    workflows = {"peer.yml": {"permissions": {}, "jobs": {"codeql": job}}}
    with pytest.raises(AssertionError):
        _assert_callers_grant_actions_read(workflows)


def test_unrelated_jobs_are_not_required_to_grant_actions_read() -> None:
    """Leave callers of other reusables and plain jobs alone."""
    workflows = {
        "peer.yml": {
            "permissions": {},
            "jobs": {
                "other": {
                    "uses": "lgtm-hq/lgtm-ci/.github/workflows/"
                    "reusable-validate-action-pinning.yml@" + "a" * 40,
                    "permissions": {"contents": "read"},
                },
                "local": {"runs-on": "ubuntu-latest", "steps": []},
            },
        }
    }
    _assert_callers_grant_actions_read(workflows)


@pytest.mark.parametrize(
    "permissions",
    [{"actions": "read"}, "read-all", "write-all", None],
    ids=["scoped", "read-all", "write-all", "absent"],
)
def test_non_empty_workflow_level_permissions_fail(permissions: Any) -> None:
    """Reject any workflow-level grant, including a missing key."""
    workflow: dict[str, Any] = {"jobs": {}}
    if permissions is not None:
        workflow["permissions"] = permissions
    with pytest.raises(AssertionError):
        _assert_workflow_permissions_empty({"peer.yml": workflow})
