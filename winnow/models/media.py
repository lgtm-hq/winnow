"""Media file domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, auto
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class MediaType(StrEnum):
    """Supported media types discovered during scanning."""

    IMAGE = auto()
    VIDEO = auto()
    AUDIO = auto()


class MediaMetadata(BaseModel):
    """Extended metadata extracted from a media file."""

    model_config = ConfigDict(validate_assignment=True)

    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    codec: str | None = None
    bitrate: int | None = Field(default=None, ge=0)
    frame_rate: float | None = Field(default=None, ge=0)
    sample_rate: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    image_format: str | None = None
    color_mode: str | None = None
    bit_depth: int | None = Field(default=None, ge=0)
    has_alpha: bool | None = None


class MediaFile(BaseModel):
    """Represents a discovered media file before organization."""

    model_config = ConfigDict(validate_assignment=True)

    path: Path
    media_type: MediaType
    creation_date: datetime
    extension: str
    size_bytes: int = Field(ge=0)
    metadata: MediaMetadata | None = None
    live_photo_id: str | None = None
