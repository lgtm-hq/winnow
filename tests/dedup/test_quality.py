"""Tests for the duplicate quality comparator."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.dedup import quality
from winnow.dedup.quality import (
    QualityComparator,
    QualityWeights,
    laplacian_sharpness,
)
from winnow.exceptions import DuplicateError
from winnow.models.duplicates import DuplicateGroup
from winnow.models.media import MediaFile, MediaMetadata, MediaType

_CREATED = datetime(2021, 6, 1, 12, 0, 0)


def _media(
    path: str,
    *,
    width: int | None = None,
    height: int | None = None,
    size_bytes: int = 0,
    bitrate: int | None = None,
    media_type: MediaType = MediaType.IMAGE,
    image_format: str | None = None,
) -> MediaFile:
    """Build a ``MediaFile`` for comparator tests.

    Args:
        path: File path string.
        width: Optional pixel width.
        height: Optional pixel height.
        size_bytes: File size in bytes.
        bitrate: Optional media bitrate.
        media_type: Media type of the file.
        image_format: Optional image format label.

    Returns:
        A media file with the requested metadata.
    """
    return MediaFile(
        path=Path(path),
        media_type=media_type,
        creation_date=_CREATED,
        extension=Path(path).suffix.lstrip("."),
        size_bytes=size_bytes,
        metadata=MediaMetadata(
            width=width,
            height=height,
            bitrate=bitrate,
            image_format=image_format,
        ),
    )


def test_rank_orders_higher_quality_first() -> None:
    """A larger, higher-resolution copy ranks ahead of a smaller one."""
    comparator = QualityComparator(compute_sharpness=False)

    ranked = comparator.rank(
        [
            _media("/low.jpg", width=100, height=100, size_bytes=1_000),
            _media("/high.jpg", width=1_000, height=1_000, size_bytes=50_000),
        ],
    )

    assert_that([item.media.path for item in ranked]).is_equal_to(
        [Path("/high.jpg"), Path("/low.jpg")],
    )
    assert_that([item.rank for item in ranked]).is_equal_to([1, 2])


def test_weights_select_the_favored_dimension() -> None:
    """Weighting a single dimension makes it decide the ranking."""
    sharp_resolution = _media("/res.jpg", width=4_000, height=3_000, size_bytes=10)
    big_file = _media("/size.jpg", width=10, height=10, size_bytes=9_000_000)

    resolution_first = QualityComparator(
        weights=QualityWeights(resolution=1, file_size=0, sharpness=0, bitrate=0),
        compute_sharpness=False,
    ).select_best([sharp_resolution, big_file])
    size_first = QualityComparator(
        weights=QualityWeights(resolution=0, file_size=1, sharpness=0, bitrate=0),
        compute_sharpness=False,
    ).select_best([sharp_resolution, big_file])

    assert_that(resolution_first.path).is_equal_to(Path("/res.jpg"))
    assert_that(size_first.path).is_equal_to(Path("/size.jpg"))


def test_composite_score_is_normalized_between_zero_and_one() -> None:
    """The best copy scores 1.0 when it maximizes every weighted dimension."""
    comparator = QualityComparator(
        weights=QualityWeights(resolution=1, file_size=1, sharpness=0, bitrate=1),
        compute_sharpness=False,
    )

    ranked = comparator.rank(
        [
            _media("/best.jpg", width=100, height=100, size_bytes=100, bitrate=100),
            _media("/worst.jpg", width=10, height=10, size_bytes=10, bitrate=10),
        ],
    )

    assert_that(ranked[0].score.composite_score).is_equal_to(1.0)
    assert_that(ranked[1].score.composite_score).is_greater_than(0.0)
    assert_that(ranked[1].score.composite_score).is_less_than(1.0)


def test_score_populates_quality_fields_from_metadata() -> None:
    """A computed score mirrors resolution and metadata from the media file."""
    comparator = QualityComparator(compute_sharpness=False)

    ranked = comparator.rank(
        [_media("/only.jpg", width=800, height=600, image_format="JPEG")],
    )
    score = ranked[0].score

    assert_that(score.resolution).is_equal_to(480_000)
    assert_that(score.width).is_equal_to(800)
    assert_that(score.height).is_equal_to(600)
    assert_that(score.image_format).is_equal_to("JPEG")
    assert_that(score.creation_date).is_equal_to(_CREATED)


def test_injected_sharpness_provider_influences_ranking() -> None:
    """A sharper image outranks a blurrier one when only sharpness is weighted."""
    sharpness_by_path = {Path("/sharp.jpg"): 900.0, Path("/blurry.jpg"): 5.0}
    comparator = QualityComparator(
        weights=QualityWeights(resolution=0, file_size=0, sharpness=1, bitrate=0),
        sharpness_provider=lambda path: sharpness_by_path[path],
    )

    best = comparator.select_best(
        [
            _media("/sharp.jpg", width=100, height=100),
            _media("/blurry.jpg", width=100, height=100),
        ],
    )

    assert_that(best.path).is_equal_to(Path("/sharp.jpg"))


def test_sharpness_ignored_for_non_image_media() -> None:
    """Non-image media never invoke the sharpness provider."""

    def _fail(path: Path) -> float | None:
        raise AssertionError(f"sharpness should not be measured for {path}")

    comparator = QualityComparator(sharpness_provider=_fail)

    ranked = comparator.rank(
        [_media("/clip.mp4", size_bytes=10, media_type=MediaType.VIDEO)],
    )

    assert_that(ranked[0].score.quality_metric).is_equal_to(0.0)


def test_rank_group_sets_target_path_and_ranks_members() -> None:
    """Ranking a group marks the best copy as the target path."""
    comparator = QualityComparator(compute_sharpness=False)
    low = _media("/low.jpg", width=100, height=100, size_bytes=1_000)
    high = _media("/high.jpg", width=900, height=900, size_bytes=90_000)
    group = DuplicateGroup(group_number=1, media_type=MediaType.IMAGE)
    group.add_file(low.path)
    group.add_file(high.path)

    ranked = comparator.rank_group(
        group,
        {low.path: low, high.path: high},
    )

    assert_that(group.target_path).is_equal_to(Path("/high.jpg"))
    assert_that(ranked[0].media.path).is_equal_to(Path("/high.jpg"))


def test_rank_group_can_skip_target_path_assignment() -> None:
    """Target-path assignment is optional."""
    comparator = QualityComparator(compute_sharpness=False)
    media = _media("/a.jpg", width=10, height=10)
    other = _media("/b.jpg", width=20, height=20)
    group = DuplicateGroup(group_number=1, media_type=MediaType.IMAGE)
    group.add_file(media.path)
    group.add_file(other.path)

    comparator.rank_group(
        group,
        {media.path: media, other.path: other},
        set_target_path=False,
    )

    assert_that(group.target_path).is_none()


def test_rank_group_rejects_missing_media() -> None:
    """A group member without a media file raises an error."""
    comparator = QualityComparator(compute_sharpness=False)
    group = DuplicateGroup(group_number=1, media_type=MediaType.IMAGE)
    group.add_file(Path("/missing.jpg"))
    group.add_file(Path("/present.jpg"))
    present = _media("/present.jpg", width=10, height=10)

    with pytest.raises(DuplicateError, match="missing media file"):
        comparator.rank_group(group, {present.path: present})


def test_rank_group_rejects_empty_group() -> None:
    """An empty duplicate group cannot be ranked."""
    comparator = QualityComparator(compute_sharpness=False)
    group = DuplicateGroup(group_number=1, media_type=MediaType.IMAGE)

    with pytest.raises(DuplicateError, match="empty duplicate group"):
        comparator.rank_group(group, {})


def test_rank_rejects_empty_input() -> None:
    """Ranking an empty set of media files raises an error."""
    comparator = QualityComparator(compute_sharpness=False)

    with pytest.raises(DuplicateError, match="empty set"):
        comparator.rank([])


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"resolution": -1.0}, "non-negative"),
        (
            {"resolution": 0.0, "file_size": 0.0, "sharpness": 0.0, "bitrate": 0.0},
            "must be positive",
        ),
    ],
    ids=["negative", "all_zero"],
)
def test_invalid_weights_are_rejected(
    kwargs: dict[str, float],
    match: str,
) -> None:
    """Weight validation happens on construction of ``QualityWeights``."""
    with pytest.raises(DuplicateError, match=match):
        QualityWeights(**kwargs)


def test_laplacian_sharpness_returns_none_without_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharpness degrades to ``None`` when Pillow cannot be imported."""
    monkeypatch.setattr(quality, "_import_pillow", lambda: None)

    assert_that(laplacian_sharpness(Path("/whatever.jpg"))).is_none()


def test_laplacian_sharpness_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    """A non-image file yields ``None`` rather than raising."""
    not_an_image = tmp_path / "note.txt"
    not_an_image.write_text("not an image", encoding="utf-8")

    assert_that(laplacian_sharpness(not_an_image)).is_none()


def test_laplacian_sharpness_ranks_detailed_images_above_flat_ones(
    tmp_path: Path,
) -> None:
    """A high-frequency image measures sharper than a uniform one."""
    pillow = pytest.importorskip("PIL.Image")

    flat = tmp_path / "flat.png"
    pillow.new("RGB", (64, 64), color=(128, 128, 128)).save(flat)

    detailed = tmp_path / "detailed.png"
    checker = pillow.new("RGB", (64, 64))
    pixels = checker.load()
    for row in range(64):
        for column in range(64):
            shade = 255 if (row + column) % 2 == 0 else 0
            pixels[column, row] = (shade, shade, shade)
    checker.save(detailed)

    flat_sharpness = laplacian_sharpness(flat)
    detailed_sharpness = laplacian_sharpness(detailed)

    assert_that(flat_sharpness).is_equal_to(0.0)
    assert_that(detailed_sharpness).is_not_none()
    assert_that(detailed_sharpness).is_greater_than(flat_sharpness)
