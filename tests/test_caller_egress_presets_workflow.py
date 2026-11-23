"""Pass each lgtm-ci reusable workflow only an egress preset it accepts.

Since lgtm-ci v0.111.0 each reusable workflow embeds only its own egress
presets, and its "Fail on unknown egress-preset" step stops the job when the
preset it would use is not one of them. A pull request exercises only some
callers, so a preset the pinned reusable does not accept would otherwise
surface on main, on a tag or on a schedule. These tests fail locally first.

A caller selects the job's allow-list in one of three ways, and each is
checked on its own terms:

- no ``egress-preset`` and no replacement list: the reusable's default preset;
- an ``egress-preset``, alone or in append mode: that preset, which the
  reusable checks against the presets it accepts;
- a non-empty ``allowed-endpoints`` list in replace mode (the default mode):
  the list itself. The reusable then ignores the preset and skips its check,
  but a preset it does not accept would fail as soon as the list is removed or
  switched to append, so a caller may name only an accepted preset or none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_REUSABLE = re.compile(
    r"^lgtm-hq/lgtm-ci/\.github/workflows/(?P<name>[^@]+)@(?P<pin>\S+)",
    re.IGNORECASE,
)
# The lgtm-ci commit the table below was read from: v0.116.3. Every caller
# must pin it, so moving the pin means reading the table again.
_LGTM_CI_COMMIT = "0c373f1252e0fe23a1e119df639b51d63b02b7d8"


@dataclass(frozen=True)
class _Presets:
    """The egress presets of one reusable workflow at ``_LGTM_CI_COMMIT``.

    Attributes:
        default: Preset the reusable uses when the caller names none.
        accepted: Presets on the reusable's ``# lgtm-ci-egress-accepts:`` line.
    """

    default: str
    accepted: frozenset[str]


def _presets(default: str, *also: str) -> _Presets:
    """Build a table row whose accepted set is the default plus ``also``."""
    return _Presets(default=default, accepted=frozenset({default, *also}))


# Every reusable workflow winnow calls, read from each file at
# ``_LGTM_CI_COMMIT``: the default from its ``inputs.egress-preset || '<name>'``
# selector and the accepted set from its ``# lgtm-ci-egress-accepts:`` line.
# The two failure notifiers declare no egress-preset input and always use
# github-minimal. When winnow calls another reusable, or the pin moves, the
# table has to be read again from the new commit.
_TABLE = {
    "reusable-ai-review.yml": _presets("ai-review"),
    "reusable-all-checks-gate.yml": _presets("github-results"),
    "reusable-auto-rerun-on-infra-failure.yml": _presets("github-results"),
    "reusable-build-python-dist.yml": _presets("python-dist"),
    "reusable-codeql.yml": _presets("github-tooling"),
    "reusable-dependency-review.yml": _presets("github-tooling"),
    "reusable-docker.yml": _presets("docker"),
    "reusable-ghcr-cleanup.yml": _presets("github-tooling"),
    "reusable-github-release.yml": _presets("github-tooling"),
    "reusable-main-failure-notifier.yml": _presets("github-minimal"),
    "reusable-pr-auto-assign.yml": _presets("github-tooling"),
    "reusable-pr-labeler.yml": _presets("github-tooling"),
    "reusable-publish-file-breakdown.yml": _presets("github-minimal"),
    "reusable-publish-quality-summary.yml": _presets("github-minimal"),
    "reusable-publish-security-audit-comment.yml": _presets("github-minimal"),
    "reusable-quality-lint.yml": _presets("quality"),
    "reusable-release-auto-tag.yml": _presets("github-tooling", "github-minimal"),
    "reusable-release-failure-notifier.yml": _presets("github-minimal"),
    "reusable-release-version-pr.yml": _presets(
        "release-version-pr",
        "github-minimal",
    ),
    "reusable-sbom.yml": _presets("sbom"),
    "reusable-scorecards.yml": _presets("scorecard"),
    "reusable-security-audit.yml": _presets("quality"),
    "reusable-semantic-pr-title.yml": _presets("github-minimal"),
    "reusable-test-python-publish.yml": _presets("github-pages"),
    "reusable-test-python.yml": _presets("pypi"),
    "reusable-validate-action-pinning.yml": _presets("github-tooling"),
    "reusable-validate-lintro-version.yml": _presets("quality"),
}
# The Python test calls: the two Linux ones, which used to pass the quality
# preset, and the macOS leg (#375), where the preset is passed but not
# enforced because the reusable skips harden-runner on macOS.
_PYTHON_CALLERS = (
    ("test-ci.yml", "test"),
    ("test-ci.yml", "test-coverage"),
    ("test-ci.yml", "test-macos"),
)


class _Selection(StrEnum):
    """How a caller selects the allow-list its reusable job runs with."""

    DEFAULT = auto()
    PRESET = auto()
    REPLACEMENT = auto()


@dataclass(frozen=True)
class _Caller:
    """One job that calls an lgtm-ci reusable workflow.

    Attributes:
        reusable: File name of the called reusable workflow.
        pin: Ref after the ``@`` in ``uses``.
        inputs: The job's ``with`` mapping.
    """

    reusable: str
    pin: str
    inputs: dict[str, Any]


def _workflows() -> dict[str, dict[str, Any]]:
    """Parse every workflow file, keyed by file name."""
    paths = sorted(
        path for pattern in ("*.yml", "*.yaml") for path in _WORKFLOWS.glob(pattern)
    )
    return {
        path.name: cast(dict[str, Any], yaml.safe_load(path.read_text()))
        for path in paths
    }


def _callers(workflows: dict[str, dict[str, Any]]) -> dict[tuple[str, str], _Caller]:
    """Return every job that calls an lgtm-ci reusable, keyed by file and job."""
    callers: dict[tuple[str, str], _Caller] = {}
    for name, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            uses = job.get("uses")
            match = _REUSABLE.match(uses.strip()) if isinstance(uses, str) else None
            if match is None:
                continue
            callers[(name, job_id)] = _Caller(
                reusable=match.group("name"),
                pin=match.group("pin"),
                inputs=cast(dict[str, Any], job.get("with") or {}),
            )
    return callers


def _selection(inputs: dict[str, Any]) -> _Selection:
    """Tell how these inputs select the job's allow-list.

    Mirrors the reusables: a non-empty ``allowed-endpoints`` list replaces the
    preset unless ``allowed-endpoints-mode`` is ``append``.
    """
    endpoints = str(inputs.get("allowed-endpoints") or "").strip()
    if endpoints and inputs.get("allowed-endpoints-mode") != "append":
        return _Selection.REPLACEMENT
    if inputs.get("egress-preset") is None:
        return _Selection.DEFAULT
    return _Selection.PRESET


def _effective_preset(caller: _Caller) -> str | None:
    """Return the preset the reusable job runs with, or None for a list."""
    selection = _selection(caller.inputs)
    if selection is _Selection.REPLACEMENT:
        return None
    if selection is _Selection.DEFAULT:
        return _TABLE[caller.reusable].default
    return str(caller.inputs["egress-preset"])


def _assert_callers_pass_accepted_presets(
    workflows: dict[str, dict[str, Any]],
) -> None:
    """Require every preset a caller uses or names to be an accepted one."""
    for (name, job_id), caller in _callers(workflows).items():
        where = f"{name}:{job_id}"
        assert_that(_TABLE).described_as(where).contains_key(caller.reusable)
        accepted = _TABLE[caller.reusable].accepted
        effective = _effective_preset(caller)
        if effective is not None:
            assert_that(effective).described_as(f"{where} preset").is_in(*accepted)
        named = caller.inputs.get("egress-preset")
        if named is not None:
            assert_that(named).described_as(f"{where} egress-preset").is_in(
                *accepted,
            )


def _caller(reusable: str, inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a one-job workflow set that calls ``reusable`` with ``inputs``."""
    job = {
        "uses": f"lgtm-hq/lgtm-ci/.github/workflows/{reusable}@" + "a" * 40,
        "with": inputs,
    }
    return {"peer.yml": {"permissions": {}, "jobs": {"call": job}}}


def test_every_caller_passes_presets_its_reusable_accepts() -> None:
    """Keep each caller on a preset the pinned reusable embeds."""
    _assert_callers_pass_accepted_presets(_workflows())


def test_the_table_was_read_at_the_pinned_lgtm_ci_commit() -> None:
    """Fail a pin move until the table is read again from the new commit."""
    pins = {caller.pin for caller in _callers(_workflows()).values()}
    assert_that(pins).is_equal_to({_LGTM_CI_COMMIT})


def test_the_collector_finds_callers_on_every_trigger() -> None:
    """Guard the collector: pull request, main, tag and scheduled callers."""
    assert_that(_callers(_workflows())).contains_key(
        *_PYTHON_CALLERS,
        ("quality-ci.yml", "publish-quality-summary"),
        ("release-version-pr.yml", "version-pr"),
        ("publish-pypi-on-tag.yml", "sbom"),
        ("scorecards.yml", "scorecards"),
    )


@pytest.mark.parametrize(
    "key", _PYTHON_CALLERS, ids=["test", "test-coverage", "test-macos"]
)
def test_every_python_caller_runs_with_the_default_pypi_preset(
    key: tuple[str, str],
) -> None:
    """Name the preset the Python matrix and coverage jobs now run with."""
    caller = _callers(_workflows())[key]
    assert_that(caller.reusable).is_equal_to("reusable-test-python.yml")
    assert_that(_selection(caller.inputs)).is_equal_to(_Selection.DEFAULT)
    assert_that(_effective_preset(caller)).is_equal_to("pypi")


@pytest.mark.parametrize(
    ("key", "preset"),
    [
        (("docker-build-publish.yml", "docker"), "docker"),
        (("publish-pypi-on-tag.yml", "pypi-build"), "python-dist"),
        (("release-version-pr.yml", "version-pr"), "github-minimal"),
        (("scorecards.yml", "scorecards"), "scorecard"),
        (("codeql.yml", "codeql"), "github-tooling"),
    ],
    ids=["docker", "pypi-build", "version-pr", "scorecards", "codeql"],
)
def test_migrated_callers_run_an_accepted_preset(
    key: tuple[str, str],
    preset: str,
) -> None:
    """Name the preset each call moved to in #465, now that the reusable checks it.

    These calls used to pass a full replacement list, which skips the
    reusable's preset check. Each now runs its preset, so a preset the pinned
    reusable does not accept would fail the job.
    """
    caller = _callers(_workflows())[key]
    assert_that(_selection(caller.inputs)).is_equal_to(_Selection.PRESET)
    assert_that(_effective_preset(caller)).is_equal_to(preset)
    assert_that(_TABLE[caller.reusable].accepted).contains(preset)


def test_every_table_default_is_accepted() -> None:
    """Keep the table consistent: a reusable accepts its own default."""
    for reusable, presets in _TABLE.items():
        assert_that(presets.accepted).described_as(reusable).contains(
            presets.default,
        )


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        ({}, _Selection.DEFAULT),
        ({"egress-preset": "pypi"}, _Selection.PRESET),
        (
            {
                "egress-preset": "pypi",
                "allowed-endpoints-mode": "append",
                "allowed-endpoints": "example.org:443",
            },
            _Selection.PRESET,
        ),
        ({"allowed-endpoints-mode": "append"}, _Selection.DEFAULT),
        ({"egress-preset": "pypi", "allowed-endpoints": " \n"}, _Selection.PRESET),
        ({"allowed-endpoints": "pypi.org:443\n"}, _Selection.REPLACEMENT),
        (
            {"allowed-endpoints-mode": "replace", "allowed-endpoints": "pypi.org:443"},
            _Selection.REPLACEMENT,
        ),
    ],
    ids=[
        "omitted",
        "named",
        "append",
        "append-without-list",
        "blank-list",
        "replace-implicit",
        "replace-explicit",
    ],
)
def test_selection_mirrors_the_reusables(
    inputs: dict[str, Any],
    expected: _Selection,
) -> None:
    """Treat an omitted preset, append mode and a replacement list apart."""
    assert_that(_selection(inputs)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("reusable", "inputs"),
    [
        ("reusable-test-python.yml", {"egress-preset": "quality"}),
        ("reusable-publish-quality-summary.yml", {"egress-preset": "github-tooling"}),
        (
            "reusable-publish-security-audit-comment.yml",
            {"egress-preset": "github-tooling"},
        ),
        (
            "reusable-test-python.yml",
            {
                "egress-preset": "quality",
                "allowed-endpoints-mode": "append",
                "allowed-endpoints": "example.org:443",
            },
        ),
        (
            "reusable-release-version-pr.yml",
            {"egress-preset": "pypi", "allowed-endpoints": "pypi.org:443"},
        ),
    ],
    ids=[
        "test-python",
        "quality-summary",
        "security-audit-comment",
        "append",
        "inert-under-replacement",
    ],
)
def test_a_preset_the_reusable_does_not_accept_fails(
    reusable: str,
    inputs: dict[str, Any],
) -> None:
    """Reject the presets lgtm-ci v0.111.0 stopped accepting from winnow."""
    with pytest.raises(AssertionError):
        _assert_callers_pass_accepted_presets(_caller(reusable, inputs))


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"egress-preset": "pypi"},
        {
            "egress-preset": "pypi",
            "allowed-endpoints-mode": "append",
            "allowed-endpoints": "example.org:443",
        },
        {"allowed-endpoints": "pypi.org:443"},
        {"egress-preset": "pypi", "allowed-endpoints": "pypi.org:443"},
    ],
    ids=["default", "named", "append", "replacement", "replacement-with-preset"],
)
def test_callers_the_reusable_accepts_pass(inputs: dict[str, Any]) -> None:
    """Pass the default, an accepted preset, and a replacement list."""
    _assert_callers_pass_accepted_presets(_caller("reusable-test-python.yml", inputs))


def test_an_unknown_reusable_fails() -> None:
    """Make the table grow with every reusable winnow starts calling."""
    with pytest.raises(AssertionError):
        _assert_callers_pass_accepted_presets(
            _caller("reusable-not-in-the-table.yml", {}),
        )


def test_a_differently_cased_owner_is_still_checked() -> None:
    """Match the owner and repository case-insensitively, as GitHub does."""
    job = {
        "uses": "LGTM-HQ/LGTM-CI/.github/workflows/reusable-test-python.yml@main",
        "with": {"egress-preset": "quality"},
    }
    workflows = {"peer.yml": {"permissions": {}, "jobs": {"test": job}}}
    with pytest.raises(AssertionError):
        _assert_callers_pass_accepted_presets(workflows)


def test_jobs_that_call_no_lgtm_ci_reusable_are_ignored() -> None:
    """Leave plain jobs and other owners' reusables alone."""
    workflows = {
        "peer.yml": {
            "permissions": {},
            "jobs": {
                "local": {"runs-on": "ubuntu-latest", "steps": []},
                "other": {
                    "uses": "octo/other/.github/workflows/x.yml@" + "a" * 40,
                    "with": {"egress-preset": "anything"},
                },
            },
        }
    }
    assert_that(_callers(workflows)).is_empty()
    _assert_callers_pass_accepted_presets(workflows)
