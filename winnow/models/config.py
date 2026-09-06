"""Configuration domain models."""

from __future__ import annotations

import re
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


_MIN_PRINTABLE_CODEPOINT = 0x20
_YEAR_FOLDER_PATTERN = re.compile(r"^\d{4}$")
_ROUTING_FOLDER_FIELDS: tuple[str, ...] = (
    "screenshots",
    "graphics",
    "live_photos",
    "review",
    "duplicates",
)


class RoutingSettings(BaseModel):
    """Special-folder names and thresholds used when routing classified media.

    Folder names are directory prefixes under the organize destination root.
    ``duplicates`` is a name only; the dedup step owns that tree.
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    enabled: bool = True
    screenshots: str = "Screenshots"
    graphics: str = "Graphics"
    live_photos: str = "LivePhotos"
    review: str = "Review"
    duplicates: str = "Duplicates"
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    keep_dated_layout: bool = True

    @model_validator(mode="after")
    def _validate_folder_names(self) -> Self:
        """Reject unsafe, year-like, or duplicated folder names.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If a folder name is empty, padded, contains a path
                separator or a control character, is ``.``/``..``, matches a
                four-digit year, or is shared with another category
                case-insensitively.
        """
        seen: dict[str, str] = {}
        for field_name in _ROUTING_FOLDER_FIELDS:
            value: str = getattr(self, field_name)
            _check_folder_name(field_name=field_name, value=value)
            key = value.casefold()
            if key in seen:
                raise ValueError(
                    f"routing.{field_name} duplicates routing.{seen[key]}: {value!r}",
                )
            seen[key] = field_name
        return self


def _check_folder_name(*, field_name: str, value: str) -> None:
    """Validate a single routing folder name.

    Args:
        field_name: Name of the ``RoutingSettings`` field being checked.
        value: Folder name to validate.

    Raises:
        ValueError: If the name is not a safe single path component.
    """
    if not value or value != value.strip():
        raise ValueError(f"routing.{field_name} must be a non-empty, unpadded name")
    if "/" in value or "\\" in value:
        raise ValueError(f"routing.{field_name} must not contain path separators")
    if any(ord(char) < _MIN_PRINTABLE_CODEPOINT for char in value):
        raise ValueError(f"routing.{field_name} must not contain control characters")
    if value in {".", ".."}:
        raise ValueError(f"routing.{field_name} must not be '.' or '..'")
    if _YEAR_FOLDER_PATTERN.match(value):
        raise ValueError(f"routing.{field_name} must not look like a year folder")


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
    routing: RoutingSettings = Field(default_factory=RoutingSettings)

    @model_validator(mode="after")
    def _reject_conflicting_symlink_settings(self) -> Self:
        """Reject contradictory legacy and policy symlink settings."""
        follows_by_policy = self.symlink_policy is SymlinkPolicy.FOLLOW
        if self.follow_symlinks != follows_by_policy:
            raise ValueError("follow_symlinks conflicts with symlink_policy")
        return self
