"""Pipeline execution domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, auto
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from winnow.models.duplicates import DuplicateGroup


class PipelineStep(StrEnum):
    """Discrete steps executed during a winnow pipeline run."""

    DISCOVERY = auto()
    METADATA = auto()
    SCAN = auto()
    DEDUPLICATION = auto()
    REPORTING = auto()


class RunMetadata(BaseModel):
    """Metadata describing a single pipeline run."""

    model_config = ConfigDict(validate_assignment=True)

    started_at: datetime
    completed_at: datetime | None = None
    winnow_version: str
    source_roots: list[Path] = Field(default_factory=list)
    config_path: Path | None = None

    @model_validator(mode="after")
    def completed_at_is_not_before_started_at(self) -> RunMetadata:
        """Ensure completed_at is not earlier than started_at.

        Returns:
            Validated run metadata.

        Raises:
            ValueError: If completed_at precedes started_at.
        """
        if self.completed_at is None:
            return self
        started_aware = self.started_at.tzinfo is not None
        completed_aware = self.completed_at.tzinfo is not None
        if started_aware != completed_aware:
            msg = (
                "started_at and completed_at must both be timezone-aware "
                "or both be naive"
            )
            raise ValueError(msg)
        if self.completed_at < self.started_at:
            msg = "completed_at cannot be earlier than started_at"
            raise ValueError(msg)
        return self

    @property
    def elapsed_seconds(self) -> float | None:
        """Return elapsed runtime in seconds when the run has completed.

        Returns:
            Elapsed seconds, or None if the run has not completed.
        """
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


class PipelineResult(BaseModel):
    """Aggregate result of a completed or in-progress pipeline run."""

    model_config = ConfigDict(validate_assignment=True)

    run: RunMetadata
    steps_completed: list[PipelineStep] = Field(default_factory=list)
    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)
    total_files_scanned: int = Field(default=0, ge=0)
    duplicate_files_found: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def duplicate_counts_are_consistent(self) -> PipelineResult:
        """Ensure duplicate file count is not less than group count.

        Returns:
            Validated pipeline result.

        Raises:
            ValueError: If duplicate file count is inconsistent.
        """
        if self.duplicate_files_found < len(self.duplicate_groups):
            msg = "duplicate_files_found cannot be less than duplicate group count"
            raise ValueError(msg)
        return self

    @property
    def total_elapsed_seconds(self) -> float | None:
        """Return total elapsed runtime from run metadata.

        Returns:
            Elapsed seconds, or None if the run has not completed.
        """
        return self.run.elapsed_seconds
