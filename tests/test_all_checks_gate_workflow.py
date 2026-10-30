"""Keep the required aggregate aligned with the repository's producers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_GATE = _WORKFLOWS / "all-checks-gate.yml"
_PIN = "8f0fd9339df5d30a529aa572ceb51345b8be33a0"
_DEFAULT_PR_TYPES = {"opened", "synchronize", "reopened"}


def _workflow(path: Path) -> dict[str, Any]:
    """Load GitHub's event key, including YAML 1.1's unquoted on spelling."""
    data = yaml.safe_load(path.read_text())
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def test_gate_uses_the_reviewed_release() -> None:
    """Pin the dedicated read-only caller to the reviewed released graph."""
    gate = _workflow(_GATE)
    assert_that(set(gate["jobs"])).is_equal_to({"gate"})
    job = gate["jobs"]["gate"]
    assert_that(job["uses"]).is_equal_to(
        "lgtm-hq/lgtm-ci/.github/workflows/reusable-all-checks-gate.yml@" + _PIN
    )
    assert_that(gate["permissions"]).is_equal_to({})
    assert_that(job["permissions"]).is_equal_to(
        dict.fromkeys(("actions", "checks", "contents", "statuses"), "read")
    )
    assert_that(job).does_not_contain("secrets")
    assert_that(job.get("with", {})).is_equal_to({"egress-policy": "block"})


def test_gate_covers_every_pr_activity() -> None:
    """Refresh when any peer workflow can create checks on an unchanged head."""
    activities = set()
    for path in _WORKFLOWS.glob("*.yml"):
        if path == _GATE:
            continue
        events = _workflow(path)["on"]
        for event in ("pull_request", "pull_request_target"):
            if event in events:
                config = events[event] or {}
                activities.update(config.get("types", _DEFAULT_PR_TYPES))
    assert_that(activities).is_not_empty()
    events = _workflow(_GATE)["on"]
    assert_that(activities - set(events["pull_request"]["types"])).is_empty()
    filters = {"paths", "paths-ignore", "branches", "branches-ignore"}
    assert_that(filters & set(events["pull_request"])).is_empty()


def test_gate_runs_for_main_and_merge_queue() -> None:
    """Emit merged-main proof and preserve merge-queue checks-requested support."""
    events = _workflow(_GATE)["on"]
    assert_that(events["push"]).is_equal_to({"branches": ["main"]})
    assert_that(events).contains("merge_group")
    assert_that(
        (events["merge_group"] or {}).get("types", ["checks_requested"])
    ).is_equal_to(["checks_requested"])


def test_main_gate_cannot_be_cancelled_by_another_push() -> None:
    """PR refreshes cancel stale gates but each main push keeps its evidence."""
    concurrency = _workflow(_GATE)["concurrency"]
    assert_that(concurrency["cancel-in-progress"]).is_equal_to(
        "${{ github.event_name == 'pull_request' }}"
    )
    assert_that(concurrency["group"]).contains(
        "github.run_id", "github.event.pull_request.number", "github.ref"
    )
