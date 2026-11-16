"""Keep the required aggregate aligned with the repository's producers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_GATE = _WORKFLOWS / "all-checks-gate.yml"
_REUSABLE = "lgtm-hq/lgtm-ci/.github/workflows/reusable-all-checks-gate.yml"
_DEFAULT_PR_TYPES = {"opened", "synchronize", "reopened"}


def _workflow(path: Path) -> dict[str, Any]:
    """Load GitHub's event key, including YAML 1.1's unquoted on spelling."""
    data = yaml.safe_load(path.read_text())
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def _assert_peer_pins(value: Any, pin: str) -> None:
    """Require every lgtm-ci use and explicit tooling ref to share the gate pin."""
    if isinstance(value, dict):
        uses = value.get("uses", "")
        if isinstance(uses, str) and uses.startswith("lgtm-hq/lgtm-ci/"):
            assert_that(uses).ends_with("@" + pin)
        if "tooling-ref" in value:
            assert_that(value["tooling-ref"]).is_equal_to(pin)
        for child in value.values():
            _assert_peer_pins(value=child, pin=pin)
    elif isinstance(value, list):
        for child in value:
            _assert_peer_pins(value=child, pin=pin)


def _assert_gate_pins(workflows: dict[str, str]) -> None:
    """Validate the reusable path, immutable pin, annotation and peer agreement."""
    text = workflows[_GATE.name]
    gate = yaml.safe_load(text)
    uses = gate["jobs"]["gate"]["uses"]
    assert_that(uses).matches(rf"^{re.escape(_REUSABLE)}@[0-9a-fA-F]{{40}}$")
    assert_that(text).matches(
        rf"(?m)^ +uses: +{re.escape(uses)} +# v[0-9]+\.[0-9]+\.[0-9]+[ \t]*$"
    )
    pin = uses.rsplit("@", 1)[1]
    for workflow in workflows.values():
        _assert_peer_pins(value=yaml.safe_load(workflow), pin=pin)


def test_gate_uses_an_annotated_commit_pin() -> None:
    """Keep the dedicated read-only caller pinned without freezing a release."""
    _assert_gate_pins(
        {path.name: path.read_text() for path in _WORKFLOWS.glob("*.yml")}
    )
    gate = _workflow(_GATE)
    assert_that(set(gate["jobs"])).is_equal_to({"gate"})
    job = gate["jobs"]["gate"]
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


@pytest.fixture
def updated_workflows() -> dict[str, str]:
    """Model a coherent future Renovate update entirely in scratch workflow text."""
    pin = _workflow(_GATE)["jobs"]["gate"]["uses"].rsplit("@", 1)[1]
    return {
        path.name: re.sub(
            r"# v[0-9]+\.[0-9]+\.[0-9]+",
            "# v9.8.7",
            path.read_text().replace(pin, "a" * 40),
        )
        for path in _WORKFLOWS.glob("*.yml")
    }


def test_coherent_pin_update_passes(updated_workflows: dict[str, str]) -> None:
    """Accept a second annotated SHA without editing a Python release constant."""
    _assert_gate_pins(updated_workflows)


@pytest.mark.parametrize(
    "pin",
    ["v9.8.7", "main", "a" * 7, "a" * 39, "a" * 41, "g" * 40, ""],
    ids=["tag", "branch", "short", "39-hex", "41-hex", "nonhex", "unpinned"],
)
def test_invalid_gate_pins_fail(updated_workflows: dict[str, str], pin: str) -> None:
    """Reject mutable or malformed pins even when every peer agrees."""
    workflows = {
        name: text.replace("a" * 40, pin) for name, text in updated_workflows.items()
    }
    with pytest.raises(AssertionError):
        _assert_gate_pins(workflows)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (" # v9.8.7", ""),
        (" # v9.8.7", " # latest"),
        ("reusable-all-checks-gate.yml", "reusable-quality-lint.yml"),
    ],
    ids=["missing-version", "invalid-version", "wrong-reusable"],
)
def test_invalid_gate_annotations_and_paths_fail(
    updated_workflows: dict[str, str], old: str, new: str
) -> None:
    """Require the exact reusable and its inline release annotation."""
    updated_workflows[_GATE.name] = updated_workflows[_GATE.name].replace(old, new)
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


@pytest.mark.parametrize(
    "peer",
    [
        "jobs:\n  peer:\n    uses: lgtm-hq/lgtm-ci/.github/workflows/peer.yml@{pin}",
        "jobs:\n  peer:\n    steps:\n"
        "      - uses: lgtm-hq/lgtm-ci/.github/actions/peer@{pin}",
        "jobs:\n  peer:\n    with:\n      tooling-ref: '{pin}'",
    ],
    ids=["workflow-use", "action-use", "tooling-ref"],
)
def test_peer_pin_drift_fails(updated_workflows: dict[str, str], peer: str) -> None:
    """Detect drift in reusable workflows, nested actions and explicit tooling refs."""
    updated_workflows["peer.yml"] = peer.format(pin="a" * 40)
    _assert_gate_pins(updated_workflows)
    updated_workflows["peer.yml"] = peer.format(pin="b" * 40)
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)
