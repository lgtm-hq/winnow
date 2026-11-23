"""Keep the file breakdown caller and bounded comment history wired correctly.

The file breakdown comment groups a PR's changed files by category. It and
three existing PR publishers keep up to three earlier runs in a collapsible
"Previous runs" archive instead of overwriting their comment on every push.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_BREAKDOWN = _WORKFLOWS / "file-breakdown.yml"
_GATE = _WORKFLOWS / "all-checks-gate.yml"
_REUSABLE = re.compile(
    r"^lgtm-hq/lgtm-ci/\.github/workflows/(?P<name>[^@]+)@(?P<pin>.*)$",
    re.IGNORECASE,
)
_DEFAULT_PR_TYPES = ["opened", "synchronize", "reopened"]
_SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
# Inputs reusable-publish-file-breakdown.yml declares in lgtm-ci v0.116.3, read
# from the released workflow file. A caller passing anything else would fail
# to start, so this list only grows when the pin is bumped and checked.
_BREAKDOWN_INPUTS = frozenset(
    {
        "allowed-endpoints",
        "allowed-endpoints-mode",
        "comment-fields",
        "comment-history",
        "comment-history-limit",
        "comment-marker",
        "config-path",
        "egress-policy",
        "egress-preset",
        "job-name",
        "max-rows",
        "runner-image",
        "timeout-minutes",
        "tooling-ref",
        "tooling-ref-allow-divergence",
    }
)
# Reusables that declare comment-history and comment-history-limit in lgtm-ci
# v0.116.3. Others, such as reusable-semantic-pr-title.yml, do not.
_DECLARE_HISTORY = frozenset(
    {
        "reusable-publish-file-breakdown.yml",
        "reusable-publish-quality-summary.yml",
        "reusable-publish-security-audit-comment.yml",
        "reusable-test-python.yml",
    }
)
# Every PR comment publisher that keeps bounded history: (workflow, job).
_HISTORY_PUBLISHERS = (
    ("file-breakdown.yml", "file-breakdown"),
    ("quality-ci.yml", "publish-quality-summary"),
    ("security-audit.yml", "publish-security-audit-comment"),
    ("test-ci.yml", "test-coverage"),
)


def _workflow(path: Path) -> dict[str, Any]:
    """Load a workflow, mapping YAML 1.1's unquoted ``on`` key back to text."""
    data = yaml.safe_load(path.read_text())
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def _breakdown_job() -> dict[str, Any]:
    """Return the single job of the file breakdown workflow."""
    jobs = _workflow(_BREAKDOWN)["jobs"]
    assert_that(set(jobs)).is_equal_to({"file-breakdown"})
    return cast(dict[str, Any], jobs["file-breakdown"])


def _reusable(job: dict[str, Any]) -> tuple[str, str] | None:
    """Return the (reusable file, pin) a job calls from lgtm-ci, if any."""
    uses = job.get("uses")
    if not isinstance(uses, str):
        return None
    match = _REUSABLE.match(uses.strip())
    return (match.group("name"), match.group("pin")) if match else None


def _history_inputs(job: dict[str, Any]) -> dict[str, Any]:
    """Return the history inputs a job passes, keyed by input name."""
    return {
        key: value
        for key, value in (job.get("with") or {}).items()
        if key.startswith("comment-history")
    }


def test_breakdown_calls_the_reusable_at_the_gate_pin() -> None:
    """Call the shared publisher at the same annotated pin as the gate."""
    called = _reusable(_breakdown_job())
    assert_that(called).is_not_none()
    name, pin = cast(tuple[str, str], called)
    assert_that(name).is_equal_to("reusable-publish-file-breakdown.yml")
    assert_that(pin).matches(r"^[0-9a-f]{40}$")
    gate_pin = _workflow(_GATE)["jobs"]["gate"]["uses"].strip().rsplit("@", 1)[1]
    assert_that(pin).is_equal_to(gate_pin)
    assert_that(_BREAKDOWN.read_text()).matches(
        rf"\n +uses: +\S+@{pin} +# v[0-9]+\.[0-9]+\.[0-9]+\n"
    )


def test_breakdown_grants_only_what_the_reusable_requests() -> None:
    """Grant nothing at workflow level and the publisher's two scopes on the job."""
    workflow = _workflow(_BREAKDOWN)
    assert_that(workflow["permissions"]).is_equal_to({})
    job = _breakdown_job()
    assert_that(job["permissions"]).is_equal_to(
        {"contents": "read", "pull-requests": "write"}
    )
    assert_that(job).does_not_contain_key("secrets")


def test_breakdown_runs_only_for_same_repository_pull_requests() -> None:
    """Never use pull_request_target, and skip fork PRs whose token is read-only."""
    events = _workflow(_BREAKDOWN)["on"]
    assert_that(set(events)).is_equal_to({"pull_request"})
    condition = " ".join(_breakdown_job()["if"].split())
    assert_that(condition).contains("github.event_name == 'pull_request'", _SAME_REPO)


def test_breakdown_blocks_egress_and_passes_only_declared_inputs() -> None:
    """Block egress with the minimal preset and stay inside the input contract."""
    inputs = _breakdown_job()["with"]
    assert_that(set(inputs) - _BREAKDOWN_INPUTS).is_empty()
    assert_that(inputs).contains_entry(
        {"egress-policy": "block"}, {"egress-preset": "github-minimal"}
    )


def test_gate_refreshes_on_every_breakdown_activity() -> None:
    """Let the aggregate gate observe the breakdown check on every PR activity."""
    config = _workflow(_BREAKDOWN)["on"]["pull_request"] or {}
    activities = set(config.get("types", _DEFAULT_PR_TYPES))
    gate_types = set(_workflow(_GATE)["on"]["pull_request"]["types"])
    assert_that(activities - gate_types).is_empty()


@pytest.mark.parametrize(
    ("workflow", "job_id"),
    _HISTORY_PUBLISHERS,
    ids=[workflow for workflow, _ in _HISTORY_PUBLISHERS],
)
def test_publishers_keep_three_earlier_runs(workflow: str, job_id: str) -> None:
    """Upsert one comment per marker and archive at most three earlier runs."""
    job = _workflow(_WORKFLOWS / workflow)["jobs"][job_id]
    assert_that(_history_inputs(job)).is_equal_to(
        {"comment-history": True, "comment-history-limit": 3}
    )


def test_history_is_only_passed_where_the_reusable_declares_it() -> None:
    """Keep history off semantic title, compat tests and every other caller."""
    publishers = set(_HISTORY_PUBLISHERS)
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        for job_id, job in (_workflow(path).get("jobs") or {}).items():
            label = f"{path.name}:{job_id}"
            history = _history_inputs(job)
            if (path.name, job_id) in publishers:
                called = _reusable(job)
                assert_that(called).described_as(label).is_not_none()
                name = cast(tuple[str, str], called)[0]
                assert_that(_DECLARE_HISTORY).described_as(label).contains(name)
            else:
                assert_that(history).described_as(label).is_empty()
