"""Tests for pipeline domain models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from assertpy import assert_that
from pydantic import ValidationError

from winnow.models.duplicates import DuplicateGroup
from winnow.models.media import MediaType
from winnow.models.pipeline import PipelineResult, PipelineStep, RunMetadata


def test_run_metadata_elapsed_seconds() -> None:
    """RunMetadata computes elapsed seconds when completed."""
    started = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    completed = started + timedelta(seconds=42)
    run = RunMetadata(
        started_at=started,
        completed_at=completed,
        winnow_version="0.0.3",
        source_roots=[Path("/media")],
    )

    assert_that(run.elapsed_seconds).is_equal_to(42.0)


def test_pipeline_result_validation() -> None:
    """PipelineResult validates duplicate counters and elapsed time."""
    started = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    run = RunMetadata(started_at=started, winnow_version="0.0.3")
    group = DuplicateGroup(group_number=1, media_type=MediaType.IMAGE)
    result = PipelineResult(
        run=run,
        steps_completed=[PipelineStep.DISCOVERY, PipelineStep.SCAN],
        duplicate_groups=[group],
        total_files_scanned=10,
        duplicate_files_found=2,
    )

    assert_that(result.total_elapsed_seconds).is_none()
    assert_that(result.steps_completed[0]).is_equal_to(PipelineStep.DISCOVERY)


def test_run_metadata_rejects_completed_at_before_started_at() -> None:
    """RunMetadata rejects completed_at earlier than started_at."""
    started = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    completed = started - timedelta(seconds=1)

    with pytest.raises(ValidationError, match="completed_at cannot be earlier"):
        RunMetadata(
            started_at=started,
            completed_at=completed,
            winnow_version="0.0.3",
        )


def test_run_metadata_rejects_mismatched_timezone_awareness() -> None:
    """RunMetadata rejects mixed naive and timezone-aware timestamps."""
    started = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    completed = datetime(2024, 6, 1, 12, 1)

    with pytest.raises(ValidationError, match="timezone-aware or both be naive"):
        RunMetadata(
            started_at=started,
            completed_at=completed,
            winnow_version="0.0.3",
        )


def test_pipeline_result_rejects_inconsistent_duplicate_counts() -> None:
    """PipelineResult rejects duplicate_files_found below group count."""
    started = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    run = RunMetadata(started_at=started, winnow_version="0.0.3")
    groups = [
        DuplicateGroup(group_number=1, media_type=MediaType.IMAGE),
        DuplicateGroup(group_number=2, media_type=MediaType.VIDEO),
    ]

    with pytest.raises(ValidationError):
        PipelineResult(
            run=run,
            duplicate_groups=groups,
            duplicate_files_found=1,
        )
