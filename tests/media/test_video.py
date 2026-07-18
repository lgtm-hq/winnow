"""Tests for the video metadata and frame extraction helpers.

FFmpeg and ffprobe are mocked so these tests run without the external binaries.
A single opt-in check exercises the real ffprobe path when it is installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import (  # nosec B404 - test builds fake results; no process launched
    CompletedProcess,
    TimeoutExpired,
)

import pytest
from assertpy import assert_that

from winnow.exceptions import MediaError
from winnow.media.video import _parse_frame_rate as parse_frame_rate
from winnow.media.video import (
    extract_frame,
    extract_video_metadata,
    ffprobe_available,
)

_WHICH_TARGET = "winnow.media.video.shutil.which"
_RUN_TARGET = "winnow.media.video.subprocess.run"

_FFPROBE_JSON = json.dumps(
    {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "aac",
            },
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "bit_rate": "5000000",
                "duration": "12.5",
            },
        ],
        "format": {"duration": "12.5", "bit_rate": "5200000"},
    }
)


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> CompletedProcess[str]:
    """Build a fake completed process for mocking subprocess.run.

    Args:
        returncode: Simulated process exit code.
        stdout: Simulated standard output.
        stderr: Simulated standard error.

    Returns:
        A populated ``CompletedProcess`` instance.
    """
    return CompletedProcess(
        args=["ffprobe"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _use_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ffprobe and ffmpeg are installed at a fixed path.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(_WHICH_TARGET, lambda name: f"/usr/bin/{name}")


def test_extract_video_metadata_parses_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe JSON is parsed into structured video metadata."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    _use_binaries(monkeypatch)
    monkeypatch.setattr(_RUN_TARGET, lambda *a, **k: _completed(stdout=_FFPROBE_JSON))

    metadata = extract_video_metadata(video)

    assert_that(metadata.width).is_equal_to(1920)
    assert_that(metadata.height).is_equal_to(1080)
    assert_that(metadata.codec).is_equal_to("h264")
    assert_that(metadata.duration_seconds).is_equal_to(12.5)
    assert_that(metadata.bitrate).is_equal_to(5000000)
    assert_that(metadata.frame_rate).is_close_to(29.97, 0.01)


def test_extract_video_metadata_degrades_without_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ffprobe yields empty metadata instead of raising."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(_WHICH_TARGET, lambda name: None)

    metadata = extract_video_metadata(video)

    assert_that(metadata.width).is_none()
    assert_that(metadata.codec).is_none()
    assert_that(metadata.duration_seconds).is_none()


def test_extract_video_metadata_missing_file() -> None:
    """A missing video path raises MediaError."""
    with pytest.raises(MediaError):
        extract_video_metadata(Path("/nonexistent/clip.mp4"))


def test_extract_video_metadata_ffprobe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero ffprobe exit raises MediaError."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    _use_binaries(monkeypatch)
    monkeypatch.setattr(
        _RUN_TARGET,
        lambda *a, **k: _completed(returncode=1, stderr="boom"),
    )

    with pytest.raises(MediaError):
        extract_video_metadata(video)


def test_extract_video_metadata_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undecodable ffprobe output raises MediaError."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    _use_binaries(monkeypatch)
    monkeypatch.setattr(_RUN_TARGET, lambda *a, **k: _completed(stdout="not json"))

    with pytest.raises(MediaError):
        extract_video_metadata(video)


def test_extract_video_metadata_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess timeout is surfaced as MediaError."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    _use_binaries(monkeypatch)

    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise TimeoutExpired(cmd="ffprobe", timeout=1.0)

    monkeypatch.setattr(_RUN_TARGET, _raise_timeout)

    with pytest.raises(MediaError):
        extract_video_metadata(video)


@pytest.mark.parametrize(
    ("frame_rate", "expected"),
    [
        ("30000/1001", 29.97),
        ("25/1", 25.0),
    ],
    ids=["ntsc", "pal"],
)
def test_frame_rate_parsing_valid(frame_rate: str, expected: float) -> None:
    """Valid frame-rate fractions convert to frames per second."""
    result = parse_frame_rate(frame_rate)

    assert_that(result).is_close_to(expected, 0.01)


@pytest.mark.parametrize(
    "frame_rate",
    ["0/0", "not-a-rate", "", "30"],
    ids=["degenerate", "garbage", "empty", "no_slash"],
)
def test_frame_rate_parsing_invalid(frame_rate: str) -> None:
    """Degenerate or malformed frame-rate values return None."""
    assert_that(parse_frame_rate(frame_rate)).is_none()


def test_extract_frame_writes_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame extraction writes and returns the destination path."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    destination = tmp_path / "frames" / "frame.png"
    _use_binaries(monkeypatch)

    def _fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        Path(argv[-1]).write_bytes(b"image")
        return _completed()

    monkeypatch.setattr(_RUN_TARGET, _fake_run)

    result = extract_frame(video, destination, timestamp_seconds=1.0)

    assert_that(result).is_equal_to(destination)
    assert_that(destination.is_file()).is_true()


def test_extract_frame_requires_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame extraction raises MediaError when ffmpeg is absent."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(_WHICH_TARGET, lambda name: None)

    with pytest.raises(MediaError):
        extract_frame(video, tmp_path / "frame.png")


def test_extract_frame_missing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frame extraction raises MediaError when the source is missing."""
    _use_binaries(monkeypatch)

    with pytest.raises(MediaError):
        extract_frame(tmp_path / "missing.mp4", tmp_path / "frame.png")


def test_extract_frame_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ffmpeg run raises MediaError."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    _use_binaries(monkeypatch)
    monkeypatch.setattr(
        _RUN_TARGET,
        lambda *a, **k: _completed(returncode=1, stderr="boom"),
    )

    with pytest.raises(MediaError):
        extract_frame(video, tmp_path / "frame.png")


def test_run_handles_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch OSError from subprocess is surfaced as MediaError."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    _use_binaries(monkeypatch)

    def _raise_os_error(*args: object, **kwargs: object) -> None:
        raise OSError("cannot exec")

    monkeypatch.setattr(_RUN_TARGET, _raise_os_error)

    with pytest.raises(MediaError):
        extract_video_metadata(video)


@pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")
def test_extract_video_metadata_real_ffprobe(fixtures_dir: Path) -> None:
    """The real ffprobe binary probes the committed MP4 fixture."""
    metadata = extract_video_metadata(fixtures_dir / "sample.mp4")

    assert_that(metadata.width).is_equal_to(64)
    assert_that(metadata.height).is_equal_to(48)
    assert_that(metadata.codec).is_equal_to("h264")
    assert_that(metadata.frame_rate).is_equal_to(10.0)
