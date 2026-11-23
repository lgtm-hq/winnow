"""Shared helpers for the media-type detection tests (#393)."""

from __future__ import annotations

import functools
import itertools
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from assertpy import assert_that
from PIL import Image

from tests.media.bmff_support import box, canon_uuid, ftyp, moov
from winnow.media import service as service_module
from winnow.media._detect import DetectionResult, TypeSignal, detect
from winnow.media._ffprobe import FFMPEG as FFMPEG_NAME
from winnow.media._ffprobe import FFPROBE as FFPROBE_NAME
from winnow.media._ffprobe import find_binary, run_command
from winnow.models.media import MediaMetadata, MediaType

FIXTURES = Path(__file__).resolve().parent / "fixtures"
AUDIO_FIXTURES = FIXTURES / "audio"
FFMPEG = find_binary(FFMPEG_NAME)
FFPROBE = find_binary(FFPROBE_NAME)
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")
needs_ffprobe = pytest.mark.skipif(FFPROBE is None, reason="ffprobe not installed")

LIBHEIF_BRANDS = ("mif1", "heic", "miaf")
CAMERA_BRANDS = ("mif1", "MiHE", "MiPr", "miaf", "MiHB", "heic")
CR3_PROFILE = b"CanonCR3_001/00.09.00/00.00.00"
CRM_PROFILE = b"CanonCRM0001/02.09.00/00.00.00"

VIDEO_IN = ("-f", "lavfi", "-i", "testsrc=duration=0.2:size=176x144:rate=10")
AUDIO_IN = ("-f", "lavfi", "-i", "sine=duration=0.3")
AUDIO_48K_IN = ("-f", "lavfi", "-i", "sine=duration=0.3:sample_rate=48000")

_CODEC_FLAGS = frozenset({"-c", "-c:a", "-c:v", "-codec", "-acodec", "-vcodec"})


@functools.cache
def ffmpeg_encoders() -> frozenset[str]:
    """Return the encoders the installed ffmpeg offers.

    Returns:
        Encoder names; empty without ffmpeg or when it cannot list them.
    """
    if FFMPEG is None:
        return frozenset()
    completed = run_command(
        argv=[FFMPEG, "-hide_banner", "-encoders"],
        timeout=60,
        operation="list ffmpeg encoders",
        path=Path(FFMPEG),
    )
    names = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        # Rows look like " V....D libx264  description"; the legend ends at "------".
        if len(fields) >= 2 and len(fields[0]) == 6 and fields[0][0] in {"V", "A", "S"}:
            names.add(fields[1])
    return frozenset(names)


def run_ffmpeg(*args: str, binary: str | None = None) -> None:
    """Run ffmpeg quietly with a fixed argument vector.

    Skips the test when ffmpeg, or an encoder ``args`` names, is missing.

    Args:
        args: Arguments after the binary and the quiet flags.
        binary: ffmpeg to run; defaults to the one on ``PATH``.
    """
    binary = binary if binary is not None else FFMPEG
    if binary is None:
        pytest.skip("ffmpeg not installed")
    encoders = ffmpeg_encoders()
    for flag, value in itertools.pairwise(args):
        if flag in _CODEC_FLAGS and value not in encoders:
            pytest.skip(f"ffmpeg encoder {value} not available")
    completed = run_command(
        argv=[binary, "-hide_banner", "-loglevel", "error", "-y", *args],
        timeout=60,
        operation="write test sample",
        path=Path(args[-1]),
    )
    assert_that(completed.returncode).described_as(completed.stderr).is_zero()


def decision(result: DetectionResult) -> TypeSignal:
    """Return the signal that carries the decision.

    Args:
        result: Detection result.

    Returns:
        The single ``decision=`` signal.
    """
    decisions = [s for s in result.signals if s.detail.startswith("decision=")]
    assert_that(decisions).is_length(1)
    return decisions[0]


def details(result: DetectionResult) -> list[str]:
    """Return every signal detail.

    Args:
        result: Detection result.

    Returns:
        Signal details in order.
    """
    return [signal.detail for signal in result.signals]


def save_heif(path: Path, image: Image.Image) -> None:
    """Save ``image`` as HEIF with pillow-heif, skipping when it cannot.

    Args:
        path: Destination.
        image: Image to save.
    """
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        image.save(path, format="HEIF")
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        pytest.skip(f"pillow-heif cannot encode HEIF here: {exc}")


def write_canon(path: Path, profile: bytes | None) -> Path:
    """Write a bounded synthetic Canon CRX file with ``vide`` tracks.

    Args:
        path: Destination.
        profile: ``CNCV`` payload, or ``None`` for no Canon uuid box.

    Returns:
        ``path``.
    """
    extra = b"" if profile is None else canon_uuid(profile)
    data = ftyp("crx ", ("crx ", "isom"))
    data += moov(("vide", "vide", "vide"), extra=extra) + box(b"mdat", b"\x00" * 16)
    path.write_bytes(data)
    return path


def detect_with_deadline(target: Path) -> DetectionResult:
    """Run :func:`detect` in a thread and fail if it blocks.

    Args:
        target: Path to classify.

    Returns:
        The result.
    """
    results: list[DetectionResult] = []
    worker = threading.Thread(target=lambda: results.append(detect(target)))
    worker.daemon = True
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Release a reader stuck opening a FIFO, then fail.
        writer = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        worker.join(timeout=5)
        pytest.fail(f"detect blocked on {target}")
    return results[0]


def record_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[MediaType]:
    """Replace the service's extractors with recorders.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The list each dispatched media type is appended to.
    """
    dispatched: list[MediaType] = []

    def _recorder(media_type: MediaType) -> Callable[[Path], MediaMetadata]:
        def record(_: Path) -> MediaMetadata:
            dispatched.append(media_type)
            return MediaMetadata()

        return record

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {media_type: _recorder(media_type) for media_type in MediaType},
    )
    return dispatched
