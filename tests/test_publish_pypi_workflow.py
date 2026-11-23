"""Keep the PyPI upload behind its attestation gates, and nothing after it.

The build reusable attests every distribution file, and ``prepare-pypi-upload``
verifies those attestations before it hands the upload a path. That is the
release's provenance. A second, best-effort attestation after the upload
(#357) added nothing that could stop a bad release and needed
``attestations: write`` on the job that holds the PyPI token, so it is gone,
along with the TestPyPI staging workflow that never ran.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml
from assertpy import assert_that

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_PUBLISH = _WORKFLOWS / "publish-pypi-on-tag.yml"
_PYPI_PUBLISH = "pypa/gh-action-pypi-publish@"
_PREPARE = "lgtm-hq/lgtm-ci/.github/actions/prepare-pypi-upload@"
_SIGNER = "lgtm-hq/lgtm-ci/.github/workflows/reusable-build-python-dist.yml"
_FINAL_RECHECK = "Recheck harden-runner agent"


def _publish() -> dict[str, Any]:
    """Parse the tag publish workflow.

    Returns:
        The parsed workflow.
    """
    return cast(dict[str, Any], yaml.safe_load(_PUBLISH.read_text()))


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


def test_only_the_final_recheck_follows_the_upload() -> None:
    """No attestation or other step runs after the irreversible upload."""
    steps = _publish()["jobs"]["pypi-upload"]["steps"]
    uploads = [i for i, step in enumerate(steps) if _uses(step, _PYPI_PUBLISH)]
    assert_that(uploads).is_length(1)
    after = [step.get("name") for step in steps[uploads[0] + 1 :]]
    assert_that(after).is_equal_to([_FINAL_RECHECK])


def test_the_upload_job_holds_only_read_and_oidc() -> None:
    """The job that publishes may read the artifact and mint its OIDC token."""
    job = _publish()["jobs"]["pypi-upload"]
    assert_that(job["permissions"]).is_equal_to(
        {"contents": "read", "id-token": "write"},
    )
    assert_that(job["environment"]).is_equal_to("pypi")


def test_every_prepared_upload_verifies_the_build_attestation() -> None:
    """The smoke test and the upload both check the build's attestation."""
    jobs = _publish()["jobs"]
    for job_id in ("pypi-smoke-test", "pypi-upload"):
        prepares = [step for step in jobs[job_id]["steps"] if _uses(step, _PREPARE)]
        assert_that(prepares).described_as(job_id).is_length(1)
        inputs = prepares[0].get("with") or {}
        assert_that(inputs.get("signer-workflow")).described_as(job_id).is_equal_to(
            _SIGNER,
        )
        # Unset means the action's default, which is "true".
        assert_that(str(inputs.get("require-attestation", "true"))).described_as(
            job_id,
        ).is_equal_to("true")


def test_the_build_still_attests_the_distribution() -> None:
    """The build reusable keeps the scopes it signs the attestation with."""
    permissions = _publish()["jobs"]["pypi-build"]["permissions"]
    assert_that(permissions).contains_entry(
        {"id-token": "write"},
        {"attestations": "write"},
    )


def test_no_testpypi_workflow_remains() -> None:
    """The unused TestPyPI staging lane is removed (#357)."""
    assert_that(str(_WORKFLOWS / "publish-testpypi.yml")).does_not_exist()
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        text = path.read_text()
        assert_that(text).described_as(path.name).does_not_contain(
            "test.pypi.org/legacy",
        )
        assert_that(text.lower()).described_as(path.name).does_not_contain(
            "publish-testpypi",
        )
