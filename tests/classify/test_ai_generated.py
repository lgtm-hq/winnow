"""Tests for AI-generated image detection heuristics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from tests.classify.conftest import ImageFactory
from tests.media.conftest import FIXTURE_DIR
from tests.media.test_c2pa import write_jpeg_with_app11
from winnow.classify import (
    AiGeneratedConfig,
    classify_ai_generated,
    detect_ai_generated,
)
from winnow.media import manifest_declares_ai_source, read_c2pa_manifest

_EXIF_SOFTWARE_TAG = 0x0131
_EXIF_DESCRIPTION_TAG = 0x010E


@pytest.mark.parametrize(
    ("kwargs", "expected_signal"),
    [
        ({"c2pa_ai_source": True}, "c2pa_ai_source"),
        ({"text_chunks": {"parameters": "a cat, Steps: 20"}}, "prompt_chunk"),
        ({"text_chunks": {"workflow": "{}"}}, "prompt_chunk"),
        ({"software": "Midjourney v6"}, "generator_marker"),
        ({"description": "Made with Adobe Firefly"}, "generator_marker"),
        ({"xmp": "<x:xmpmeta>DALL-E 3</x:xmpmeta>"}, "generator_marker"),
    ],
    ids=[
        "c2pa_ai_source",
        "prompt_chunk_parameters",
        "prompt_chunk_workflow",
        "marker_in_software",
        "marker_in_description",
        "marker_in_xmp",
    ],
)
def test_each_signal_alone_crosses_threshold(
    kwargs: dict[str, Any],
    expected_signal: str,
) -> None:
    """Every signal is strong enough on its own to flag the image."""
    result = classify_ai_generated(**kwargs)

    assert_that(result.is_ai_generated).is_true()
    assert_that(result.confidence).is_greater_than_or_equal_to(0.5)
    assert_that(bool(getattr(result.signals, expected_signal))).is_true()


def test_no_signals_scores_zero() -> None:
    """Without any signal the image is not flagged and confidence is 0.0."""
    result = classify_ai_generated()

    assert_that(result.is_ai_generated).is_false()
    assert_that(result.confidence).is_equal_to(0.0)
    assert_that(result.signals.c2pa_ai_source).is_false()
    assert_that(result.signals.prompt_chunk).is_none()
    assert_that(result.signals.generator_marker).is_none()


def test_ordinary_metadata_scores_zero() -> None:
    """Camera and editor metadata without generator markers is not flagged."""
    result = classify_ai_generated(
        text_chunks={"Comment": "holiday"},
        software="Adobe Photoshop 25.0",
        description="Beach at sunset",
        xmp="<x:xmpmeta xmlns:x='adobe:ns:meta/'/>",
    )

    assert_that(result.is_ai_generated).is_false()
    assert_that(result.confidence).is_equal_to(0.0)


@pytest.mark.parametrize(
    "software",
    ["MIDJOURNEY", "Stable Diffusion", "stablediffusion", "ComfyUI 0.3"],
    ids=["upper", "title", "joined", "versioned"],
)
def test_marker_matching_is_case_insensitive(software: str) -> None:
    """Generator markers match regardless of case."""
    result = classify_ai_generated(software=software)

    assert_that(result.signals.generator_marker).is_not_none()
    assert_that(result.is_ai_generated).is_true()


def test_prompt_chunk_key_matching_is_case_insensitive() -> None:
    """Prompt chunk keywords match regardless of case."""
    result = classify_ai_generated(text_chunks={"Parameters": "prompt text"})

    assert_that(result.signals.prompt_chunk).is_equal_to("Parameters")


def test_c2pa_without_ai_source_scores_zero() -> None:
    """A manifest that is present but not AI-sourced contributes nothing."""
    result = classify_ai_generated(c2pa_ai_source=False)

    assert_that(result.confidence).is_equal_to(0.0)
    assert_that(result.is_ai_generated).is_false()


def test_signals_sum_and_clamp_to_one() -> None:
    """Multiple fired signals add up but never exceed 1.0."""
    result = classify_ai_generated(
        text_chunks={"prompt": "x"},
        software="Fooocus",
        c2pa_ai_source=True,
    )

    assert_that(result.confidence).is_equal_to(1.0)


def test_configured_markers_match_case_insensitively() -> None:
    """A mixed-case configured marker still matches casefolded metadata."""
    config = AiGeneratedConfig(generator_markers=frozenset({"MyGen"}))

    matched = classify_ai_generated(software="rendered by mygen 2", config=config)

    assert_that(matched.signals.generator_marker).is_equal_to("MyGen")


def test_generic_words_do_not_trigger_generator_marker() -> None:
    """Ordinary text containing ``imagen`` is not treated as a generator name."""
    result = classify_ai_generated(description="Imagen de vacaciones", xmp="ImageName")

    assert_that(result.signals.generator_marker).is_none()
    assert_that(result.is_ai_generated).is_false()


def test_custom_config_changes_weights_and_markers() -> None:
    """Custom weights, threshold, and markers are honoured."""
    config = AiGeneratedConfig(
        marker_weight=0.2,
        threshold=0.3,
        generator_markers=frozenset({"mygen"}),
    )

    matched = classify_ai_generated(software="MyGen 1.0", config=config)
    unmatched = classify_ai_generated(software="Midjourney", config=config)

    assert_that(matched.signals.generator_marker).is_equal_to("mygen")
    assert_that(matched.confidence).is_equal_to(0.2)
    assert_that(matched.is_ai_generated).is_false()
    assert_that(unmatched.signals.generator_marker).is_none()


def test_detect_flags_png_with_parameters_chunk(tmp_path: Path) -> None:
    """A PNG carrying a ``parameters`` text chunk is detected as AI-generated."""
    info = PngInfo()
    info.add_text("parameters", "a cat, Steps: 20, Sampler: Euler")
    path = tmp_path / "generated.png"
    Image.new("RGB", (8, 8)).save(path, pnginfo=info)

    result = detect_ai_generated(path)

    assert_that(result.is_ai_generated).is_true()
    assert_that(result.signals.prompt_chunk).is_equal_to("parameters")


def test_detect_reads_png_xmp_text_chunk(tmp_path: Path) -> None:
    """The PNG ``XML:com.adobe.xmp`` chunk is searched for generator markers."""
    info = PngInfo()
    info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta>Adobe Firefly</x:xmpmeta>")
    path = tmp_path / "firefly.png"
    Image.new("RGB", (8, 8)).save(path, pnginfo=info)

    result = detect_ai_generated(path)

    assert_that(result.signals.generator_marker).is_equal_to("adobe firefly")


def test_detect_reads_exif_software_and_description(make_image: ImageFactory) -> None:
    """EXIF Software and ImageDescription are both searched for markers."""
    by_software = make_image(name="a.jpg", exif={_EXIF_SOFTWARE_TAG: "NovelAI"})
    by_description = make_image(
        name="b.jpg",
        exif={_EXIF_DESCRIPTION_TAG: "Generated with Ideogram"},
    )

    assert_that(detect_ai_generated(by_software).signals.generator_marker).is_equal_to(
        "novelai",
    )
    assert_that(
        detect_ai_generated(by_description).signals.generator_marker,
    ).is_equal_to("ideogram")


def test_detect_reads_jpeg_xmp(tmp_path: Path) -> None:
    """A JPEG XMP packet is decoded and searched for generator markers."""
    path = tmp_path / "xmp.jpg"
    xmp = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>Leonardo.Ai</x:xmpmeta>"
    Image.new("RGB", (8, 8)).save(path, xmp=xmp)

    result = detect_ai_generated(path)

    assert_that(result.signals.generator_marker).is_equal_to("leonardo.ai")


def test_detect_scores_c2pa_only_with_ai_source(tmp_path: Path) -> None:
    """A C2PA manifest scores only when it declares an AI source type."""
    camera = write_jpeg_with_app11(
        tmp_path / "camera.jpg",
        b"jumbc2pa.digitalSourceType:digitalCapture",
    )
    generated = write_jpeg_with_app11(
        tmp_path / "generated.jpg",
        b"jumbc2pa.digitalSourceType:trainedAlgorithmicMedia",
    )
    camera_manifest = read_c2pa_manifest(camera)

    camera_result = detect_ai_generated(camera)
    generated_result = detect_ai_generated(generated)

    assert_that(camera_manifest).is_not_none()
    assert_that(manifest_declares_ai_source(camera_manifest or b"")).is_false()
    assert_that(camera_result.confidence).is_equal_to(0.0)
    assert_that(camera_result.is_ai_generated).is_false()
    assert_that(generated_result.signals.c2pa_ai_source).is_true()
    assert_that(generated_result.is_ai_generated).is_true()


def test_detect_ignores_plain_camera_jpeg() -> None:
    """The committed camera-style JPEG fixture is not flagged."""
    result = detect_ai_generated(FIXTURE_DIR / "sample.jpg")

    assert_that(result.is_ai_generated).is_false()
    assert_that(result.confidence).is_equal_to(0.0)
