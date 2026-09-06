"""Import-hygiene tests for the pipeline package."""

from __future__ import annotations

import subprocess  # nosec B404 - fixed argv subprocess used only to test imports
import sys

from assertpy import assert_that


def test_pipeline_package_imports_without_circular_imports() -> None:
    """Importing the pipeline package in a fresh interpreter succeeds cleanly.

    Running in a subprocess guarantees no other test's imports mask a circular
    import between ``winnow.pipeline`` and the modules it depends on.
    """
    result = subprocess.run(  # nosec B603 - fixed argv, no shell, trusted interpreter
        [
            sys.executable,
            "-c",
            "import winnow.pipeline; "
            "from winnow.pipeline import Command, MoveFile, PipelineContext, "
            "Step, RunState, StepEvents, NullEvents, StepStarted, StepProgress, "
            "StepCompleted, StepIssue, DiscoveryStep",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert_that(result.returncode).described_as(result.stderr).is_equal_to(0)


def test_pipeline_public_exports_are_accessible() -> None:
    """The package exposes its documented public names."""
    import winnow.pipeline as pipeline

    assert_that(pipeline.__all__).contains(
        "Command",
        "CopyFile",
        "CreateDirectory",
        "DeleteFile",
        "MoveFile",
        "NullEvents",
        "PipelineContext",
        "PipelineEvent",
        "RunState",
        "Step",
        "StepCompleted",
        "StepEvents",
        "StepIssue",
        "StepProgress",
        "StepStarted",
        "DiscoveryStep",
    )
