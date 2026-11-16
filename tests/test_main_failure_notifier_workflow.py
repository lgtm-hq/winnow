"""Report failed main and scheduled runs as durable, deduplicated issues.

Each workflow that runs on a push to main or on a schedule ends with a
``notify-failure`` job calling lgtm-ci's ``reusable-main-failure-notifier``.
The reusable opens one issue per workflow key and comments on it for every
later failure, so the key must be stable and unique, and the job must wait for
every other job so a failure anywhere in the run is reported.
"""

from __future__ import annotations

import re
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
# Workflows that run on a main push or a schedule but carry no notifier, each
# with the reason. A new main or scheduled workflow must either get a notifier
# (and a _COVERED entry) or be listed here, and only such workflows may be.
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


# Branch filters follow GitHub's "Filter pattern cheat sheet" (workflow
# syntax reference). Supported constructs:
#   *       zero or more characters except "/"
#   **      zero or more of any character, "/" included
#   ?  +    zero-or-one / one-or-more of the preceding literal or [] class
#   [...]   one character from a list of letters and digits, or the ranges
#           a-z, A-Z and 0-9 (or sub-ranges of them, such as 1-2)
#   \x      the character x taken literally
#   !p      in ``branches`` only, as the first character: exclude refs that p
#           matches; patterns apply in order and the last match wins
# Every other character is literal. Anything else (a negated or malformed
# class, a quantifier with nothing literal before it, "!" in branches-ignore,
# both branches and branches-ignore, only negative patterns) raises, so an
# unsupported filter fails the test instead of being guessed at.
_BRANCH = "main"
_CLASS_RANGES = (("a", "z"), ("A", "Z"), ("0", "9"))


def _class_regex(body: str, pattern: str) -> str:
    """Translate a ``[...]`` class body, allowing only what GitHub documents."""
    if not body:
        raise ValueError(f"empty character class in branch pattern {pattern!r}")
    parts: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if not char.isascii() or not char.isalnum():
            raise ValueError(
                f"unsupported character {char!r} in class of branch pattern {pattern!r}"
            )
        if index + 2 < len(body) and body[index + 1] == "-":
            end = body[index + 2]
            if not any(low <= char <= end <= high for low, high in _CLASS_RANGES):
                raise ValueError(
                    f"unsupported range {char}-{end} in branch pattern {pattern!r}"
                )
            parts.append(f"{char}-{end}")
            index += 3
            continue
        parts.append(char)
        index += 1
    return "[" + "".join(parts) + "]"


def _token(pattern: str, index: int) -> tuple[str, bool, int]:
    """Translate the token at ``index``.

    Returns:
        The token's regex, whether a ``?`` or ``+`` may follow it, and the
        index after it.

    Raises:
        ValueError: On a class or escape the matcher does not support.
    """
    char = pattern[index]
    if pattern.startswith("**", index):
        return ".*", False, index + 2
    if char == "*":
        return "[^/]*", False, index + 1
    if char == "[":
        end = pattern.find("]", index + 1)
        if end == -1:
            raise ValueError(f"unclosed class in branch pattern {pattern!r}")
        return _class_regex(pattern[index + 1 : end], pattern), True, end + 1
    if char == "]":
        raise ValueError(f"unopened class in branch pattern {pattern!r}")
    if char == "\\":
        if index + 1 == len(pattern):
            raise ValueError(f"trailing escape in branch pattern {pattern!r}")
        return re.escape(pattern[index + 1]), True, index + 2
    return re.escape(char), True, index + 1


def _pattern_regex(pattern: str) -> str:
    """Translate one GitHub branch filter pattern into a regex.

    Raises:
        ValueError: On a quantifier with no literal or class before it.
    """
    out: list[str] = []
    # Whether the last token is a literal or class a quantifier binds to.
    quantifiable = False
    index = 0
    while index < len(pattern):
        if pattern[index] in "?+":
            if not quantifiable:
                raise ValueError(
                    f"unsupported {pattern[index]!r} without a preceding character"
                    f" in branch pattern {pattern!r}"
                )
            out.append(pattern[index])
            quantifiable = False
            index += 1
            continue
        token, quantifiable, index = _token(pattern, index)
        out.append(token)
    return "".join(out)


def _patterns(value: Any, key: str) -> list[str]:
    """Return a filter's patterns, accepting one string or a list of strings."""
    patterns = [value] if isinstance(value, str) else value
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) for pattern in patterns
    ):
        raise ValueError(f"push.{key} must be a string or a list of strings")
    return cast(list[str], patterns)


def _branch_matches(pattern: str, branch: str) -> bool:
    """Tell whether one positive pattern matches a branch name in full."""
    return re.fullmatch(_pattern_regex(pattern), branch) is not None


def _branches_include(patterns: list[str], branch: str) -> bool:
    """Apply a ``branches`` list in order; the last matching pattern wins."""
    if patterns and all(pattern.startswith("!") for pattern in patterns):
        raise ValueError("push.branches needs a pattern without '!'")
    included = False
    for pattern in patterns:
        negative = pattern.startswith("!")
        if _branch_matches(pattern[1:] if negative else pattern, branch):
            included = not negative
    return included


def _push_runs_on(push: dict[str, Any], branch: str) -> bool:
    """Tell whether a push trigger's branch and tag filters admit a branch push."""
    if "branches" in push and "branches-ignore" in push:
        raise ValueError("GitHub rejects branches together with branches-ignore")
    if "branches" in push:
        return _branches_include(_patterns(push["branches"], "branches"), branch)
    if "branches-ignore" in push:
        ignored = _patterns(push["branches-ignore"], "branches-ignore")
        if any(pattern.startswith("!") for pattern in ignored):
            raise ValueError("'!' in push.branches-ignore is not supported")
        return not any(_branch_matches(pattern, branch) for pattern in ignored)
    # With only tag filters the workflow never runs for branch pushes; with no
    # ref filter at all it runs for every branch.
    return "tags" not in push and "tags-ignore" not in push


def _runs_on_main_or_schedule(workflow: dict[str, Any]) -> bool:
    """Tell whether a workflow runs for main pushes or on a schedule."""
    events = _events(workflow)
    if "schedule" in events:
        return True
    if "push" not in events:
        return False
    return _push_runs_on(events["push"] or {}, _BRANCH)


def _unreported(
    workflows: dict[str, dict[str, Any]], exempt: dict[str, str]
) -> list[str]:
    """Return main or scheduled workflows with neither notifier nor exemption."""
    notifiers = _notifiers(workflows)
    return sorted(
        name
        for name, workflow in workflows.items()
        if _runs_on_main_or_schedule(workflow)
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


def test_covered_workflows_run_on_main_or_a_schedule() -> None:
    """A notifier in a workflow that never runs on main would never fire."""
    workflows = _workflows()
    for name in _COVERED:
        assert_that(_runs_on_main_or_schedule(workflows[name])).described_as(
            name
        ).is_true()


def test_every_main_or_scheduled_workflow_reports_or_is_exempt() -> None:
    """A new main or scheduled workflow must get a notifier or a reason.

    Scope: workflows that run on a push to main or on a schedule, because
    those are the only runs the notifier's condition reports. Workflows
    triggered only by ``workflow_run``, ``release`` or ``workflow_dispatch``
    are outside this check and are not required to carry a notifier.
    """
    assert_that(_unreported(_workflows(), _EXEMPT)).is_empty()


def _needless_exemptions(
    workflows: dict[str, dict[str, Any]], exempt: dict[str, str]
) -> list[str]:
    """Return exemptions that name a missing file or one that needs none."""
    return sorted(
        name
        for name in exempt
        if name not in workflows or not _runs_on_main_or_schedule(workflows[name])
    )


def test_exemptions_name_existing_uncovered_files_with_a_reason() -> None:
    """Keep the exemption list from going stale or overlapping coverage."""
    for name, reason in _EXEMPT.items():
        assert_that(_COVERED).described_as("exempt file").does_not_contain_key(name)
        assert_that(reason.strip()).described_as(name).is_not_empty()


def test_every_exemption_is_needed() -> None:
    """Exempt only files that exist and run on a main push or a schedule."""
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
    # Event spellings and schedules.
    ("schedule", {"schedule": [{"cron": "0 0 * * 1"}]}, True),
    ("pull-request", {"pull_request": None, "merge_group": None}, False),
    ("dispatch", "workflow_dispatch", False),
    ("workflow-run", {"workflow_run": {"workflows": ["CI"]}}, False),
    ("release", {"release": {"types": ["published"]}}, False),
    ("bare-push", "push", True),
    ("push-list", ["push", "pull_request"], True),
    ("push-null", {"push": None}, True),
    ("empty-push", {"push": {}}, True),
    ("path-filter", {"push": {"paths": ["src/**"]}}, True),
    # branches: literal names, a bare string and lists.
    ("main-push", {"push": {"branches": ["main"]}}, True),
    ("main-string", {"push": {"branches": "main"}}, True),
    ("other-branch", {"push": {"branches": ["release/*"]}}, False),
    ("prefix-is-not-main", {"push": {"branches": ["mai"]}}, False),
    ("longer-is-not-main", {"push": {"branches": ["main2"]}}, False),
    ("dot-is-literal", {"push": {"branches": ["m.in"]}}, False),
    # Wildcards.
    ("double-star", {"push": {"branches": ["**"]}}, True),
    ("star", {"push": {"branches": ["*"]}}, True),
    ("m-star", {"push": {"branches": ["m*"]}}, True),
    ("ma-star", {"push": {"branches": ["ma*"]}}, True),
    ("star-n", {"push": {"branches": ["*n"]}}, True),
    ("star-needs-slash", {"push": {"branches": ["*/*"]}}, False),
    ("double-star-slash", {"push": {"branches": ["**/main"]}}, False),
    ("other-star", {"push": {"branches": ["v*"]}}, False),
    # Quantifiers and classes.
    ("optional-char", {"push": {"branches": ["mainx?"]}}, True),
    ("repeated-char", {"push": {"branches": ["mai+n"]}}, True),
    ("plus-needs-one", {"push": {"branches": ["mainx+"]}}, False),
    ("class", {"push": {"branches": ["[lm]ain"]}}, True),
    ("class-range", {"push": {"branches": ["[a-z]ain"]}}, True),
    ("class-misses", {"push": {"branches": ["[A-Z]ain"]}}, False),
    ("class-plus", {"push": {"branches": ["[a-z]+"]}}, True),
    ("escaped-star", {"push": {"branches": ["mai\\*"]}}, False),
    # Negations, applied in order; the last match wins.
    ("negated-after", {"push": {"branches": ["**", "!main"]}}, False),
    ("re-included", {"push": {"branches": ["**", "!m*", "main"]}}, True),
    ("negation-before", {"push": {"branches": ["!main", "**"]}}, True),
    ("negation-other", {"push": {"branches": ["**", "!release/**"]}}, True),
    # branches-ignore.
    ("ignore-main", {"push": {"branches-ignore": ["main"]}}, False),
    ("ignore-star", {"push": {"branches-ignore": ["m*"]}}, False),
    ("ignore-other", {"push": {"branches-ignore": ["release/**"]}}, True),
    ("ignore-string", {"push": {"branches-ignore": "main"}}, False),
    ("tags-and-ignore", {"push": {"tags": ["v*"], "branches-ignore": ["x"]}}, True),
    # Tag-only pushes never run for a branch.
    ("tag-push", {"push": {"tags": ["v*"]}}, False),
    ("tags-ignore", {"push": {"tags-ignore": ["v*"]}}, False),
    (
        "tags-and-branches",
        {"push": {"tags": ["v*"], "branches": ["main"]}},
        True,
    ),
]


@pytest.mark.parametrize(
    ("events", "expected"),
    [(events, expected) for _, events, expected in _DETECTION_CASES],
    ids=[case_id for case_id, _, _ in _DETECTION_CASES],
)
def test_main_or_schedule_detection(events: Any, expected: bool) -> None:
    """Match push branch filters the way GitHub does for a push to main."""
    assert_that(_runs_on_main_or_schedule({"on": events})).is_equal_to(expected)


@pytest.mark.parametrize(
    "push",
    [
        {"branches": ["main"], "branches-ignore": ["x"]},
        {"branches": ["!main"]},
        {"branches-ignore": ["**", "!main"]},
        {"branches": ["[^x]ain"]},
        {"branches": ["[!x]ain"]},
        {"branches": ["[a-Z]ain"]},
        {"branches": ["[]main"]},
        {"branches": ["[main"]},
        {"branches": ["main]"]},
        {"branches": ["?main"]},
        {"branches": ["*+"]},
        {"branches": ["main\\"]},
        {"branches": [1]},
        {"branches": {"main": None}},
    ],
    ids=[
        "branches-and-ignore",
        "only-negations",
        "negation-in-ignore",
        "caret-class",
        "bang-class",
        "mixed-range",
        "empty-class",
        "unclosed-class",
        "unopened-class",
        "leading-question-mark",
        "quantified-wildcard",
        "trailing-escape",
        "non-string-pattern",
        "mapping-filter",
    ],
)
def test_unsupported_branch_filters_raise(push: dict[str, Any]) -> None:
    """Fail loudly on a filter construct the matcher does not implement."""
    with pytest.raises(ValueError):
        _runs_on_main_or_schedule({"on": {"push": push}})


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
