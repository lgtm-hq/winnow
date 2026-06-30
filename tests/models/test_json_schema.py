"""JSON schema export tests for domain models."""

from __future__ import annotations

from assertpy import assert_that
from pydantic import BaseModel

from winnow.models.config import WinnowConfig
from winnow.models.duplicates import DuplicateGroup, DuplicatePair, QualityScore
from winnow.models.media import MediaFile, MediaMetadata
from winnow.models.pipeline import PipelineResult, RunMetadata

MODEL_CLASSES: list[type[BaseModel]] = [
    MediaMetadata,
    MediaFile,
    QualityScore,
    DuplicatePair,
    DuplicateGroup,
    RunMetadata,
    PipelineResult,
    WinnowConfig,
]


def test_model_json_schema_exports_for_all_models() -> None:
    """Each Pydantic model class exposes a non-empty JSON schema."""
    for model_cls in MODEL_CLASSES:
        schema = model_cls.model_json_schema()
        assert_that(schema).contains_key("title")
        assert_that(schema["title"]).is_not_empty()
