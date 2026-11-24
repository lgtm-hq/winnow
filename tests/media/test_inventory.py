"""Tests for directory inventory and file inspection in :mod:`winnow.media`."""

from __future__ import annotations

import contextlib
import os
import shutil
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from assertpy import assert_that
from PIL import Image

from tests.media.conftest import FIXTURE_DIR
from tests.media.ebml_support import AUDIO as MKV_AUDIO
from tests.media.ebml_support import cluster, matroska, tracks
from tests.media.signature_support import program_stream
from winnow.exceptions import MediaError
from winnow.fs.walk import WalkEvent, WalkEventKind, WalkPolicy
from winnow.media import detect, inventory
from winnow.media import service as service_module
from winnow.media._detect_base import DetectionResult
from winnow.media.inventory import (
    Classification,
    ExtensionCheck,
    collect_directory_stats,
    extension_check,
    inspect_file,
    iter_inventory,
    iter_regular_files,
)
from winnow.media.registry import create_default_format_registry, detect_media_type
from winnow.models.config import PathSettings, WinnowConfig
from winnow.models.enums import SymlinkPolicy
from winnow.models.media import MediaMetadata, MediaType
from winnow.models.pipeline import PipelineResult, RunMetadata
from winnow.pipeline import DiscoveryStep, PipelineContext, RunState

if TYPE_CHECKING:
    from collections.abc import Iterator

_MIDI = (
    b"MThd"
    + struct.pack(">IHHH", 6, 0, 1, 96)
    + b"MTrk"
    + struct.pack(">I", 4)
    + b"\x00\xff\x2f\x00"
)
"""A minimal Standard MIDI File: header chunk and one empty track."""

_CAF = b"caff\x00\x01\x00\x00" + b"desc" + struct.pack(">q", 32) + b"\x00" * 32
"""A minimal Core Audio Format file: file header and a ``desc`` chunk."""

_VOB = program_stream((0xE0, b"\x00\x00\x01\xb3" + b"\x00" * 16), video_bound=1)
"""An MPEG-2 program stream carrying one video PES packet."""


def _deny_scandir(
    monkeypatch: pytest.MonkeyPatch,
    locked: Path,
    *,
    vanish: Path | None = None,
) -> None:
    """Make ``locked`` unlistable and optionally delete a file mid-walk.

    ``os.scandir`` raises ``PermissionError`` for ``locked``, as it does for a
    directory without read permission, on every platform and for every user.
    When ``vanish`` is given, it is deleted right after its directory is
    listed, so the walk sees an entry whose file is gone.

    Args:
        monkeypatch: Fixture used to replace ``os.scandir``.
        locked: Directory that cannot be listed.
        vanish: File removed after its parent is listed.
    """
    real_scandir = os.scandir

    def fake_scandir(directory: Any) -> Any:
        if Path(directory) == locked:
            raise PermissionError(13, "Permission denied", str(directory))
        with real_scandir(directory) as handle:
            entries = list(handle)
        if vanish is not None and Path(directory) == vanish.parent:
            vanish.unlink(missing_ok=True)
        return contextlib.nullcontext(entries)

    monkeypatch.setattr(os, "scandir", fake_scandir)


def _spy_detect(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every path the inventory passes to content detection.

    Args:
        monkeypatch: Fixture used to wrap ``detect``.

    Returns:
        The list the spy appends each path to.
    """
    calls: list[Path] = []
    real_detect = detect

    def spy(path: Path, **kwargs: Any) -> DetectionResult:
        calls.append(path)
        return real_detect(path, **kwargs)

    monkeypatch.setattr(inventory, "detect", spy)
    return calls


def _discover(source: Path, *, config: WinnowConfig) -> RunState:
    """Run the Discovery step on ``source``.

    Args:
        source: Directory to discover.
        config: Configuration for the run.

    Returns:
        The state after the step ran.
    """
    result = PipelineResult(
        run=RunMetadata(started_at=datetime.now(tz=UTC), winnow_version="0.0.0"),
    )
    state = RunState(source=source, destination=source / "out", result=result)
    DiscoveryStep().run(state, context=PipelineContext.from_config(config))
    return state


_EARLY = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp()
_LATE = datetime(2023, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp()


def _populate(root: Path) -> int:
    """Create a mixed-media directory tree with known modification times.

    The image, video and audio files hold real content (the committed
    fixtures), so content detection types them as their names say.

    Args:
        root: Directory to populate.

    Returns:
        The total size in bytes of the four files.
    """
    (root / "sub").mkdir()
    image = root / "photo.jpg"
    video = root / "sub" / "clip.mp4"
    audio = root / "song.mp3"
    unknown = root / "notes.txt"
    shutil.copyfile(FIXTURE_DIR / "sample.jpg", image)
    shutil.copyfile(FIXTURE_DIR / "sample.mp4", video)
    shutil.copyfile(FIXTURE_DIR / "sample.mp3", audio)
    unknown.write_text("d")
    os.utime(image, (_EARLY, _EARLY))
    os.utime(video, (_LATE, _LATE))
    os.utime(audio, (_EARLY, _EARLY))
    os.utime(unknown, (_EARLY, _EARLY))
    return sum(path.stat().st_size for path in (image, video, audio, unknown))


def test_iter_regular_files_skips_directories_and_symlinks(tmp_path: Path) -> None:
    """Only regular files are yielded; directories and symlinks are skipped."""
    _populate(tmp_path)
    (tmp_path / "link.jpg").symlink_to(tmp_path / "photo.jpg")

    result = list(iter_regular_files(tmp_path))

    assert_that(result).contains_only(
        tmp_path / "photo.jpg",
        tmp_path / "sub" / "clip.mp4",
        tmp_path / "song.mp3",
        tmp_path / "notes.txt",
    )


def test_iter_regular_files_non_recursive_stays_top_level(tmp_path: Path) -> None:
    """Non-recursive iteration ignores files in subdirectories."""
    _populate(tmp_path)

    result = list(iter_regular_files(tmp_path, recursive=False))

    assert_that(result).does_not_contain(tmp_path / "sub" / "clip.mp4")
    assert_that(result).is_length(3)


def test_collect_directory_stats_aggregates_recursively(tmp_path: Path) -> None:
    """Recursive stats count every file and classify by media type."""
    total_bytes = _populate(tmp_path)

    result = collect_directory_stats(tmp_path, recursive=True)

    assert_that(result.total_files).is_equal_to(4)
    assert_that(result.total_bytes).is_equal_to(total_bytes)
    assert_that(result.counts_by_type.get(MediaType.IMAGE)).is_equal_to(1)
    assert_that(result.counts_by_type.get(MediaType.VIDEO)).is_equal_to(1)
    assert_that(result.counts_by_type.get(MediaType.AUDIO)).is_equal_to(1)
    assert_that(result.unknown_count).is_equal_to(1)


def test_collect_directory_stats_tracks_modification_span(tmp_path: Path) -> None:
    """Stats capture the earliest and latest modification times."""
    _populate(tmp_path)

    result = collect_directory_stats(tmp_path, recursive=True)

    assert_that(result.earliest_modified).is_equal_to(
        datetime.fromtimestamp(_EARLY, tz=UTC),
    )
    assert_that(result.latest_modified).is_equal_to(
        datetime.fromtimestamp(_LATE, tz=UTC),
    )


def test_collect_directory_stats_respects_non_recursive(tmp_path: Path) -> None:
    """Non-recursive stats ignore files in subdirectories."""
    _populate(tmp_path)

    result = collect_directory_stats(tmp_path, recursive=False)

    assert_that(result.total_files).is_equal_to(3)
    assert_that(result.counts_by_type.get(MediaType.VIDEO)).is_none()


def test_collect_directory_stats_handles_empty_directory(tmp_path: Path) -> None:
    """An empty directory yields zeroed counts and no date span."""
    result = collect_directory_stats(tmp_path)

    assert_that(result.total_files).is_equal_to(0)
    assert_that(result.total_bytes).is_equal_to(0)
    assert_that(result.unknown_count).is_equal_to(0)
    assert_that(result.counts_by_type).is_empty()
    assert_that(result.earliest_modified).is_none()
    assert_that(result.latest_modified).is_none()


def test_inspect_file_reads_image_metadata(fixtures_dir: Path) -> None:
    """A decodable image reports its media type and structural metadata."""
    file_info = inspect_file(fixtures_dir / "sample.png")

    assert_that(file_info.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(file_info.extension).is_equal_to("png")
    assert_that(file_info.size_bytes).is_greater_than(0)
    metadata = file_info.metadata
    assert_that(metadata).is_not_none()
    if metadata is None:
        pytest.fail("expected image metadata for sample.png")
    assert_that(metadata.width).is_greater_than(0)
    assert_that(metadata.image_format).is_equal_to("PNG")


def test_inspect_file_reports_timestamps_in_utc(tmp_path: Path) -> None:
    """Modification time is read from ``stat`` and expressed in UTC."""
    target = tmp_path / "notes.txt"
    target.write_text("payload")
    os.utime(target, (_EARLY, _EARLY))

    file_info = inspect_file(target)

    assert_that(file_info.modified).is_equal_to(datetime.fromtimestamp(_EARLY, tz=UTC))
    assert_that(file_info.modified.tzinfo).is_equal_to(UTC)
    assert_that(file_info.size_bytes).is_equal_to(7)


def test_inspect_file_text_file_has_no_media_type(tmp_path: Path) -> None:
    """An unrecognized extension yields no media type and no metadata."""
    target = tmp_path / "notes.txt"
    target.write_text("payload")

    file_info = inspect_file(target)

    assert_that(file_info.media_type).is_none()
    assert_that(file_info.extension).is_equal_to("txt")
    assert_that(file_info.metadata).is_none()


def test_inspect_file_text_named_as_video_is_unknown(tmp_path: Path) -> None:
    """A text file named ``.mp4`` is not video and carries no metadata."""
    target = tmp_path / "clip.mp4"
    target.write_text("0123456789")

    file_info = inspect_file(target)

    assert_that(file_info.media_type).is_none()
    assert_that(file_info.size_bytes).is_equal_to(10)
    assert_that(file_info.metadata).is_none()


def test_inspect_file_corrupt_image_degrades_to_none(fixtures_dir: Path) -> None:
    """An undecodable image still classifies as an image with no metadata."""
    file_info = inspect_file(fixtures_dir / "corrupt.jpg")

    assert_that(file_info.media_type).is_equal_to(MediaType.IMAGE)
    assert_that(file_info.metadata).is_none()


# --- Content detection, walk policy and read errors (#366) -------------------


def _write_jpeg(path: Path, *, size: tuple[int, int] = (8, 8)) -> Path:
    """Write a real JPEG to ``path`` whatever its name says.

    Args:
        path: Destination file; its suffix is ignored by the encoder.
        size: Image width and height in pixels.

    Returns:
        The written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 60, 30)).save(path, format="JPEG")
    return path


def test_collect_directory_stats_counts_renamed_jpeg_as_image(tmp_path: Path) -> None:
    """A JPEG named ``.bin`` is an image; its extension gives no type."""
    _write_jpeg(tmp_path / "IMG_0001.bin")

    result = collect_directory_stats(tmp_path)

    assert_that(result.counts_by_type.get(MediaType.IMAGE)).is_equal_to(1)
    assert_that(result.unknown_count).is_equal_to(0)
    assert_that(result.extension_untyped_count).is_equal_to(1)
    assert_that(result.extension_mismatch_count).is_equal_to(0)


def test_collect_directory_stats_counts_extensionless_jpeg_as_image(
    tmp_path: Path,
) -> None:
    """A JPEG with no suffix at all is still counted as an image."""
    _write_jpeg(tmp_path / "noext_jpeg")

    result = collect_directory_stats(tmp_path)

    assert_that(result.counts_by_type.get(MediaType.IMAGE)).is_equal_to(1)
    assert_that(result.unknown_count).is_equal_to(0)
    assert_that(result.extension_untyped_count).is_equal_to(1)


def test_collect_directory_stats_counts_text_named_mp4_as_unknown(
    tmp_path: Path,
) -> None:
    """A text file named ``.mp4`` is not counted as video."""
    (tmp_path / "notes.mp4").write_text("just text\n")

    result = collect_directory_stats(tmp_path)

    assert_that(result.counts_by_type.get(MediaType.VIDEO)).is_none()
    assert_that(result.unknown_count).is_equal_to(1)
    assert_that(result.total_files).is_equal_to(1)


def test_collect_directory_stats_counts_extension_mismatch(tmp_path: Path) -> None:
    """A JPEG named ``.mp3`` is an image and counted as a mismatch."""
    _write_jpeg(tmp_path / "photo.mp3")

    result = collect_directory_stats(tmp_path)

    assert_that(result.counts_by_type).is_equal_to({MediaType.IMAGE: 1})
    assert_that(result.extension_mismatch_count).is_equal_to(1)
    assert_that(result.extension_untyped_count).is_equal_to(0)


def test_collect_directory_stats_counts_file_that_vanishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file deleted between enumeration and ``stat`` is counted unreadable."""
    _write_jpeg(tmp_path / "a.jpg")
    doomed = _write_jpeg(tmp_path / "b.jpg")
    real_walk = inventory.iter_regular_files

    def vanishing_walk(*args: Any, **kwargs: Any) -> Iterator[Path]:
        for path in real_walk(*args, **kwargs):
            if path == doomed:
                path.unlink()
            yield path

    monkeypatch.setattr(inventory, "iter_regular_files", vanishing_walk)

    result = collect_directory_stats(tmp_path)

    assert_that(result.total_files).is_equal_to(1)
    assert_that(result.counts_by_type).is_equal_to({MediaType.IMAGE: 1})
    assert_that(result.unreadable_count).is_equal_to(1)
    assert_that(result.unreadable_examples).is_equal_to((doomed,))
    assert_that(result.complete).is_false()


def test_collect_directory_stats_counts_unreadable_subtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory that cannot be listed is counted once; its files are not."""
    _write_jpeg(tmp_path / "a.jpg")
    locked = tmp_path / "locked"
    _write_jpeg(locked / "inside.jpg")
    _write_jpeg(locked / "deeper" / "more.jpg")
    _deny_scandir(monkeypatch, locked)

    result = collect_directory_stats(tmp_path)

    assert_that(result.total_files).is_equal_to(1)
    assert_that(result.counts_by_type).is_equal_to({MediaType.IMAGE: 1})
    assert_that(result.unreadable_directory_count).is_equal_to(1)
    assert_that(result.unreadable_directory_examples).is_equal_to((locked,))
    assert_that(result.unreadable_count).is_equal_to(0)
    assert_that(result.complete).is_false()


def test_collect_directory_stats_counts_unreadable_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose content cannot be read is unreadable, not typed by name."""
    _write_jpeg(tmp_path / "a.jpg")
    blocked = _write_jpeg(tmp_path / "blocked.jpg")
    real_detect = detect

    def failing_detect(path: Path, **kwargs: Any) -> DetectionResult:
        if path == blocked:
            return real_detect(tmp_path / "missing.jpg", **kwargs)
        return real_detect(path, **kwargs)

    monkeypatch.setattr(inventory, "detect", failing_detect)

    result = collect_directory_stats(tmp_path)

    assert_that(result.counts_by_type).is_equal_to({MediaType.IMAGE: 1})
    assert_that(result.unreadable_count).is_equal_to(1)
    assert_that(result.unreadable_examples).is_equal_to((blocked,))


def test_collect_directory_stats_caps_unreadable_examples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most ten unreadable examples are kept, the first ten by path."""
    names = [f"f{index:02d}.jpg" for index in range(12)]
    for name in names:
        _write_jpeg(tmp_path / name)

    def vanishing_walk(*args: Any, **kwargs: Any) -> Iterator[Path]:
        for path in reversed(sorted(tmp_path.iterdir())):
            path.unlink()
            yield path

    monkeypatch.setattr(inventory, "iter_regular_files", vanishing_walk)

    result = collect_directory_stats(tmp_path)

    assert_that(result.unreadable_count).is_equal_to(12)
    assert_that(result.unreadable_examples).is_equal_to(
        tuple(tmp_path / name for name in names[:10]),
    )


def test_collect_directory_stats_counts_heic_fixtures_as_images(
    fixtures_dir: Path,
) -> None:
    """The committed fixtures are counted by content, HEIC as image."""
    result = collect_directory_stats(fixtures_dir, recursive=False)

    assert_that(dict(result.counts_by_type)).is_equal_to(
        {MediaType.IMAGE: 10, MediaType.VIDEO: 1, MediaType.AUDIO: 4},
    )
    assert_that(result.unknown_count).is_equal_to(1)
    assert_that(result.extension_mismatch_count).is_equal_to(0)
    assert_that(result.extension_untyped_count).is_equal_to(0)


@pytest.mark.parametrize("use_mime_fallback", [True, False])
def test_collect_directory_stats_types_media_whatever_the_name_fallback(
    tmp_path: Path,
    *,
    use_mime_fallback: bool,
) -> None:
    """MIDI, CAF and MPEG-PS are counted by content with or without MIME names.

    With ``use_mime_fallback=False`` the names ``.mid``, ``.caf`` and ``.vob``
    give no type (the behaviour #464 makes the default); the counts must not
    change.
    """
    (tmp_path / "tune.mid").write_bytes(_MIDI)
    (tmp_path / "loop.caf").write_bytes(_CAF)
    (tmp_path / "movie.vob").write_bytes(_VOB)
    registry = create_default_format_registry(use_mime_fallback=use_mime_fallback)

    result = collect_directory_stats(tmp_path, registry=registry)

    assert_that(dict(result.counts_by_type)).is_equal_to(
        {MediaType.AUDIO: 2, MediaType.VIDEO: 1},
    )
    assert_that(result.unknown_count).is_equal_to(0)
    assert_that(result.extension_mismatch_count).is_equal_to(0)
    assert_that(result.extension_untyped_count).is_equal_to(
        0 if use_mime_fallback else 3,
    )


def test_collect_directory_stats_fast_is_extension_only(tmp_path: Path) -> None:
    """``fast=True`` classifies by name and computes no extension checks."""
    _write_jpeg(tmp_path / "IMG_0001.bin")
    (tmp_path / "notes.mp4").write_text("just text\n")

    result = collect_directory_stats(tmp_path, fast=True)

    assert_that(result.classified_by).is_equal_to(Classification.EXTENSION)
    assert_that(dict(result.counts_by_type)).is_equal_to({MediaType.VIDEO: 1})
    assert_that(result.unknown_count).is_equal_to(1)
    assert_that(result.extension_mismatch_count).is_none()
    assert_that(result.extension_untyped_count).is_none()


def test_collect_directory_stats_detects_once_per_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content detection runs exactly once for each counted file."""
    _write_jpeg(tmp_path / "a.jpg")
    _write_jpeg(tmp_path / "sub" / "b.bin")
    calls = _spy_detect(monkeypatch)

    collect_directory_stats(tmp_path)

    assert_that(sorted(calls)).is_equal_to(
        [tmp_path / "a.jpg", tmp_path / "sub" / "b.bin"],
    )


def test_collect_directory_stats_reports_skipped_entries(tmp_path: Path) -> None:
    """Hidden, excluded and symlinked entries are skipped with a reason."""
    _write_jpeg(tmp_path / "a.jpg")
    _write_jpeg(tmp_path / ".winnow-backups" / "x.jpg")
    _write_jpeg(tmp_path / "draft_1.jpg")
    (tmp_path / "link.jpg").symlink_to(tmp_path / "a.jpg")
    policy = WalkPolicy(exclude_patterns=("draft_*",))

    result = collect_directory_stats(tmp_path, policy=policy)

    assert_that(result.total_files).is_equal_to(1)
    assert_that(dict(result.skipped_by_reason)).is_equal_to(
        {
            WalkEventKind.HIDDEN: 1,
            WalkEventKind.EXCLUDED: 1,
            WalkEventKind.SYMLINK: 1,
        },
    )
    assert_that(result.complete).is_true()


def test_iter_inventory_follow_counts_a_linked_file_once(tmp_path: Path) -> None:
    """Under FOLLOW a symlink to a counted file is skipped as a duplicate."""
    _write_jpeg(tmp_path / "a.jpg")
    (tmp_path / "b_link.jpg").symlink_to(tmp_path / "a.jpg")
    events: list[WalkEvent] = []
    policy = WalkPolicy(symlink_policy=SymlinkPolicy.FOLLOW)

    entries = list(iter_inventory(tmp_path, policy=policy, on_event=events.append))

    assert_that([entry.path for entry in entries]).is_equal_to([tmp_path / "a.jpg"])
    assert_that([event.kind for event in events]).is_equal_to(
        [WalkEventKind.DUPLICATE],
    )
    assert_that(events[0].path).is_equal_to(tmp_path / "b_link.jpg")


def test_inspect_file_detects_renamed_jpeg(tmp_path: Path) -> None:
    """A JPEG named ``.bin`` is inspected as an image with its dimensions."""
    target = _write_jpeg(tmp_path / "IMG_0001.bin", size=(8, 6))

    file_info = inspect_file(target)

    assert_that(file_info.media_type).is_equal_to(MediaType.IMAGE)
    metadata = file_info.metadata
    if metadata is None:
        pytest.fail("expected image metadata for a renamed JPEG")
    assert_that((metadata.width, metadata.height)).is_equal_to((8, 6))
    detection = file_info.detection
    if detection is None:
        pytest.fail("expected a detection result")
    assert_that(extension_check(detection)).is_equal_to(
        ExtensionCheck.EXTENSION_UNTYPED,
    )


def test_inspect_file_detects_once(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspecting a file reads its content for detection exactly once."""
    calls = _spy_detect(monkeypatch)
    service_calls: list[Path] = []
    real_service_detect = detect_media_type

    def counting_service_detect(path: Path, **kwargs: Any) -> MediaType | None:
        service_calls.append(path)
        return real_service_detect(path, **kwargs)

    monkeypatch.setattr(service_module, "detect_media_type", counting_service_detect)

    inspect_file(fixtures_dir / "sample.png")

    assert_that(calls).is_equal_to([fixtures_dir / "sample.png"])
    assert_that(service_calls).is_empty()


def test_inspect_file_extracts_video_metadata(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Video metadata comes from the video extractor for the detected type."""
    seen: list[Path] = []

    def fake_video(path: Path) -> MediaMetadata:
        seen.append(path)
        return MediaMetadata(duration_seconds=2.5, codec="h264")

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {**service_module._EXTRACTORS, MediaType.VIDEO: fake_video},
    )

    file_info = inspect_file(fixtures_dir / "sample.mp4")

    assert_that(file_info.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(seen).is_equal_to([fixtures_dir / "sample.mp4"])
    metadata = file_info.metadata
    if metadata is None:
        pytest.fail("expected video metadata")
    assert_that(metadata.duration_seconds).is_equal_to(2.5)
    assert_that(metadata.codec).is_equal_to("h264")


def test_inspect_file_metadata_error_degrades_to_none(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata extractor that fails leaves the type and drops the metadata."""

    def failing_audio(path: Path) -> MediaMetadata:
        raise MediaError("boom", operation="extract", file_path=path)

    monkeypatch.setattr(
        service_module,
        "_EXTRACTORS",
        {**service_module._EXTRACTORS, MediaType.AUDIO: failing_audio},
    )

    file_info = inspect_file(fixtures_dir / "sample.mp3")

    assert_that(file_info.media_type).is_equal_to(MediaType.AUDIO)
    assert_that(file_info.metadata).is_none()


def test_inspect_file_wraps_stat_error(tmp_path: Path) -> None:
    """A file that is gone when inspected raises ``MediaError``."""
    missing = tmp_path / "gone.jpg"

    with pytest.raises(MediaError) as caught:
        inspect_file(missing)

    assert_that(caught.value.context.operation).is_equal_to("inspect_file")
    assert_that(caught.value.context.file_path).is_equal_to(missing)


def test_inspect_file_rejects_unreadable_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose content cannot be read is an error, not typed by name."""
    target = _write_jpeg(tmp_path / "a.jpg")
    real_detect = detect
    monkeypatch.setattr(
        inventory,
        "detect",
        lambda path, **kwargs: real_detect(tmp_path / "missing.jpg", **kwargs),
    )

    with pytest.raises(MediaError) as caught:
        inspect_file(target)

    assert_that(caught.value.context.operation).is_equal_to("inspect_file")


def test_inspect_file_rejects_non_regular_file(tmp_path: Path) -> None:
    """A FIFO is reported as not a regular file and never opened."""
    fifo = tmp_path / "pipe.jpg"
    os.mkfifo(fifo)

    with pytest.raises(MediaError) as caught:
        inspect_file(fifo)

    assert_that(caught.value.message).contains("not a regular file")


def test_inspect_file_fast_uses_extension_only(tmp_path: Path) -> None:
    """``fast=True`` types by name and records no detection."""
    target = _write_jpeg(tmp_path / "IMG_0001.bin")

    file_info = inspect_file(target, fast=True)

    assert_that(file_info.media_type).is_none()
    assert_that(file_info.detection).is_none()


@pytest.mark.parametrize(
    ("name", "write", "expected"),
    [
        ("a.jpg", "jpeg", ExtensionCheck.MATCH),
        ("a.mp3", "jpeg", ExtensionCheck.MISMATCH),
        ("a.bin", "jpeg", ExtensionCheck.EXTENSION_UNTYPED),
        ("a.mp4", "text", ExtensionCheck.CONTENT_UNKNOWN),
        ("a.txt", "text", ExtensionCheck.CONTENT_UNKNOWN),
    ],
)
def test_extension_check_states(
    tmp_path: Path,
    *,
    name: str,
    write: str,
    expected: ExtensionCheck,
) -> None:
    """Each pairing of content and extension maps to one check state."""
    target = tmp_path / name
    if write == "jpeg":
        _write_jpeg(target)
    else:
        target.write_text("just text\n")

    assert_that(extension_check(detect(target))).is_equal_to(expected)


def test_stats_walk_matches_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stats and Discovery count the same media; every skip has a reason."""
    root = tmp_path / "root"
    _write_jpeg(root / "a.jpg")
    _write_jpeg(root / "sub" / "b.bin")
    _write_jpeg(root / "linked_target" / "t.jpg")
    (root / "notes.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text("not media\n")
    (root / "file_link.jpg").symlink_to(root / "a.jpg")
    (root / "dir_link").symlink_to(root / "linked_target")
    _write_jpeg(root / ".winnow-backups" / "x.jpg")
    _write_jpeg(root / "skip_me.jpg")
    locked = root / "locked"
    _write_jpeg(locked / "inside.jpg")
    gone = _write_jpeg(root / "gone.jpg")
    _deny_scandir(monkeypatch, locked, vanish=gone)
    config = WinnowConfig(paths=PathSettings(exclude_patterns=["skip_*"]))

    state = _discover(root, config=config)
    _write_jpeg(gone)
    events: list[WalkEvent] = []
    entries = list(
        iter_inventory(
            root,
            policy=WalkPolicy.from_config(config),
            on_event=events.append,
        ),
    )
    _write_jpeg(gone)
    stats = collect_directory_stats(root, policy=WalkPolicy.from_config(config))

    discovered = {media.path for media in state.files}
    counted = {entry.path.resolve() for entry in entries if entry.media_type}
    assert_that(counted).is_equal_to(discovered)
    assert_that(discovered).is_length(3)
    assert_that(stats.unknown_count).is_equal_to(1)
    assert_that(dict(stats.skipped_by_reason)).is_equal_to(
        {
            WalkEventKind.HIDDEN: 1,
            WalkEventKind.EXCLUDED: 1,
            WalkEventKind.SYMLINK: 2,
        },
    )
    assert_that(stats.unreadable_directory_count).is_equal_to(1)
    assert_that(stats.unreadable_count).is_equal_to(1)
    assert_that(state.result.errors).is_length(2)
    issue_paths = sorted(
        event.path
        for event in events
        if event.kind
        in {WalkEventKind.UNREADABLE_DIRECTORY, WalkEventKind.UNREADABLE_ENTRY}
    )
    assert_that(issue_paths).is_equal_to(sorted([gone, locked]))


def test_collect_directory_stats_skips_non_regular_files(tmp_path: Path) -> None:
    """A FIFO is skipped as not a regular file and never opened."""
    _write_jpeg(tmp_path / "a.jpg")
    os.mkfifo(tmp_path / "pipe.jpg")

    result = collect_directory_stats(tmp_path)

    assert_that(result.total_files).is_equal_to(1)
    assert_that(dict(result.skipped_by_reason)).is_equal_to(
        {WalkEventKind.NOT_REGULAR: 1},
    )


_MATCHING_FIXTURES = (
    "dated.heic",
    "dated.jpg",
    "sample.bmp",
    "sample.flac",
    "sample.gif",
    "sample.heic",
    "sample.jpg",
    "sample.mp3",
    "sample.mp4",
    "sample.ogg",
    "sample.png",
    "sample.tiff",
    "sample.wav",
    "sample.webp",
)
"""Fixtures whose content and extension give the same media type."""


def test_collect_directory_stats_equals_extension_counts_for_matching_media(
    tmp_path: Path,
) -> None:
    """Media whose extensions match their content count the same both ways."""
    for name in _MATCHING_FIXTURES:
        shutil.copyfile(FIXTURE_DIR / name, tmp_path / name)

    by_content = collect_directory_stats(tmp_path)
    by_name = collect_directory_stats(tmp_path, fast=True)

    expected = {MediaType.IMAGE: 9, MediaType.VIDEO: 1, MediaType.AUDIO: 4}
    assert_that(dict(by_content.counts_by_type)).is_equal_to(expected)
    assert_that(dict(by_name.counts_by_type)).is_equal_to(expected)
    assert_that(by_content.unknown_count).is_equal_to(0)
    assert_that(by_content.extension_mismatch_count).is_equal_to(0)
    assert_that(by_content.total_bytes).is_equal_to(by_name.total_bytes)


def test_collect_directory_stats_documented_corrections(tmp_path: Path) -> None:
    """Audio-only MP4 and WebM are audio; header-only files are unknown.

    These are the only differences from extension counting: the extension
    says video for the two audio-only containers and image or video for the
    two files holding no media header.
    """
    shutil.copyfile(FIXTURE_DIR / "audio" / "aac_frag.mp4", tmp_path / "voice.mp4")
    (tmp_path / "voice.webm").write_bytes(
        matroska(tracks(MKV_AUDIO), cluster(), doctype=b"webm"),
    )
    (tmp_path / "photo.jpg").write_text("aa")
    (tmp_path / "clip.mp4").write_text("bbbb")

    by_content = collect_directory_stats(tmp_path)
    by_name = collect_directory_stats(tmp_path, fast=True)

    assert_that(dict(by_content.counts_by_type)).is_equal_to({MediaType.AUDIO: 2})
    assert_that(by_content.unknown_count).is_equal_to(2)
    assert_that(by_content.extension_mismatch_count).is_equal_to(2)
    assert_that(dict(by_name.counts_by_type)).is_equal_to(
        {MediaType.VIDEO: 3, MediaType.IMAGE: 1},
    )
    assert_that(by_name.unknown_count).is_equal_to(0)
