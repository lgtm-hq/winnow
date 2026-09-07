"""AI-generated image detection via metadata heuristics.

Generative-AI tools leave three cheap, dependency-free tells in the files they
write:

- A C2PA manifest whose ``digitalSourceType`` names trained algorithmic media
  (see :mod:`winnow.media.c2pa`). Bare manifest presence never scores: cameras
  and desktop editors write C2PA too.
- A PNG text chunk carrying the generation prompt or workflow (for example
  ``parameters`` from AUTOMATIC1111 or ``workflow`` from ComfyUI).
- A generator name in EXIF ``Software``, EXIF ``ImageDescription``, or the raw
  XMP packet (for example ``Midjourney`` or ``Adobe Firefly``).

Detection is a weighted score. Each fired signal contributes its configured
weight; the image is flagged when the summed confidence reaches the configured
threshold. The pure :func:`classify_ai_generated` function operates on
already-extracted signals; :func:`detect_ai_generated` reads them from a file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from winnow.classify._image import (
    extract_exif,
    extract_text_chunks,
    extract_xmp,
    open_image,
)
from winnow.media.c2pa import manifest_declares_ai_source, read_c2pa_manifest

DEFAULT_PROMPT_CHUNK_KEYS: frozenset[str] = frozenset(
    {
        "parameters",
        "prompt",
        "workflow",
        "sd-metadata",
        "Dream",
        "invokeai_metadata",
    },
)
"""Default PNG text chunk keywords written by image generators.

Matched case-insensitively. Override or extend via
:attr:`AiGeneratedConfig.prompt_chunk_keys`.
"""

DEFAULT_GENERATOR_MARKERS: frozenset[str] = frozenset(
    {
        "midjourney",
        "dall-e",
        "dall·e",
        "stable diffusion",
        "stablediffusion",
        "adobe firefly",
        "novelai",
        "leonardo.ai",
        "ideogram",
        "google imagen",
        "flux.1",
        "comfyui",
        "automatic1111",
        "fooocus",
    },
)
"""Default case-insensitive substrings that name an image generator.

Matched against EXIF ``Software``, EXIF ``ImageDescription``, and the raw XMP
text. Override or extend via :attr:`AiGeneratedConfig.generator_markers`.
"""


@dataclass(frozen=True, slots=True)
class AiGeneratedConfig:
    """Tunable weights and threshold for AI-generated detection.

    Attributes:
        c2pa_weight: Confidence added when the C2PA manifest declares a
            generative-AI source type.
        prompt_chunk_weight: Confidence added when a PNG text chunk carries a
            generation prompt or workflow.
        marker_weight: Confidence added when a generator name appears in EXIF
            or XMP text.
        threshold: Minimum summed confidence, in ``[0.0, 1.0]``, required to
            flag an image as AI-generated.
        prompt_chunk_keys: Case-insensitive PNG text chunk keywords that mark a
            generation prompt. Defaults to :data:`DEFAULT_PROMPT_CHUNK_KEYS`.
        generator_markers: Case-insensitive substrings that name a generator.
            Defaults to :data:`DEFAULT_GENERATOR_MARKERS`.
    """

    c2pa_weight: float = 0.9
    prompt_chunk_weight: float = 0.8
    marker_weight: float = 0.8
    threshold: float = 0.5
    prompt_chunk_keys: frozenset[str] = field(
        default_factory=lambda: DEFAULT_PROMPT_CHUNK_KEYS,
    )
    generator_markers: frozenset[str] = field(
        default_factory=lambda: DEFAULT_GENERATOR_MARKERS,
    )


@dataclass(frozen=True, slots=True)
class AiGeneratedSignals:
    """Individual heuristics that fired during AI-generated detection.

    Attributes:
        c2pa_ai_source: Whether the C2PA manifest declares an AI source type.
        prompt_chunk: The matching PNG text chunk keyword, or ``None``.
        generator_marker: The matching generator marker, or ``None``.
    """

    c2pa_ai_source: bool
    prompt_chunk: str | None
    generator_marker: str | None


@dataclass(frozen=True, slots=True)
class AiGeneratedClassification:
    """Result of AI-generated detection.

    Attributes:
        is_ai_generated: Whether the confidence reached the configured threshold.
        confidence: Summed signal confidence, clamped to ``[0.0, 1.0]``.
        signals: The individual signals that contributed to the score.
    """

    is_ai_generated: bool
    confidence: float
    signals: AiGeneratedSignals


def classify_ai_generated(
    *,
    text_chunks: Mapping[str, str] | None = None,
    software: str | None = None,
    description: str | None = None,
    xmp: str | None = None,
    c2pa_ai_source: bool = False,
    config: AiGeneratedConfig | None = None,
) -> AiGeneratedClassification:
    """Classify an image as AI-generated from pre-extracted signals.

    Args:
        text_chunks: PNG text chunks keyed by keyword, if any.
        software: EXIF ``Software`` value, if any.
        description: EXIF ``ImageDescription`` value, if any.
        xmp: Raw XMP packet text, if any.
        c2pa_ai_source: Whether the file's C2PA manifest declares a
            generative-AI source type. Bare manifest presence must be passed as
            ``False``.
        config: Detection weights and threshold. Defaults to
            :class:`AiGeneratedConfig`.

    Returns:
        An :class:`AiGeneratedClassification` describing the decision.
    """
    active_config = config if config is not None else AiGeneratedConfig()

    signals = AiGeneratedSignals(
        c2pa_ai_source=c2pa_ai_source,
        prompt_chunk=_matching_prompt_chunk(
            text_chunks=text_chunks,
            config=active_config,
        ),
        generator_marker=_matching_generator_marker(
            texts=(software, description, xmp),
            config=active_config,
        ),
    )

    confidence = 0.0
    if signals.c2pa_ai_source:
        confidence += active_config.c2pa_weight
    if signals.prompt_chunk is not None:
        confidence += active_config.prompt_chunk_weight
    if signals.generator_marker is not None:
        confidence += active_config.marker_weight
    confidence = min(1.0, confidence)

    return AiGeneratedClassification(
        is_ai_generated=confidence >= active_config.threshold,
        confidence=confidence,
        signals=signals,
    )


def detect_ai_generated(
    path: Path,
    *,
    config: AiGeneratedConfig | None = None,
) -> AiGeneratedClassification:
    """Detect whether an image file is AI-generated.

    Reads PNG text chunks, EXIF, XMP, and the C2PA manifest from the file, then
    applies :func:`classify_ai_generated`.

    Args:
        path: Filesystem path to the image.
        config: Detection weights and threshold. Defaults to
            :class:`AiGeneratedConfig`.

    Returns:
        An :class:`AiGeneratedClassification` describing the decision.

    Raises:
        MediaError: If the file cannot be read as an image.
    """
    with open_image(path) as image:
        text_chunks = extract_text_chunks(image)
        exif = extract_exif(image)
        xmp = extract_xmp(image)
    manifest = read_c2pa_manifest(path)

    return classify_ai_generated(
        text_chunks=text_chunks,
        software=exif.get("Software"),
        description=exif.get("ImageDescription"),
        xmp=xmp,
        c2pa_ai_source=manifest is not None and manifest_declares_ai_source(manifest),
        config=config,
    )


def _matching_prompt_chunk(
    *,
    text_chunks: Mapping[str, str] | None,
    config: AiGeneratedConfig,
) -> str | None:
    """Return the first text chunk keyword that names a generation prompt.

    Args:
        text_chunks: PNG text chunks keyed by keyword.
        config: Detection config providing the prompt chunk keywords.

    Returns:
        The matching chunk keyword as stored in the image, or ``None``.
    """
    if not text_chunks:
        return None
    wanted = {key.casefold() for key in config.prompt_chunk_keys}
    return next(
        (key for key in sorted(text_chunks) if key.casefold() in wanted),
        None,
    )


def _matching_generator_marker(
    *,
    texts: tuple[str | None, ...],
    config: AiGeneratedConfig,
) -> str | None:
    """Return the first generator marker found in any of the given texts.

    Args:
        texts: Candidate metadata strings (``None`` entries are skipped).
        config: Detection config providing the generator markers.

    Returns:
        The matching marker, or ``None``.
    """
    haystack = "\n".join(text.casefold() for text in texts if text)
    if not haystack:
        return None
    return next(
        (
            marker
            for marker in sorted(config.generator_markers)
            if marker.casefold() in haystack
        ),
        None,
    )
