"""Quality comparison and ranking for duplicate media files.

Scores each candidate copy across four dimensions — pixel resolution, file size,
image sharpness, and bitrate — then ranks copies within a
:class:`~winnow.models.duplicates.DuplicateGroup` to identify the best one to keep.

Dimensions are min-max normalized across the compared set before the configurable
weights are applied, so no single large-magnitude dimension (typically file size)
dominates the composite score.

Sharpness is measured as the variance of a Laplacian-filtered grayscale image and
depends on the optional `Pillow <https://python-pillow.org/>`_ package. When Pillow
is unavailable, or a file cannot be opened as an image, the sharpness dimension
degrades to ``0.0`` rather than raising.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from winnow.exceptions import DuplicateError
from winnow.models.duplicates import DuplicateGroup, QualityScore
from winnow.models.media import MediaFile, MediaMetadata, MediaType

if TYPE_CHECKING:
    from typing import Self

_LAPLACIAN_KERNEL: tuple[int, ...] = (0, 1, 0, 1, -4, 1, 0, 1, 0)


@dataclass(frozen=True, slots=True)
class QualityWeights:
    """Relative weights applied to each quality dimension.

    Attributes:
        resolution: Weight for pixel resolution (``width * height``).
        file_size: Weight for file size in bytes.
        sharpness: Weight for Laplacian-variance sharpness.
        bitrate: Weight for media bitrate.
    """

    resolution: float = 1.0
    file_size: float = 1.0
    sharpness: float = 1.0
    bitrate: float = 1.0

    def __post_init__(self) -> None:
        """Validate that weights are non-negative and not all zero.

        Raises:
            DuplicateError: If any weight is negative or every weight is zero.
        """
        values = (self.resolution, self.file_size, self.sharpness, self.bitrate)
        if any(weight < 0 for weight in values):
            raise DuplicateError(
                "quality weights must be non-negative",
                operation="QualityWeights",
            )
        if not any(values):
            raise DuplicateError(
                "at least one quality weight must be positive",
                operation="QualityWeights",
            )

    def total(self) -> float:
        """Return the sum of all weights.

        Returns:
            Sum of the four dimension weights.
        """
        return self.resolution + self.file_size + self.sharpness + self.bitrate


@dataclass(frozen=True, slots=True)
class _Metrics:
    """Raw, un-normalized quality metrics for a single file."""

    resolution: int
    file_size: int
    sharpness: float
    bitrate: int


@dataclass(frozen=True, slots=True)
class RankedMedia:
    """A media file with its computed quality score and rank.

    Attributes:
        media: The ranked media file.
        score: Quality score with a normalized composite in ``[0, 1]``.
        rank: One-based rank where ``1`` is the best copy.
    """

    media: MediaFile
    score: QualityScore
    rank: int


class SharpnessProvider(Protocol):
    """Callable that returns Laplacian-variance sharpness for an image path."""

    def __call__(self, path: Path) -> float | None:
        """Return the sharpness of the image at ``path``.

        Args:
            path: Filesystem path to an image file.

        Returns:
            Sharpness variance, or ``None`` when it cannot be measured.
        """


class QualityComparator:
    """Score and rank duplicate media copies to select the best one.

    Args:
        weights: Relative weights for each quality dimension.
        compute_sharpness: Whether to measure image sharpness. When ``False`` the
            sharpness dimension is always ``0.0``.
        sharpness_provider: Callable used to measure sharpness. Defaults to the
            Pillow-backed :func:`laplacian_sharpness`.
    """

    __slots__ = ("_sharpness_provider", "compute_sharpness", "weights")

    def __init__(
        self,
        *,
        weights: QualityWeights | None = None,
        compute_sharpness: bool = True,
        sharpness_provider: SharpnessProvider | None = None,
    ) -> None:
        self.weights = weights if weights is not None else QualityWeights()
        self.compute_sharpness = compute_sharpness
        self._sharpness_provider: SharpnessProvider = (
            sharpness_provider
            if sharpness_provider is not None
            else laplacian_sharpness
        )

    def rank(self, media_files: Iterable[MediaFile]) -> list[RankedMedia]:
        """Rank media files best-first by composite quality score.

        Args:
            media_files: Candidate copies to rank.

        Returns:
            Ranked media ordered from best to worst copy.

        Raises:
            DuplicateError: If ``media_files`` is empty.
        """
        files = list(media_files)
        if not files:
            raise DuplicateError(
                "cannot rank an empty set of media files",
                operation="QualityComparator.rank",
            )

        metrics = [self._extract_metrics(media) for media in files]
        maxima = _dimension_maxima(metrics)
        scored = [
            (media, self._build_score(media=media, metrics=metric, maxima=maxima))
            for media, metric in zip(files, metrics, strict=True)
        ]
        scored.sort(key=lambda pair: _rank_key(media=pair[0], score=pair[1]))
        return [
            RankedMedia(media=media, score=score, rank=position)
            for position, (media, score) in enumerate(scored, start=1)
        ]

    def select_best(self, media_files: Iterable[MediaFile]) -> MediaFile:
        """Return the highest-quality copy among ``media_files``.

        Args:
            media_files: Candidate copies to compare.

        Returns:
            The media file ranked first.

        Raises:
            DuplicateError: If ``media_files`` is empty.
        """
        return self.rank(media_files)[0].media

    def rank_group(
        self,
        group: DuplicateGroup,
        media_by_path: Mapping[Path, MediaFile],
        *,
        set_target_path: bool = True,
    ) -> list[RankedMedia]:
        """Rank the files of a duplicate group and mark the best copy.

        Args:
            group: Duplicate group whose files should be ranked.
            media_by_path: Mapping from file path to its :class:`MediaFile`.
            set_target_path: Whether to set ``group.target_path`` to the best copy.

        Returns:
            Ranked media ordered from best to worst copy.

        Raises:
            DuplicateError: If the group is empty or a member path is missing from
                ``media_by_path``.
        """
        if not group.files:
            raise DuplicateError(
                "cannot rank an empty duplicate group",
                operation="QualityComparator.rank_group",
                details={"group_number": group.group_number},
            )
        media_files: list[MediaFile] = []
        for path in group.files:
            media = media_by_path.get(path)
            if media is None:
                raise DuplicateError(
                    "missing media file for duplicate group member",
                    operation="QualityComparator.rank_group",
                    file_path=path,
                )
            media_files.append(media)

        ranked = self.rank(media_files)
        if set_target_path:
            group.target_path = ranked[0].media.path
        return ranked

    def _extract_metrics(self, media: MediaFile) -> _Metrics:
        """Extract raw quality metrics from a media file.

        Args:
            media: Media file to inspect.

        Returns:
            Raw resolution, file size, sharpness, and bitrate metrics.
        """
        metadata = media.metadata
        width = metadata.width if metadata is not None else None
        height = metadata.height if metadata is not None else None
        bitrate = metadata.bitrate if metadata is not None else None
        resolution = (width or 0) * (height or 0)
        return _Metrics(
            resolution=resolution,
            file_size=media.size_bytes,
            sharpness=self._sharpness_for(media),
            bitrate=bitrate or 0,
        )

    def _sharpness_for(self, media: MediaFile) -> float:
        """Measure sharpness for a media file, degrading to ``0.0``.

        Args:
            media: Media file to inspect.

        Returns:
            Sharpness variance, or ``0.0`` when unavailable or not an image.
        """
        if not self.compute_sharpness or media.media_type is not MediaType.IMAGE:
            return 0.0
        sharpness = self._sharpness_provider(media.path)
        return sharpness if sharpness is not None else 0.0

    def _build_score(
        self,
        *,
        media: MediaFile,
        metrics: _Metrics,
        maxima: _Metrics,
    ) -> QualityScore:
        """Build a :class:`QualityScore` with a normalized composite.

        Args:
            media: Source media file.
            metrics: Raw metrics for ``media``.
            maxima: Per-dimension maxima across the compared set.

        Returns:
            Quality score whose ``composite_score`` lies in ``[0, 1]``.
        """
        composite = self._composite(metrics=metrics, maxima=maxima)
        metadata = media.metadata if media.metadata is not None else MediaMetadata()
        return QualityScore(
            composite_score=composite,
            resolution=metrics.resolution,
            quality_metric=metrics.sharpness,
            file_size=metrics.file_size,
            creation_date=media.creation_date,
            image_format=metadata.image_format,
            color_mode=metadata.color_mode,
            bit_depth=metadata.bit_depth,
            has_alpha=metadata.has_alpha,
            width=metadata.width,
            height=metadata.height,
        )

    def _composite(self, *, metrics: _Metrics, maxima: _Metrics) -> float:
        """Compute the weighted, normalized composite quality score.

        Args:
            metrics: Raw metrics for a single file.
            maxima: Per-dimension maxima across the compared set.

        Returns:
            Weighted average of normalized dimensions in ``[0, 1]``.
        """
        weighted = (
            self.weights.resolution * _normalize(metrics.resolution, maxima.resolution)
            + self.weights.file_size * _normalize(metrics.file_size, maxima.file_size)
            + self.weights.sharpness * _normalize(metrics.sharpness, maxima.sharpness)
            + self.weights.bitrate * _normalize(metrics.bitrate, maxima.bitrate)
        )
        return weighted / self.weights.total()

    @classmethod
    def from_weights(cls, weights: QualityWeights) -> Self:
        """Build a comparator with the given weights and default settings.

        Args:
            weights: Relative weights for each quality dimension.

        Returns:
            Configured quality comparator.
        """
        return cls(weights=weights)


def laplacian_sharpness(path: Path) -> float | None:
    """Measure image sharpness as the variance of a Laplacian filter.

    Soft-depends on Pillow: returns ``None`` when Pillow is not installed or the
    file cannot be opened and processed as an image.

    Args:
        path: Filesystem path to an image file.

    Returns:
        Variance of the Laplacian-filtered grayscale image, or ``None`` when it
        cannot be measured.
    """
    pillow = _import_pillow()
    if pillow is None:
        return None
    image_module, filter_module, stat_module = pillow
    try:
        with image_module.open(path) as image:
            grayscale = image.convert("L")
        kernel = filter_module.Kernel(
            size=(3, 3),
            kernel=_LAPLACIAN_KERNEL,
            scale=1,
        )
        edges = grayscale.filter(kernel)
        edges = _crop_unfiltered_border(edges)
        return float(stat_module.Stat(edges).var[0])
    except (OSError, ValueError, IndexError):
        return None


def _crop_unfiltered_border(edges: Any) -> Any:
    """Drop the one-pixel border a 3x3 kernel leaves unprocessed.

    Pillow copies the outermost pixels through unchanged, so their original
    intensities would otherwise skew the variance. The border is removed only
    when the image is large enough to leave an interior region.

    Args:
        edges: Laplacian-filtered image.

    Returns:
        The interior of the filtered image, or the original image when it is too
        small to crop.
    """
    width, height = edges.size
    if width <= 2 or height <= 2:
        return edges
    return edges.crop((1, 1, width - 1, height - 1))


def _import_pillow() -> tuple[Any, Any, Any] | None:
    """Import the Pillow modules used for sharpness measurement.

    Returns:
        Tuple of ``(Image, ImageFilter, ImageStat)`` modules, or ``None`` when
        Pillow is not installed.
    """
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return None
    return Image, ImageFilter, ImageStat


def _dimension_maxima(metrics: list[_Metrics]) -> _Metrics:
    """Compute the maximum value of each dimension across all metrics.

    Args:
        metrics: Raw metrics for every compared file.

    Returns:
        Metrics holding the per-dimension maxima.
    """
    return _Metrics(
        resolution=max((metric.resolution for metric in metrics), default=0),
        file_size=max((metric.file_size for metric in metrics), default=0),
        sharpness=max((metric.sharpness for metric in metrics), default=0.0),
        bitrate=max((metric.bitrate for metric in metrics), default=0),
    )


def _normalize(value: float, maximum: float) -> float:
    """Normalize a value to ``[0, 1]`` against a maximum.

    Args:
        value: Raw dimension value.
        maximum: Maximum value of the dimension across the compared set.

    Returns:
        ``value / maximum`` when ``maximum`` is positive, otherwise ``0.0``.
    """
    if maximum <= 0:
        return 0.0
    return value / maximum


def _rank_key(
    *,
    media: MediaFile,
    score: QualityScore,
) -> tuple[float, int, int, str]:
    """Return a deterministic best-first sort key for a scored file.

    Args:
        media: Source media file.
        score: Computed quality score.

    Returns:
        Sort key ordering by composite, resolution, file size (all descending via
        negation), then path ascending for stable tie-breaking.
    """
    return (
        -score.composite_score,
        -score.resolution,
        -score.file_size,
        str(media.path),
    )


__all__ = [
    "QualityComparator",
    "QualityWeights",
    "RankedMedia",
    "SharpnessProvider",
    "laplacian_sharpness",
]
