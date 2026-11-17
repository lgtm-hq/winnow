"""Tests for the audio metadata and tag helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that
from tinytag import TinyTag, TinyTagException

from tests.media.audio_samples import write_ffmpeg_sample, write_id3_sample
from winnow.exceptions import MediaError
from winnow.media import audio as audio_module
from winnow.media.audio import extract_audio_metadata, read_audio_tags
from winnow.models.media import MediaMetadata

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


# Field-by-field record of the audio backend (#400). Values were recorded from
# main (mutagen 1.48.1 primary, tinytag fallback) on ffmpeg 9.0.2 samples and
# on the committed fixtures. Durations compare to 2 ms and bitrates to 1 %, so
# a different ffmpeg build writing the same samples still matches.
@dataclass(frozen=True, slots=True)
class _AudioFields:
    duration: float
    bitrate: int | None
    sample_rate: int | None
    channels: int
    codec: str | None


_FFMPEG_EXPECTED: dict[str, _AudioFields] = {
    "mp3": _AudioFields(0.3396, 63983, 44100, 2, "mp3"),
    "flac": _AudioFields(0.3, 123786, 44100, 2, "flac"),
    "wav": _AudioFields(0.3, 128000, 8000, 1, "pcm"),
    "m4a-aac": _AudioFields(0.3232, 63807, 44100, 2, "aac"),
    "m4a-alac": _AudioFields(0.3, 1411200, 44100, 2, "aac"),
    "opus": _AudioFields(0.3, 91680, None, 2, "opus"),
    "aiff": _AudioFields(0.3, 128000, 8000, 1, "pcm"),
    "adts-aac": _AudioFields(0.325, 63442, 44100, 2, "aac"),
    "wav-adpcm": _AudioFields(0.00025, 32000, 8000, 1, "pcm"),
}

_FIXTURE_EXPECTED: dict[str, _AudioFields] = {
    "sample.mp3": _AudioFields(0.2351, 127978, 44100, 2, "mp3"),
    "sample.flac": _AudioFields(0.2, 1200, 44100, 2, "flac"),
    "sample.ogg": _AudioFields(0.2, 112000, 44100, 2, "vorbis"),
    "sample.wav": _AudioFields(0.2, 128000, 8000, 1, "pcm"),
}


def _assert_fields(metadata: MediaMetadata, expected: _AudioFields) -> None:
    """Compare extracted metadata with one recorded row."""
    assert_that(metadata.duration_seconds).is_not_none()
    assert_that(float(metadata.duration_seconds or 0.0)).is_close_to(
        expected.duration,
        0.002,
    )
    if expected.bitrate is None:
        assert_that(metadata.bitrate).is_none()
    else:
        assert_that(float(metadata.bitrate or 0)).is_close_to(
            float(expected.bitrate),
            expected.bitrate * 0.01,
        )
    assert_that(metadata.sample_rate).is_equal_to(expected.sample_rate)
    assert_that(metadata.channels).is_equal_to(expected.channels)
    assert_that(metadata.codec).is_equal_to(expected.codec)


@pytest.fixture(scope="module")
def ffmpeg_samples_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a directory the ffmpeg samples are written into on demand."""
    return tmp_path_factory.mktemp("audio-samples")


@pytest.mark.parametrize("key", sorted(_FFMPEG_EXPECTED))
def test_audio_fields_match_previous_backend(
    ffmpeg_samples_dir: Path,
    key: str,
) -> None:
    """ffmpeg-written samples give the recorded technical fields."""
    path = write_ffmpeg_sample(ffmpeg_samples_dir, key)

    _assert_fields(extract_audio_metadata(path), _FFMPEG_EXPECTED[key])


@pytest.mark.parametrize("filename", sorted(_FIXTURE_EXPECTED))
def test_audio_fields_match_previous_backend_on_fixtures(
    fixtures_dir: Path,
    filename: str,
) -> None:
    """The committed fixtures give the recorded technical fields."""
    metadata = extract_audio_metadata(fixtures_dir / filename)

    _assert_fields(metadata, _FIXTURE_EXPECTED[filename])


_ID3_CASES: dict[str, tuple[int, list[tuple[str, str]]]] = {
    "v2.4": (
        4,
        [
            ("TIT2", "Tone"),
            ("TDRC", "2019-07-04T10:10:10"),
            ("TDOR", "2001-02-03"),
            ("TDRL", "2002-03-04"),
        ],
    ),
    "v2.3": (
        3,
        [
            ("TIT2", "Tone"),
            ("TYER", "2019"),
            ("TDAT", "0407"),
            ("TIME", "1010"),
            ("TORY", "2001"),
        ],
    ),
    "v2.3-conflict": (3, [("TIT2", "Tone"), ("TYER", "2019"), ("TDRC", "2020-01-01")]),
    "v2.4-conflict": (
        4,
        [("TIT2", "Tone"), ("TDRC", "2019"), ("TYER", "2018"), ("TDOR", "2001")],
    ),
}

_ID3_EXPECTED_TAGS: dict[str, dict[str, str]] = {
    # mutagen upgrades v2.3 frames to v2.4 on load, so TYER+TDAT+TIME become
    # one date and TORY becomes TDOR; easy mode renames TDOR originaldate.
    "v2.4": {
        "title": "Tone",
        "year": "2019-07-04 10:10:10",
        "originaldate": "2001-02-03",
    },
    "v2.3": {"title": "Tone", "year": "2019-07-04 10:10:00", "originaldate": "2001"},
    "v2.3-conflict": {"title": "Tone", "year": "2020-01-01"},
    "v2.4-conflict": {"title": "Tone", "year": "2019", "originaldate": "2001"},
}


@pytest.mark.parametrize("case", sorted(_ID3_CASES))
def test_read_audio_tags_id3_dates(
    tmp_path: Path,
    fixtures_dir: Path,
    case: str,
) -> None:
    """ID3 date frames map to the recorded tag keys and values."""
    version, frames = _ID3_CASES[case]
    path = write_id3_sample(
        tmp_path / "dated.mp3",
        mp3_path=fixtures_dir / "sample.mp3",
        frames=frames,
        version=version,
    )

    assert_that(read_audio_tags(path)).is_equal_to(_ID3_EXPECTED_TAGS[case])


_FFMPEG_EXPECTED_TAGS: dict[str, dict[str, str]] = {
    "mp3": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
    },
    "flac": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
        "encoder": "<ffmpeg>",
    },
    "wav": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
    },
    "m4a-aac": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
    },
    "opus": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
        "encoder": "<ffmpeg>",
    },
    "aiff": {"title": "Tone"},
    "adts-aac": {},
}


@pytest.mark.parametrize("key", sorted(_FFMPEG_EXPECTED_TAGS))
def test_read_audio_tags_match_previous_backend(
    ffmpeg_samples_dir: Path,
    key: str,
) -> None:
    """ffmpeg-written samples give the recorded tag keys and values.

    ``<ffmpeg>`` stands for an encoder string that names the ffmpeg build.
    """
    path = write_ffmpeg_sample(ffmpeg_samples_dir, key)

    tags = read_audio_tags(path)

    expected = _FFMPEG_EXPECTED_TAGS[key]
    assert_that(sorted(tags)).is_equal_to(sorted(expected))
    for name, value in expected.items():
        if value == "<ffmpeg>":
            assert_that(tags[name]).matches(r"^Lav[cf]")
        else:
            assert_that(tags[name]).is_equal_to(value)
