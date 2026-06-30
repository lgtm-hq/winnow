"""Duplicate detection domain models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from winnow.models.media import MediaType


class QualityScore(BaseModel):
    """Quality ranking attributes for a media file."""

    model_config = ConfigDict(validate_assignment=True)

    composite_score: float = Field(ge=0)
    resolution: int = Field(ge=0)
    quality_metric: float = Field(ge=0)
    file_size: int = Field(ge=0)
    creation_date: datetime | None = None
    image_format: str | None = None
    color_mode: str | None = None
    bit_depth: int | None = Field(default=None, ge=0)
    has_alpha: bool | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)


class DuplicatePair(BaseModel):
    """Pair of files identified as duplicates of each other."""

    model_config = ConfigDict(validate_assignment=True)

    path_a: Path
    path_b: Path
    similarity: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("path_a", "path_b", mode="after")
    @classmethod
    def path_must_differ_from_sibling(
        cls,
        value: Path,
        info: ValidationInfo,
    ) -> Path:
        """Ensure the assigned path differs from the sibling path.

        Args:
            value: Path value being validated.
            info: Validation context from Pydantic.

        Returns:
            The validated path.

        Raises:
            ValueError: If the path matches the sibling path.
        """
        sibling_field = "path_b" if info.field_name == "path_a" else "path_a"
        sibling = info.data.get(sibling_field)
        if sibling is not None and value == sibling:
            msg = "Duplicate pair paths must differ"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def paths_must_differ(self) -> DuplicatePair:
        """Ensure the two paired paths are not identical.

        Returns:
            Validated duplicate pair.

        Raises:
            ValueError: If both paths refer to the same file.
        """
        if self.path_a == self.path_b:
            msg = "Duplicate pair paths must differ"
            raise ValueError(msg)
        return self


class DuplicateGroup(BaseModel):
    """Container for duplicates of the same media item."""

    model_config = ConfigDict(validate_assignment=True)

    group_number: int = Field(ge=1)
    media_type: MediaType
    files: list[Path] = Field(default_factory=list)
    pairs: list[DuplicatePair] = Field(default_factory=list)
    target_path: Path | None = None
    max_depth: int = Field(default=0, ge=0)

    def add_file(self, path: Path, *, depth: int = 0) -> None:
        """Add a duplicate file path to the group.

        Args:
            path: File path to append.
            depth: Directory depth relative to the scan root.
        """
        if path not in self.files:
            self.files.append(path)
        self.max_depth = max(self.max_depth, depth)

    def add_pair(self, pair: DuplicatePair) -> None:
        """Add a duplicate pair and register both paths in the group.

        Args:
            pair: Duplicate pair to append.
        """
        self.pairs.append(pair)
        self.add_file(pair.path_a)
        self.add_file(pair.path_b)

    def to_dict(self) -> dict[str, object]:
        """Serialize the group for reporting.

        Returns:
            Dictionary representation of this group.
        """
        return {
            "media_type": self.media_type.value,
            "group_number": self.group_number,
            "target_path": str(self.target_path) if self.target_path else None,
            "max_depth": self.max_depth,
            "files": [str(path) for path in self.files],
            "pairs": [
                {
                    "path_a": str(pair.path_a),
                    "path_b": str(pair.path_b),
                    "similarity": pair.similarity,
                }
                for pair in self.pairs
            ],
        }
