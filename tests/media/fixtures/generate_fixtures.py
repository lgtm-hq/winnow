"""Regenerate the tiny media fixtures used by the media processor tests.

Run with ``uv run python tests/media/fixtures/generate_fixtures.py``. Image
fixtures are produced with Pillow; audio and video fixtures are produced with
the system ``ffmpeg`` binary. All outputs are intentionally tiny (a few KB) so
they can be committed to the repository.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed argv lists only; never shell=True
from pathlib import Path

from PIL import Image

FIXTURE_DIR = Path(__file__).resolve().parent
DATED_DATETIME_ORIGINAL = "2024:03:01 12:34:56"
DATED_DATETIME = "2024:03:02 00:00:00"
AUDIO_TITLE = "Winnow Sample"
AUDIO_ARTIST = "Winnow"
AUDIO_ALBUM = "Fixtures"


def write_dated_images(directory: Path) -> list[Path]:
    """Write ``dated.jpg`` and ``dated.heic`` carrying known EXIF capture dates.

    Both files are 32x32 RGB red with ExifIFD ``DateTimeOriginal`` set to
    :data:`DATED_DATETIME_ORIGINAL` and IFD0 ``DateTime`` set to
    :data:`DATED_DATETIME`. The HEIC is skipped when ``pillow-heif`` cannot
    encode on this platform.

    Args:
        directory: Existing directory to write the fixtures into.

    Returns:
        Paths that were written; ``dated.heic`` is absent when HEIF encoding
        is unavailable.
    """
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = DATED_DATETIME_ORIGINAL
    exif[0x0132] = DATED_DATETIME
    red = Image.new("RGB", (32, 32), (255, 0, 0))

    jpeg = directory / "dated.jpg"
    red.save(jpeg, exif=exif.tobytes())
    written = [jpeg]

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        heic = directory / "dated.heic"
        red.save(heic, format="HEIF", exif=exif.tobytes())
        written.append(heic)
    except Exception as exc:  # noqa: BLE001 - optional codec
        print(f"skipping dated HEIC fixture: {exc}")
    return written


def _generate_images() -> None:
    """Write small raster fixtures across the common image formats."""
    write_dated_images(FIXTURE_DIR)
    rgb = Image.new("RGB", (8, 6), (120, 60, 30))
    rgba = Image.new("RGBA", (8, 6), (10, 20, 30, 128))

    exif = Image.Exif()
    exif[0x010F] = "Winnow"
    exif[0x0110] = "TestCam"
    exif[0x0112] = 1
    rgb.save(FIXTURE_DIR / "sample.jpg", exif=exif)

    rgba.save(FIXTURE_DIR / "sample.png")
    rgb.save(FIXTURE_DIR / "sample.tiff")
    rgb.save(FIXTURE_DIR / "sample.webp")
    rgb.convert("P").save(FIXTURE_DIR / "sample.gif")
    rgb.save(FIXTURE_DIR / "sample.bmp")

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        rgb.save(FIXTURE_DIR / "sample.heic")
    except Exception as exc:  # noqa: BLE001 - optional codec
        print(f"skipping HEIC fixture: {exc}")

    with (FIXTURE_DIR / "corrupt.jpg").open("wb") as handle:
        handle.write(b"\xff\xd8\xff\xe0not-a-real-jpeg")


def _ffmpeg_audio(*, output: Path, source: str, extra: list[str]) -> None:
    """Generate a short silent audio fixture tagged with sample metadata."""
    argv = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
        "-t",
        "0.2",
        "-metadata",
        f"title={AUDIO_TITLE}",
        "-metadata",
        f"artist={AUDIO_ARTIST}",
        "-metadata",
        f"album={AUDIO_ALBUM}",
        *extra,
        str(output),
    ]
    subprocess.run(argv, check=True, capture_output=True)  # nosec B603 B607


def _generate_audio() -> None:
    """Write short audio fixtures across the supported containers."""
    stereo = "anullsrc=r=44100:cl=stereo"
    mono = "anullsrc=r=8000:cl=mono"
    _ffmpeg_audio(
        output=FIXTURE_DIR / "sample.mp3", source=stereo, extra=["-c:a", "libmp3lame"]
    )
    _ffmpeg_audio(
        output=FIXTURE_DIR / "sample.flac", source=stereo, extra=["-c:a", "flac"]
    )
    _ffmpeg_audio(
        output=FIXTURE_DIR / "sample.ogg", source=stereo, extra=["-c:a", "libvorbis"]
    )
    _ffmpeg_audio(
        output=FIXTURE_DIR / "sample.wav", source=mono, extra=["-c:a", "pcm_s16le"]
    )


def _generate_video() -> None:
    """Write a tiny H.264 MP4 fixture with known dimensions and frame rate."""
    argv = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=64x48:rate=10:duration=0.5",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        str(FIXTURE_DIR / "sample.mp4"),
    ]
    subprocess.run(argv, check=True, capture_output=True)  # nosec B603 B607


def main() -> None:
    """Generate every media fixture into this directory."""
    _generate_images()
    _generate_audio()
    _generate_video()
    print(f"fixtures written to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
