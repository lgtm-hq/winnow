"""Configuration domain models."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from winnow.models.enums import HashAlgorithm, MediaCategory, SortOrder, SymlinkPolicy


class CacheSettings(BaseModel):
    """Cache behavior used by duplicate scanning and metadata extraction."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    enabled: bool = True
    directory: Path = Field(default_factory=lambda: Path.home() / ".cache" / "winnow")
    max_size_mb: int = Field(default=1024, ge=1)
    ttl_seconds: int = Field(default=604_800, ge=0)


class PathSettings(BaseModel):
    """Optional output and filtering paths used by future CLI workflows."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    output_dir: Path | None = None
    quarantine_dir: Path | None = None
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)


class OrganizeSettings(BaseModel):
    """Settings for the organize pipeline (discovery and later steps)."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    max_depth: int | None = Field(default=None, ge=0)


class WinnowConfig(BaseModel):
    """Application configuration loaded from files and environment overrides."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    hash_algorithm: HashAlgorithm = HashAlgorithm.SHA256
    exact_hash_match_threshold: float = Field(default=1.0, ge=0.0, le=1.0)
    perceptual_hash_distance_threshold: int = Field(default=8, ge=0)
    sort_order: SortOrder = SortOrder.BY_QUALITY
    media_categories: list[MediaCategory] = Field(
        default_factory=lambda: [MediaCategory.ALL],
    )
    source_dirs: list[Path] = Field(default_factory=list)
    dry_run: bool = True
    min_similarity: float = Field(default=0.95, ge=0.0, le=1.0)
    keep_highest_quality: bool = True
    recursive: bool = True
    follow_symlinks: bool = False
    symlink_policy: SymlinkPolicy = SymlinkPolicy.SKIP
    workers: int = Field(default=1, ge=1)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    organize: OrganizeSettings = Field(default_factory=OrganizeSettings)

    @model_validator(mode="after")
    def _reject_conflicting_symlink_settings(self) -> Self:
        """Reject contradictory legacy and policy symlink settings."""
        follows_by_policy = self.symlink_policy is SymlinkPolicy.FOLLOW
        if self.follow_symlinks != follows_by_policy:
            raise ValueError("follow_symlinks conflicts with symlink_policy")
        return self
