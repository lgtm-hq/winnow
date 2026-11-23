"""Stop a directly hardened job when its network hardening is not enforcing.

A job that calls ``step-security/harden-runner`` itself, instead of through an
lgtm-ci reusable, gets none of the reusables' checks. harden-runner logs a
failed agent start and still exits 0, so such a job used to run with no egress
enforcement: the 0.37.7 PyPI upload did (#432). Every such job therefore
carries lgtm-ci's generated steps, copied from
``examples/publish-python-release.yml`` at the pinned lgtm-ci commit:

- "Verify harden-runner agent" right after harden-runner, before any step
  that can use a credential;
- "Recheck harden-runner agent before <step>" right before each irreversible
  publish or dispatch step;
- "Recheck harden-runner agent" as the job's last step, which fails the job
  if the agent stopped enforcing mid-job.

The steps run before any checkout, so their shell is inline. Their text must
stay lgtm-ci's: the tests compare each ``run`` with a SHA-256 of the copy in
lgtm-ci, so they stay offline.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_HARDEN_RUNNER = "step-security/harden-runner@"
# Steps after which nothing can be taken back: each needs a recheck right
# before it.
_IRREVERSIBLE = (
    "pypa/gh-action-pypi-publish@",
    "lgtm-hq/lgtm-ci/.github/actions/trigger-homebrew-update@",
)
# The jobs that call harden-runner directly today. The checks below cover any
# job that does; this set only keeps them from passing on nothing.
_DIRECT_JOBS = {
    ("ghcr-cleanup.yml", "check-package"),
    ("publish-pypi-on-tag.yml", "homebrew-dispatch"),
    ("publish-pypi-on-tag.yml", "pypi-smoke-test"),
    ("publish-pypi-on-tag.yml", "pypi-upload"),
}
_VERIFY_ID = "verify-harden-runner"
_VERIFY_NAME = "Verify harden-runner agent"
_RECHECK_NAME = "Recheck harden-runner agent"
_FINAL_IF = "${{ !cancelled() && steps.verify-harden-runner.outcome == 'success' }}"
_BEFORE_IF = "${{ success() && steps.verify-harden-runner.outcome == 'success' }}"
# The variable stays unset (#432): the standard agent installs on these
# runners, so the eBPF fallback agent is refused. The steps still read it, as
# lgtm-ci's copies do, and the recheck fails closed without the agent output.
_EBPF_OPT_IN = "${{ vars.LGTM_CI_ALLOW_HARDEN_RUNNER_EBPF_AGENT }}"
_VERIFY_ENV = {
    "EGRESS_POLICY": "block",
    "REPOSITORY_PRIVATE": "${{ github.event.repository.private }}",
    "LGTM_CI_ALLOW_HARDEN_RUNNER_EBPF_AGENT": _EBPF_OPT_IN,
}
_RECHECK_ENV = {
    "LGTM_CI_ALLOW_HARDEN_RUNNER_EBPF_AGENT": _EBPF_OPT_IN,
    "HARDEN_RUNNER_AGENT": "${{ steps.verify-harden-runner.outputs.agent }}",
}
# SHA-256 of the ``run`` text (as YAML parses it) of the "Verify
# harden-runner agent" and "Recheck harden-runner agent" steps of the
# pypi-upload job in lgtm-ci's examples/publish-python-release.yml at
# 0c373f1252e0fe23a1e119df639b51d63b02b7d8 (v0.116.3). lgtm-ci generates them
# from scripts/ci/bootstrap/verify-harden-runner-agent.sh and
# recheck-harden-runner-agent.sh. When the lgtm-ci pin moves, copy the steps
# again and recompute both from that example.
_VERIFY_SHA256 = "cc86b7cfcbbbc717b1c8fb6e28b8d97db64a5338d2631f9deb4692be8a218545"
_RECHECK_SHA256 = "336ab9530e5519cfd601c43acf431a14121bae5ec02e8773d1ca081ce707c09b"


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


def _uses(step: dict[str, Any], prefix: str) -> bool:
    """Tell whether a step runs an action whose reference starts with a prefix.

    Args:
        step: One parsed workflow step.
        prefix: Action reference prefix, ending in ``@``.

    Returns:
        True when the step's ``uses`` starts with the prefix, ignoring case.
    """
    uses = step.get("uses")
    return isinstance(uses, str) and uses.strip().lower().startswith(prefix.lower())


def _direct_jobs(
    workflows: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Return the steps of every job that calls harden-runner directly.

    Args:
        workflows: Parsed workflows keyed by file name.

    Returns:
        Each such job's steps, keyed by file name and job id.
    """
    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for name, workflow in workflows.items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            steps = (job or {}).get("steps") or []
            if any(_uses(step, _HARDEN_RUNNER) for step in steps):
                found[(name, job_id)] = steps
    return found


def _sha256(text: Any) -> str:
    """Hash a step's ``run`` text.

    Args:
        text: The value of ``run``; anything but a string hashes as empty.

    Returns:
        The hex SHA-256 digest.
    """
    data = text if isinstance(text, str) else ""
    return hashlib.sha256(data.encode()).hexdigest()


def _assert_recheck(
    step: dict[str, Any], name: str, condition: str, where: str
) -> None:
    """Require one recheck step, exactly as lgtm-ci generates it.

    Args:
        step: The step that must be the recheck.
        name: Its expected name.
        condition: Its expected ``if``.
        where: Label for failure messages.
    """
    assert_that(step.get("name")).described_as(f"{where} name").is_equal_to(name)
    assert_that(step.get("if")).described_as(f"{where} if").is_equal_to(condition)
    assert_that(step.get("timeout-minutes")).described_as(where).is_equal_to(1)
    assert_that(step.get("working-directory")).described_as(where).is_equal_to(
        "${{ runner.temp }}",
    )
    assert_that(step.get("shell")).described_as(where).is_equal_to("bash")
    assert_that(step.get("env")).described_as(f"{where} env").is_equal_to(
        _RECHECK_ENV,
    )
    assert_that(_sha256(step.get("run"))).described_as(
        f"{where} run differs from lgtm-ci's recheck-harden-runner-agent",
    ).is_equal_to(_RECHECK_SHA256)
    assert_that(set(step)).described_as(f"{where} keys").is_equal_to(
        {"name", "if", "timeout-minutes", "working-directory", "shell", "env", "run"},
    )


def _assert_direct_job(steps: list[dict[str, Any]], where: str) -> None:
    """Require the verify step, a recheck before each publish, and a final one.

    Args:
        steps: The job's steps.
        where: Label for failure messages.
    """
    hardens = [i for i, step in enumerate(steps) if _uses(step, _HARDEN_RUNNER)]
    # First, so no step runs before the agent; once, so one verify covers it.
    assert_that(hardens).described_as(f"{where} harden-runner steps").is_equal_to([0])
    policy = (steps[0].get("with") or {}).get("egress-policy")
    assert_that(policy).described_as(f"{where} egress-policy").is_equal_to("block")

    assert_that(len(steps)).described_as(f"{where} step count").is_greater_than(2)
    verify = steps[1]
    assert_that(verify.get("name")).described_as(where).is_equal_to(_VERIFY_NAME)
    assert_that(verify.get("id")).described_as(where).is_equal_to(_VERIFY_ID)
    assert_that(verify.get("shell")).described_as(where).is_equal_to("bash")
    assert_that(verify.get("env")).described_as(f"{where} verify env").is_equal_to(
        _VERIFY_ENV,
    )
    assert_that(_sha256(verify.get("run"))).described_as(
        f"{where} run differs from lgtm-ci's verify-harden-runner-agent",
    ).is_equal_to(_VERIFY_SHA256)
    assert_that(set(verify)).described_as(f"{where} verify keys").is_equal_to(
        {"name", "id", "shell", "env", "run"},
    )

    _assert_recheck(steps[-1], _RECHECK_NAME, _FINAL_IF, f"{where} last step")

    # Every other recheck sits right before an irreversible step, and every
    # irreversible step has one.
    rechecks = {
        index
        for index, step in enumerate(steps[:-1])
        if str(step.get("name", "")).startswith(_RECHECK_NAME)
        or _sha256(step.get("run")) == _RECHECK_SHA256
    }
    guarded = set()
    for index, step in enumerate(steps):
        if any(_uses(step, prefix) for prefix in _IRREVERSIBLE):
            label = f"{where} step {index} ({step.get('name')})"
            assert_that(index).described_as(label).is_greater_than(2)
            before = steps[index - 1]
            _assert_recheck(
                before,
                f"{_RECHECK_NAME} before {step.get('name')}",
                _BEFORE_IF,
                f"{label} recheck",
            )
            guarded.add(index - 1)
    assert_that(rechecks).described_as(f"{where} stray rechecks").is_equal_to(guarded)


def _assert_direct_jobs(workflows: dict[str, dict[str, Any]]) -> None:
    """Check every job that calls harden-runner directly.

    Args:
        workflows: Parsed workflows keyed by file name.
    """
    jobs = _direct_jobs(workflows)
    assert_that(set(jobs)).contains(*_DIRECT_JOBS)
    for (name, job_id), steps in jobs.items():
        _assert_direct_job(steps, f"{name}:{job_id}")


def _job_steps(
    workflows: dict[str, dict[str, Any]],
    name: str,
    job_id: str,
) -> list[dict[str, Any]]:
    """Return one job's step list, for mutation in place.

    Args:
        workflows: Parsed workflows keyed by file name.
        name: Workflow file name.
        job_id: Job id.

    Returns:
        The job's steps.
    """
    return cast(list[dict[str, Any]], workflows[name]["jobs"][job_id]["steps"])


def _index(steps: list[dict[str, Any]], name: str) -> int:
    """Find a step by its name.

    Args:
        steps: A job's steps.
        name: The step name.

    Returns:
        The step's index.
    """
    return next(i for i, step in enumerate(steps) if step.get("name") == name)


def test_every_direct_job_verifies_and_rechecks_its_agent() -> None:
    """Each direct harden-runner job carries lgtm-ci's verify and rechecks."""
    _assert_direct_jobs(_workflows())


def test_each_irreversible_step_is_found_in_a_direct_job() -> None:
    """Guard the recheck rule: both kinds of irreversible step are seen."""
    jobs = _direct_jobs(_workflows())
    found = {
        prefix
        for steps in jobs.values()
        for step in steps
        for prefix in _IRREVERSIBLE
        if _uses(step, prefix)
    }
    assert_that(found).is_equal_to(set(_IRREVERSIBLE))


@pytest.mark.parametrize(("name", "job_id"), sorted(_DIRECT_JOBS))
def test_a_job_without_its_verify_step_fails(name: str, job_id: str) -> None:
    """Removing the verify step from any one direct job fails the check.

    Args:
        name: Workflow file name.
        job_id: Job id.
    """
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, name, job_id)
    del steps[_index(steps, _VERIFY_NAME)]
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


@pytest.mark.parametrize(("name", "job_id"), sorted(_DIRECT_JOBS))
def test_a_job_without_its_final_recheck_fails(name: str, job_id: str) -> None:
    """Removing the last recheck from any one direct job fails the check.

    Args:
        name: Workflow file name.
        job_id: Job id.
    """
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, name, job_id)
    del steps[_index(steps, _RECHECK_NAME)]
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


@pytest.mark.parametrize(
    ("job_id", "step_name"),
    [
        ("pypi-upload", "Upload to PyPI"),
        ("homebrew-dispatch", "Dispatch formula update"),
    ],
)
def test_a_publish_without_its_recheck_fails(job_id: str, step_name: str) -> None:
    """Removing the recheck before a publish or dispatch fails the check.

    Args:
        job_id: Job id in the tag publish workflow.
        step_name: The irreversible step the recheck guards.
    """
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, "publish-pypi-on-tag.yml", job_id)
    del steps[_index(steps, f"{_RECHECK_NAME} before {step_name}")]
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


def _set(step: dict[str, Any], key: str, value: Any) -> None:
    """Set one key of a step.

    Args:
        step: The step to change.
        key: Key to set.
        value: New value.
    """
    step[key] = value


@pytest.mark.parametrize(
    ("step_name", "change"),
    [
        (_VERIFY_NAME, lambda s: _set(s, "run", s["run"].replace("exit 1", "true"))),
        (_VERIFY_NAME, lambda s: _set(s, "id", "verify")),
        (_VERIFY_NAME, lambda s: _set(s, "continue-on-error", True)),
        (_VERIFY_NAME, lambda s: _set(s, "if", "${{ false }}")),
        (
            _VERIFY_NAME,
            lambda s: s["env"].pop("LGTM_CI_ALLOW_HARDEN_RUNNER_EBPF_AGENT"),
        ),
        (_VERIFY_NAME, lambda s: _set(s["env"], "EGRESS_POLICY", "audit")),
        (_RECHECK_NAME, lambda s: _set(s, "if", _BEFORE_IF)),
        (_RECHECK_NAME, lambda s: _set(s, "run", s["run"] + "true\n")),
        (_RECHECK_NAME, lambda s: s["env"].pop("HARDEN_RUNNER_AGENT")),
        (_RECHECK_NAME, lambda s: _set(s, "continue-on-error", True)),
        (
            f"{_RECHECK_NAME} before Upload to PyPI",
            lambda s: _set(s, "if", _FINAL_IF),
        ),
        (
            f"{_RECHECK_NAME} before Upload to PyPI",
            lambda s: _set(s, "name", f"{_RECHECK_NAME} before upload"),
        ),
    ],
    ids=[
        "verify-run-edited",
        "verify-id",
        "verify-continue-on-error",
        "verify-skipped",
        "verify-env-missing",
        "verify-audit",
        "final-if",
        "final-run-edited",
        "final-env-missing",
        "final-continue-on-error",
        "before-if",
        "before-name",
    ],
)
def test_an_altered_step_fails(step_name: str, change: Any) -> None:
    """A step that no longer matches lgtm-ci's copy fails the check.

    Args:
        step_name: The pypi-upload step to change.
        change: Function that changes it in place.
    """
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, "publish-pypi-on-tag.yml", "pypi-upload")
    change(steps[_index(steps, step_name)])
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


def test_a_step_before_the_verify_step_fails() -> None:
    """Nothing may run between harden-runner and the verify step."""
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, "publish-pypi-on-tag.yml", "pypi-smoke-test")
    checkout = steps.pop(_index(steps, "Checkout repository"))
    steps.insert(1, checkout)
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


def test_a_step_after_the_final_recheck_fails() -> None:
    """The final recheck must be the job's last step."""
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, "publish-pypi-on-tag.yml", "homebrew-dispatch")
    steps.append({"name": "Late step", "run": "true"})
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


def test_a_new_direct_job_without_the_steps_fails() -> None:
    """A job that adds harden-runner later is held to the same rule."""
    workflows = copy.deepcopy(_workflows())
    workflows["peer.yml"] = {
        "jobs": {
            "peer": {
                "steps": [
                    {
                        "uses": "Step-Security/Harden-Runner@" + "a" * 40,
                        "with": {"egress-policy": "block"},
                    },
                    {"run": "true"},
                ],
            },
        },
    }
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)


def test_a_job_hardened_twice_fails() -> None:
    """A second harden-runner step would start an agent nothing verified."""
    workflows = copy.deepcopy(_workflows())
    steps = _job_steps(workflows, "ghcr-cleanup.yml", "check-package")
    steps.insert(3, copy.deepcopy(steps[0]))
    with pytest.raises(AssertionError):
        _assert_direct_jobs(workflows)
