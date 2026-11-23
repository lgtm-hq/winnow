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
# The tag publish workflow, its approval-gated job and that job's environment.
# A release commit on main carries this workflow's tag run; its upload waits
# for a person to approve the environment, so the main-push gate leaves it out.
_PUBLISH = _WORKFLOWS / "publish-pypi-on-tag.yml"
_PUBLISH_NAME = "Publish - PyPI Production"
_PUBLISH_APPROVAL_JOB = "pypi-upload"
_PUBLISH_ENVIRONMENT = "pypi"
# Pushes to main ignore exactly the publish run; every other event gets an
# empty pattern, which the reusable reads as "gate everything".
_PUBLISH_RE = f"^{_PUBLISH_NAME}$"
_MAIN_PUSH_IGNORE = f"${{{{ github.event_name == 'push' && '{_PUBLISH_RE}' || '' }}}}"
_IGNORE_EXPRESSION = re.compile(
    r"^\$\{\{ github\.event_name == 'push' && '(?P<pattern>[^']*)' \|\| '' \}\}$"
)


def _workflow(path: Path) -> dict[str, Any]:
    """Load GitHub's event key, including YAML 1.1's unquoted on spelling."""
    data = yaml.safe_load(path.read_text())
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def _workflow_paths() -> list[Path]:
    """List every workflow file, whichever YAML extension it uses."""
    return sorted(
        path for pattern in ("*.yml", "*.yaml") for path in _WORKFLOWS.glob(pattern)
    )


def _workflow_texts() -> dict[str, str]:
    """Read every workflow file, keyed by file name."""
    return {path.name: path.read_text() for path in _workflow_paths()}


def _assert_peer_pins(value: Any, pin: str) -> None:
    """Require every lgtm-ci use and explicit tooling ref to share the gate pin."""
    if isinstance(value, dict):
        uses = value.get("uses", "")
        # GitHub resolves owner and repository names case-insensitively.
        if isinstance(uses, str) and uses.strip().lower().startswith(
            "lgtm-hq/lgtm-ci/"
        ):
            assert_that(uses.strip()).ends_with("@" + pin)
        # Workflow and action input names are case-insensitive too.
        for key, ref in value.items():
            if isinstance(key, str) and key.lower() == "tooling-ref":
                assert_that(str(ref).strip()).is_equal_to(pin)
        for child in value.values():
            _assert_peer_pins(value=child, pin=pin)
    elif isinstance(value, list):
        for child in value:
            _assert_peer_pins(value=child, pin=pin)


def _mapping_value(node: yaml.Node, key: str) -> yaml.Node:
    """Return the value node stored under ``key`` in a YAML mapping node."""
    assert_that(node).is_instance_of(yaml.MappingNode)
    for key_node, value_node in cast(yaml.MappingNode, node).value:
        if key_node.value == key:
            return cast(yaml.Node, value_node)
    raise AssertionError(f"missing key: {key}")


def _gate_uses_line(text: str) -> str:
    """Return the source line that holds the gate job's own ``uses`` value."""
    node = cast(yaml.Node, yaml.compose(text))
    for key in ("jobs", "gate", "uses"):
        node = _mapping_value(node=node, key=key)
    line_number = cast(int, node.start_mark.line)
    return text.splitlines()[line_number]


def _assert_gate_pins(workflows: dict[str, str]) -> None:
    """Validate the reusable path, immutable pin, annotation and peer agreement."""
    text = workflows[_GATE.name]
    gate = yaml.safe_load(text)
    uses = gate["jobs"]["gate"]["uses"]
    # Owner and repository names are case-insensitive; the path is not.
    owner_repo, path = _REUSABLE.split("/.github/", 1)
    assert_that(uses.strip()).matches(
        rf"^(?i:{re.escape(owner_repo)})/\.github/{re.escape(path)}@[0-9a-fA-F]{{40}}$"
    )
    # The release comment must sit on the gate job's own line, located from the
    # parsed document, so a comment or another job elsewhere cannot stand in.
    assert_that(_gate_uses_line(text)).matches(
        rf"""^ +uses: +(["']?){re.escape(uses)}\1 +# v[0-9]+\.[0-9]+\.[0-9]+[ \t]*$"""
    )
    pin = uses.strip().rsplit("@", 1)[1]
    for workflow in workflows.values():
        _assert_peer_pins(value=yaml.safe_load(workflow), pin=pin)


def test_gate_uses_an_annotated_commit_pin() -> None:
    """Keep the dedicated read-only caller pinned without freezing a release."""
    _assert_gate_pins(_workflow_texts())
    gate = _workflow(_GATE)
    assert_that(set(gate["jobs"])).is_equal_to({"gate"})
    job = gate["jobs"]["gate"]
    assert_that(gate["permissions"]).is_equal_to({})
    assert_that(job["permissions"]).is_equal_to(
        dict.fromkeys(("actions", "checks", "contents", "statuses"), "read")
    )
    assert_that(job).does_not_contain("secrets")
    assert_that(job.get("with", {})).is_equal_to(
        {"egress-policy": "block", "ignore-checks": _MAIN_PUSH_IGNORE}
    )


def test_gate_covers_every_pr_activity() -> None:
    """Refresh when any peer workflow can create checks on an unchanged head."""
    activities = set()
    for path in _workflow_paths():
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
    pin = _workflow(_GATE)["jobs"]["gate"]["uses"].strip().rsplit("@", 1)[1]
    return {
        name: re.sub(
            r"# v[0-9]+\.[0-9]+\.[0-9]+",
            "# v9.8.7",
            text.replace(pin, "a" * 40),
        )
        for name, text in _workflow_texts().items()
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


@pytest.mark.parametrize(
    "elsewhere",
    ["  other:\n{line}\n", "# moved to:\n# {line}\n"],
    ids=["another-job", "comment"],
)
def test_annotation_elsewhere_does_not_cover_the_gate(
    updated_workflows: dict[str, str], elsewhere: str
) -> None:
    """Reject a bare gate line even when the annotated line appears elsewhere."""
    text = updated_workflows[_GATE.name]
    line = _gate_uses_line(text)
    bare = line.split(" #", 1)[0]
    updated_workflows[_GATE.name] = text.replace(line, bare) + elsewhere.format(
        line=line
    )
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


def test_a_comment_repeating_the_reference_is_accepted(
    updated_workflows: dict[str, str],
) -> None:
    """Allow documentation that quotes the pinned reference next to the gate job."""
    text = updated_workflows[_GATE.name]
    line = _gate_uses_line(text)
    updated_workflows[_GATE.name] = text + f"# reviewed pin: {line.strip()}\n"
    _assert_gate_pins(updated_workflows)


def test_gate_owner_and_repository_case_is_accepted(
    updated_workflows: dict[str, str],
) -> None:
    """Accept another casing of the owner and repository, but not of the path."""
    text = updated_workflows[_GATE.name]
    updated_workflows[_GATE.name] = text.replace(
        "lgtm-hq/lgtm-ci/.github/workflows/reusable-all-checks-gate.yml",
        "LGTM-HQ/LGTM-CI/.github/workflows/reusable-all-checks-gate.yml",
    )
    _assert_gate_pins(updated_workflows)
    updated_workflows[_GATE.name] = text.replace(
        "reusable-all-checks-gate.yml", "Reusable-All-Checks-Gate.yml"
    )
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


def test_tooling_ref_key_case_does_not_hide_drift(
    updated_workflows: dict[str, str],
) -> None:
    """Treat a differently cased tooling-ref input as the same input."""
    peer = "jobs:\n  peer:\n    with:\n      TOOLING-REF: '{pin}'"
    updated_workflows["peer.yml"] = peer.format(pin="a" * 40)
    _assert_gate_pins(updated_workflows)
    updated_workflows["peer.yml"] = peer.format(pin="main")
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


def test_peer_owner_case_does_not_hide_drift(
    updated_workflows: dict[str, str],
) -> None:
    """Treat a differently cased owner as the same repository when checking drift."""
    peer = "jobs:\n  peer:\n    uses: LGTM-HQ/lgtm-ci/.github/workflows/peer.yml@{pin}"
    updated_workflows["peer.yml"] = peer.format(pin="a" * 40)
    _assert_gate_pins(updated_workflows)
    updated_workflows["peer.yml"] = peer.format(pin="main")
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


def _rewrite_gate_scalar(text: str, quote: str, pad: str) -> str:
    """Rewrite the gate reference with the given quoting and padding.

    The reference is read from the parsed document, so this works whether the
    checked-in line is plain, quoted or padded already.
    """
    reference = yaml.safe_load(text)["jobs"]["gate"]["uses"].strip()
    line = _gate_uses_line(text)
    indent = line.split("uses:", 1)[0]
    comment = line.rsplit("#", 1)[1]
    scalar = f"{quote}{pad}{reference}{pad}{quote}"
    return text.replace(line, f"{indent}uses: {scalar} #{comment}")


@pytest.mark.parametrize("quote", ["'", '"'], ids=["single", "double"])
def test_a_quoted_gate_reference_is_accepted(
    updated_workflows: dict[str, str], quote: str
) -> None:
    """Accept YAML quoting around the gate reference; the pin is unchanged."""
    updated_workflows[_GATE.name] = _rewrite_gate_scalar(
        text=updated_workflows[_GATE.name], quote=quote, pad=""
    )
    _assert_gate_pins(updated_workflows)


def test_whitespace_around_a_peer_reference_changes_nothing(
    updated_workflows: dict[str, str],
) -> None:
    """Trim a quoted peer reference before comparing, in both directions."""
    peer = (
        "jobs:\n  peer:\n    uses: ' lgtm-hq/lgtm-ci/.github/workflows/peer.yml@{pin} '"
    )
    updated_workflows["peer.yml"] = peer.format(pin="a" * 40)
    _assert_gate_pins(updated_workflows)
    updated_workflows["peer.yml"] = peer.format(pin="main")
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


def test_padding_inside_quotes_changes_nothing(
    updated_workflows: dict[str, str],
) -> None:
    """Trim a padded gate reference and a padded tooling ref before comparing."""
    updated_workflows[_GATE.name] = _rewrite_gate_scalar(
        text=updated_workflows[_GATE.name], quote="'", pad=" "
    )
    peer = "jobs:\n  peer:\n    with:\n      tooling-ref: ' {pin} '"
    updated_workflows["peer.yml"] = peer.format(pin="a" * 40)
    _assert_gate_pins(updated_workflows)
    updated_workflows["peer.yml"] = peer.format(pin="main")
    with pytest.raises(AssertionError):
        _assert_gate_pins(updated_workflows)


def test_both_workflow_extensions_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List workflow files with either YAML extension, and nothing else."""
    for name in ("b.yml", "a.yaml", "notes.md"):
        (tmp_path / name).write_text("jobs: {}\n")
    monkeypatch.setattr(f"{__name__}._WORKFLOWS", tmp_path)
    assert_that(sorted(_workflow_texts())).is_equal_to(["a.yaml", "b.yml"])


def _workflow_text(text: str) -> dict[str, Any]:
    """Parse workflow text, including YAML 1.1's unquoted on spelling."""
    data = yaml.safe_load(text)
    if "on" not in data:
        data["on"] = data.pop(True)
    return cast(dict[str, Any], data)


def _assert_main_push_ignore(workflows: dict[str, str]) -> None:
    """Require the main-push gate to drop exactly the tag publish workflow.

    The reusable matches ``ignore-checks`` against workflow-run names,
    check-run names and status contexts, and drops a matching run together
    with its check runs. A run's name is its workflow's ``name`` unless the
    workflow sets ``run-name``, so the pattern must equal that name exactly.
    """
    gate = _workflow_text(workflows[_GATE.name])
    expression = gate["jobs"]["gate"]["with"]["ignore-checks"]
    match = _IGNORE_EXPRESSION.match(expression)
    assert_that(match).is_not_none()
    pattern = cast(re.Match[str], match).group("pattern")

    publish = _workflow_text(workflows[_PUBLISH.name])
    assert_that(publish["name"]).is_equal_to(_PUBLISH_NAME)
    assert_that(publish).does_not_contain_key("run-name")
    assert_that(pattern).is_equal_to(f"^{publish['name']}$")
    job = publish["jobs"][_PUBLISH_APPROVAL_JOB]
    assert_that(job["environment"]).is_equal_to(_PUBLISH_ENVIRONMENT)
    # Publish runs only for a tag or by hand: never on a PR, in the merge
    # queue or for a branch push, so leaving it out drops no producer there.
    events = publish["on"]
    assert_that(set(events)).is_equal_to({"push", "workflow_dispatch"})
    assert_that(set(events["push"])).is_equal_to({"tags"})

    # Nothing else in the repository can be hidden by the pattern: no other
    # workflow name, and no job id or job display name anywhere.
    regex = re.compile(pattern)
    hidden = set()
    for name, text in workflows.items():
        data = _workflow_text(text)
        if name != _PUBLISH.name and regex.search(str(data.get("name", ""))):
            hidden.add(f"workflow {name}")
        for job_id, body in (data.get("jobs") or {}).items():
            for label in (job_id, str((body or {}).get("name", ""))):
                if regex.search(label):
                    hidden.add(f"job {name}:{label}")
    assert_that(hidden).is_empty()


def test_main_push_gate_ignores_only_the_tag_publish_run() -> None:
    """Keep a release's approval wait from timing out the gate on main (#422)."""
    _assert_main_push_ignore(_workflow_texts())


@pytest.mark.parametrize(
    ("workflow", "old", "new"),
    [
        ("all-checks-gate.yml", "'^Publish - PyPI Production$'", "'^Publish$'"),
        ("all-checks-gate.yml", "'^Publish - PyPI Production$'", "'Publish'"),
        (
            "all-checks-gate.yml",
            "'^Publish - PyPI Production$'",
            "'^Release - Auto Tag$'",
        ),
        ("all-checks-gate.yml", "github.event_name == 'push'", "true"),
        (
            "all-checks-gate.yml",
            "github.event_name == 'push'",
            "github.event_name == 'merge_group'",
        ),
        ("all-checks-gate.yml", "|| '' }}", "|| '^Publish' }}"),
        (
            "publish-pypi-on-tag.yml",
            "name: Publish - PyPI Production",
            "name: Publish - PyPI",
        ),
        (
            "publish-pypi-on-tag.yml",
            "name: Publish - PyPI Production\n",
            "name: Publish - PyPI Production\n"
            "run-name: Release ${{ github.ref_name }}\n",
        ),
        (
            "publish-pypi-on-tag.yml",
            "  push:\n    tags:",
            "  push:\n    branches: [main]\n    tags:",
        ),
        ("publish-pypi-on-tag.yml", "  workflow_dispatch:", "  merge_group:"),
        ("publish-pypi-on-tag.yml", "environment: pypi", "environment: release"),
    ],
    ids=[
        "prefix-only",
        "unanchored",
        "other-workflow",
        "every-event",
        "merge-queue",
        "non-push-pattern",
        "renamed-publish",
        "publish-run-name",
        "publish-on-main",
        "publish-in-queue",
        "other-environment",
    ],
)
def test_main_push_ignore_drift_fails(workflow: str, old: str, new: str) -> None:
    """A wrong name, scope or trigger for the ignored publish run fails."""
    workflows = _workflow_texts()
    assert_that(workflows[workflow]).contains(old)
    workflows[workflow] = workflows[workflow].replace(old, new)
    with pytest.raises(AssertionError):
        _assert_main_push_ignore(workflows)


def test_main_push_ignore_hides_no_other_job() -> None:
    """A job anywhere whose name the pattern matches would go ungated."""
    workflows = _workflow_texts()
    workflows["peer.yml"] = (
        "name: Peer\n'on': push\njobs:\n  peer:\n    name: Publish - PyPI Production\n"
    )
    with pytest.raises(AssertionError):
        _assert_main_push_ignore(workflows)


# The macOS test leg (#375) is required only through this gate: the reusable
# polls every check on the head and fails on anything but success, skipped or
# neutral, so its checks need no ruleset entry. They stop counting if the
# ignore pattern hides them, or if the leg can be skipped (a skipped check
# passes): a job ``if``, ``pipeline-skip``, or a path filter on the workflow.
_TEST_CI = "test-ci.yml"
_MACOS_JOB = "test-macos"
_TEST_REUSABLE = "lgtm-hq/lgtm-ci/.github/workflows/reusable-test-python.yml@"


def _macos_check_names(job: dict[str, Any]) -> list[str]:
    """Return the check-run names the macOS leg's reusable call creates.

    A called workflow's checks are named ``<caller job id> / <job name>``.
    The reusable sets its test job's ``name`` from ``job-name``, so every
    matrix leg shares that one name, with no version suffix (seen on #476).
    """
    inputs = cast(dict[str, Any], job["with"])
    jobs = (
        str(inputs["job-name"]),
        "Prepare Python Matrix",
        "Aggregate Python Results",
    )
    return [f"{_MACOS_JOB} / {name}" for name in jobs]


def _assert_macos_leg_is_gated(workflows: dict[str, str]) -> None:
    """Require a failed macOS leg to fail the gate on every gated event."""
    tests = _workflow_text(workflows[_TEST_CI])
    assert_that(tests["jobs"]).contains_key(_MACOS_JOB)
    job = cast(dict[str, Any], tests["jobs"][_MACOS_JOB])
    assert_that(str(job["uses"]).strip().lower()).starts_with(_TEST_REUSABLE)
    assert_that(str(job["with"]["runner-image"])).starts_with("macos-")
    for key in ("if", "continue-on-error", "strategy"):
        assert_that(job).described_as(f"{_MACOS_JOB} {key}").does_not_contain_key(key)
    assert_that(job["with"].get("pipeline-skip", False)).is_false()

    # It runs wherever the gate runs: pull requests, main pushes and the
    # merge queue, with no filter that leaves it out of a run.
    events = tests["on"]
    assert_that(events).contains_key("pull_request", "push", "merge_group")
    for event in ("pull_request", "push"):
        filters = {"paths", "paths-ignore", "branches-ignore", "tags"}
        assert_that(filters & set(events[event] or {})).described_as(
            f"{_TEST_CI} {event}"
        ).is_empty()
    assert_that(events["push"]["branches"]).contains("main")

    # Neither the main-push pattern nor the empty one hides its checks.
    gate = _workflow_text(workflows[_GATE.name])
    match = _IGNORE_EXPRESSION.match(gate["jobs"]["gate"]["with"]["ignore-checks"])
    assert_that(match).is_not_none()
    pattern = cast(re.Match[str], match).group("pattern")
    names = [str(tests["name"]), *_macos_check_names(job)]
    for name in names:
        assert_that(re.search(pattern, name)).described_as(name).is_none()


def test_a_macos_only_failure_fails_the_gate() -> None:
    """Keep every check of the macOS leg visible to the required gate."""
    workflows = _workflow_texts()
    _assert_macos_leg_is_gated(workflows)
    job = _workflow_text(workflows[_TEST_CI])["jobs"][_MACOS_JOB]
    assert_that(_macos_check_names(job)).contains(
        "test-macos / 🍎 macOS", "test-macos / Aggregate Python Results"
    )


@pytest.mark.parametrize(
    ("workflow", "old", "new"),
    [
        (
            "all-checks-gate.yml",
            "'^Publish - PyPI Production$'",
            "'^Publish - PyPI Production$|macOS'",
        ),
        (
            "all-checks-gate.yml",
            "'^Publish - PyPI Production$'",
            "'^Publish - PyPI Production$|^CI - Tests$'",
        ),
        (
            "all-checks-gate.yml",
            "'^Publish - PyPI Production$'",
            "'^Publish - PyPI Production$|^test-macos / '",
        ),
        (
            "test-ci.yml",
            "  test-macos:\n",
            "  test-macos:\n    if: github.event_name != 'pull_request'\n",
        ),
        (
            "test-ci.yml",
            "      runner-image: macos-26\n",
            "      runner-image: macos-26\n      pipeline-skip: true\n",
        ),
        ("test-ci.yml", "      runner-image: macos-26\n", "      runner-image: x\n"),
        (
            "test-ci.yml",
            "  pull_request:\n    branches: [main]\n",
            "  pull_request:\n    branches: [main]\n    paths-ignore: ['**.md']\n",
        ),
        ("test-ci.yml", "  merge_group:\n", ""),
        ("test-ci.yml", "  test-macos:\n", "  test-darwin:\n"),
    ],
    ids=[
        "ignored-by-name",
        "ignored-workflow",
        "ignored-job",
        "skipped-on-prs",
        "pipeline-skip",
        "not-macos",
        "path-filtered",
        "not-in-merge-queue",
        "missing",
    ],
)
def test_a_macos_leg_the_gate_cannot_see_fails(
    workflow: str, old: str, new: str
) -> None:
    """Hiding, skipping or dropping the macOS leg fails the check."""
    workflows = _workflow_texts()
    assert_that(workflows[workflow]).contains(old)
    workflows[workflow] = workflows[workflow].replace(old, new, 1)
    with pytest.raises(AssertionError):
        _assert_macos_leg_is_gated(workflows)
