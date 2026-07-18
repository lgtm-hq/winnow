"""Tests for the audio metadata and tag helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that
from tinytag import TinyTag, TinyTagException

from winnow.exceptions import MediaError
from winnow.media import audio as audio_module
from winnow.media.audio import extract_audio_metadata, read_audio_tags

_AUDIO_CODECS: list[tuple[str, str]] = [
    ("sample.mp3", "mp3"),
    ("sample.flac", "flac"),
    ("sample.ogg", "vorbis"),
    ("sample.wav", "pcm"),
]


@pytest.mark.parametrize(
    ("filename", "expected_codec"),
    _AUDIO_CODECS,
    ids=[name for name, _ in _AUDIO_CODECS],
)
def test_extract_audio_metadata_reads_stream_info(
    fixtures_dir: Path,
    filename: str,
    expected_codec: str,
) -> None:
    """Each supported container yields duration, codec, and channel info."""
    metadata = extract_audio_metadata(fixtures_dir / filename)

    assert_that(metadata.codec).is_equal_to(expected_codec)
    assert_that(metadata.duration_seconds).is_greater_than(0)
    assert_that(metadata.sample_rate).is_greater_than(0)
    assert_that(metadata.channels).is_greater_than(0)


@pytest.mark.parametrize(
    "filename",
    ["sample.mp3", "sample.flac", "sample.ogg", "sample.wav"],
)
def test_read_audio_tags_returns_common_fields(
    fixtures_dir: Path,
    filename: str,
) -> None:
    """Tag reading normalizes title and artist across formats."""
    tags = read_audio_tags(fixtures_dir / filename)

    assert_that(tags).contains_key("title")
    assert_that(tags["title"]).is_equal_to("Winnow Sample")
    assert_that(tags["artist"]).is_equal_to("Winnow")


def test_extract_audio_metadata_missing_file() -> None:
    """A missing audio path raises MediaError."""
    with pytest.raises(MediaError):
        extract_audio_metadata(Path("/nonexistent/song.mp3"))


def test_extract_audio_metadata_falls_back_to_tinytag(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mutagen cannot read a file, tinytag provides metadata."""
    monkeypatch.setattr(
        audio_module,
        "_metadata_from_mutagen",
        lambda *, path: None,
    )

    metadata = extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(metadata.sample_rate).is_equal_to(8000)
    assert_that(metadata.channels).is_equal_to(1)
    assert_that(metadata.bitrate).is_greater_than(0)


def test_extract_audio_metadata_unsupported_raises(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both backends fail, extraction raises MediaError."""
    monkeypatch.setattr(audio_module, "_metadata_from_mutagen", lambda *, path: None)
    monkeypatch.setattr(audio_module, "_metadata_from_tinytag", lambda *, path: None)

    with pytest.raises(MediaError):
        extract_audio_metadata(fixtures_dir / "sample.wav")


def test_read_audio_tags_normalizes_year_and_lowercases_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mutagen keys are lowercased and the date field is aliased to year."""
    fake = SimpleNamespace(tags={"TITLE": ["Song"], "date": ["2024"]})
    monkeypatch.setattr(
        "winnow.media.audio.mutagen_open",
        lambda path, easy=False: fake,
    )

    tags = read_audio_tags(Path("does-not-matter.mp3"))

    assert_that(tags).contains_key("year")
    assert_that(tags["year"]).is_equal_to("2024")
    assert_that(tags).contains_key("title")
    assert_that(tags).does_not_contain_key("date")
    assert_that(tags).does_not_contain_key("TITLE")


def test_read_audio_tags_falls_back_to_tinytag(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mutagen returns no tags, tinytag supplies them."""
    monkeypatch.setattr(audio_module, "_tags_from_mutagen", lambda *, path: {})

    tags = read_audio_tags(fixtures_dir / "sample.mp3")

    assert_that(tags["title"]).is_equal_to("Winnow Sample")


def test_read_audio_tags_empty_when_both_backends_empty(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty result is returned when neither backend finds tags."""
    monkeypatch.setattr(audio_module, "_tags_from_mutagen", lambda *, path: {})
    monkeypatch.setattr(audio_module, "_tags_from_tinytag", lambda *, path: {})

    tags = read_audio_tags(fixtures_dir / "sample.mp3")

    assert_that(tags).is_empty()


def test_tinytag_tag_reader_handles_unreadable(tmp_path: Path) -> None:
    """The tinytag tag reader degrades to an empty mapping on failure."""
    garbage = tmp_path / "broken.mp3"
    garbage.write_bytes(b"not-audio")

    tags = audio_module._tags_from_tinytag(path=garbage)

    assert_that(tags).is_empty()


def test_mutagen_metadata_handles_unreadable(tmp_path: Path) -> None:
    """The mutagen metadata reader returns None on unreadable input."""
    garbage = tmp_path / "broken.flac"
    garbage.write_bytes(b"not-audio")

    result = audio_module._metadata_from_mutagen(path=garbage)

    assert_that(result).is_none()


def test_tinytag_metadata_handles_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tinytag metadata reader returns None when tinytag raises."""
    garbage = tmp_path / "broken.mp3"
    garbage.write_bytes(b"not-audio")

    def _raise(_path: Path) -> None:
        raise TinyTagException("cannot parse")

    monkeypatch.setattr(TinyTag, "get", staticmethod(_raise))

    result = audio_module._metadata_from_tinytag(path=garbage)

    assert_that(result).is_none()


def test_mutagen_tags_handles_unreadable(tmp_path: Path) -> None:
    """The mutagen tag reader degrades to an empty mapping on failure."""
    garbage = tmp_path / "broken.ogg"
    garbage.write_bytes(b"not-audio")

    tags = audio_module._tags_from_mutagen(path=garbage)

    assert_that(tags).is_empty()


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", object()],
    ids=["none", "string", "object"],
)
def test_non_negative_int_rejects_invalid(value: object) -> None:
    """Non-numeric or missing values coerce to None for integers."""
    assert_that(audio_module._non_negative_int(value)).is_none()


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", object()],
    ids=["none", "string", "object"],
)
def test_non_negative_float_rejects_invalid(value: object) -> None:
    """Non-numeric or missing values coerce to None for floats."""
    assert_that(audio_module._non_negative_float(value)).is_none()


def test_non_negative_int_rejects_negative() -> None:
    """Negative integers are rejected as None."""
    assert_that(audio_module._non_negative_int(-5)).is_none()
