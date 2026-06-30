"""Configuration domain models."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from winnow.models.enums import HashAlgorithm, MediaCategory, SortOrder


class WinnowConfig(BaseModel):
    """Application configuration stub for future ``.winnow-config.yaml`` support.

    This model captures the expected configuration surface without binding to
    filesystem loading yet. Downstream CLI and API layers will extend it with
    ``pydantic-settings`` integration when configuration loading lands.
    """

    model_config = ConfigDict(validate_assignment=True)

    hash_algorithm: HashAlgorithm = HashAlgorithm.SHA256
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
