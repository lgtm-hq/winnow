"""Tests for the audio metadata and tag helpers."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from importlib.metadata import version
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


def test_extract_audio_metadata_falls_back_to_ffprobe(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When tinytag cannot read a file, ffprobe provides metadata."""
    monkeypatch.setattr(audio_module, "_metadata_from_tinytag", lambda *, path: None)
    _fake_ffprobe(monkeypatch, _FFPROBE_WAV)

    metadata = extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(metadata.sample_rate).is_equal_to(8000)
    assert_that(metadata.channels).is_equal_to(1)
    assert_that(metadata.bitrate).is_greater_than(0)
    assert_that(metadata.codec).is_equal_to("pcm")


def test_extract_audio_metadata_unsupported_raises(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both backends fail, extraction raises MediaError."""
    monkeypatch.setattr(audio_module, "_metadata_from_tinytag", lambda *, path: None)
    monkeypatch.setattr(audio_module, "_metadata_from_ffprobe", lambda *, path: None)

    with pytest.raises(MediaError):
        extract_audio_metadata(fixtures_dir / "sample.wav")


def test_read_audio_tags_normalizes_year_and_lowercases_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tag keys are lowercased and the date field is aliased to year."""
    fake = SimpleNamespace(
        as_dict=lambda: {"TITLE": ["Song"], "date": ["2024"], "duration": 1.0},
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda _path: fake))

    tags = read_audio_tags(Path("does-not-matter.mp3"))

    assert_that(tags).contains_key("year")
    assert_that(tags["year"]).is_equal_to("2024")
    assert_that(tags).contains_key("title")
    assert_that(tags).does_not_contain_key("date")
    assert_that(tags).does_not_contain_key("TITLE")


def test_read_audio_tags_empty_when_tinytag_cannot_read(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty result is returned when tinytag cannot read the file."""
    monkeypatch.setattr(audio_module, "_read_tinytag", lambda _path: None)

    tags = read_audio_tags(fixtures_dir / "sample.mp3")

    assert_that(tags).is_empty()


def test_tinytag_tag_reader_handles_unreadable(tmp_path: Path) -> None:
    """The tinytag tag reader degrades to an empty mapping on failure."""
    garbage = tmp_path / "broken.mp3"
    garbage.write_bytes(b"not-audio")

    tags = audio_module._tags_from_tinytag(path=garbage)

    assert_that(tags).is_empty()


def test_tinytag_metadata_handles_unreadable(tmp_path: Path) -> None:
    """The tinytag metadata reader returns None on unreadable input."""
    garbage = tmp_path / "broken.flac"
    garbage.write_bytes(b"not-audio")

    result = audio_module._metadata_from_tinytag(path=garbage)

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


def test_read_audio_tags_handles_unreadable_ogg(tmp_path: Path) -> None:
    """Tag reading degrades to an empty mapping on an unreadable Ogg file."""
    garbage = tmp_path / "broken.ogg"
    garbage.write_bytes(b"not-audio")

    tags = read_audio_tags(garbage)

    assert_that(tags).is_empty()


# Field-by-field record of the audio backend (#400). Values were recorded from
# main (mutagen 1.48.1 primary, tinytag fallback) on ffmpeg 9.0.2 samples and
# on the committed fixtures. Durations compare to 2 ms and bitrates to 1 %, so
# a different ffmpeg build writing the same samples still matches. Rows marked
# "tinytag" or "ffprobe" differ from mutagen on purpose; the comment says why.
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
    # tinytag: 0.3 s, the length ffmpeg wrote; mutagen gave 0.3232 because it
    # counted the AAC priming samples.
    "m4a-aac": _AudioFields(0.3, 63807, 44100, 2, "aac"),
    # tinytag: the codec is ALAC; mutagen labelled every MP4 aac.
    "m4a-alac": _AudioFields(0.3, 1411200, 44100, 2, "alac"),
    # tinytag: bitrate 90 480 against mutagen's 91 680 (they average over
    # different spans), and tinytag knows Opus decodes at 48 kHz where mutagen
    # gave no sample rate.
    "opus": _AudioFields(0.3, 90480, 48000, 2, "opus"),
    "aiff": _AudioFields(0.3, 128000, 8000, 1, "pcm"),
    # ffprobe: tinytag cannot read raw ADTS, so ffprobe does. Its stream
    # bitrate is 65 853 against mutagen's 63 442 estimate; duration agrees.
    "adts-aac": _AudioFields(0.325, 65853, 44100, 2, "aac"),
    # tinytag + ffprobe: MS ADPCM in WAV. mutagen called it pcm and read the
    # duration as 0.25 ms; tinytag reads 0.3 s and ffprobe names the codec.
    "wav-adpcm": _AudioFields(0.3, 54613, 8000, 1, "adpcm_ms"),
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
    # mutagen upgraded v2.3 frames to v2.4 on load, merging TYER+TDAT+TIME into
    # one date and renaming TORY to TDOR, and kept one value per key. tinytag
    # keeps each frame as written: TYER and TDRC both feed year (joined when
    # both are present), and TDAT, TIME, TORY and TDRL keep their own keys.
    # TDOR keeps mutagen's name, originaldate.
    "v2.4": {
        "title": "Tone",
        "year": "2019-07-04T10:10:10",
        "originaldate": "2001-02-03",
        "tdrl": "2002-03-04",
    },
    "v2.3": {
        "title": "Tone",
        "year": "2019",
        "tdat": "0407",
        "time": "1010",
        "tory": "2001",
    },
    "v2.3-conflict": {"title": "Tone", "year": "2019, 2020-01-01"},
    "v2.4-conflict": {"title": "Tone", "year": "2019, 2018", "originaldate": "2001"},
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


# tinytag adds the encoder fields mutagen's easy mode left out: ID3 TSSE as
# encoder_settings, MP4 encoded_by, and the RIFF INFO ISFT software field.
_FFMPEG_EXPECTED_TAGS: dict[str, dict[str, str]] = {
    "mp3": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
        "encoder_settings": "<ffmpeg>",
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
        "isft": "<ffmpeg>",
    },
    "m4a-aac": {
        "title": "Tone",
        "artist": "Winnow",
        "album": "Samples",
        "year": "2019-07-04",
        "encoded_by": "<ffmpeg>",
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


# --- tinytag as the primary backend, ffprobe for the gaps (#400) --------------

_FFPROBE_WAV = json.dumps(
    {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "8000",
                "channels": 1,
                "bits_per_sample": 16,
                "duration": "0.200000",
                "bit_rate": "128000",
            },
        ],
        "format": {"duration": "0.200000", "bit_rate": "131413"},
    },
)


def _fake_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    returncode: int = 0,
) -> list[list[str]]:
    """Make ffprobe look installed and answer with ``stdout``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        stdout: JSON ffprobe prints.
        returncode: Exit status ffprobe returns.

    Returns:
        The list each ffprobe argv is appended to.
    """
    calls: list[list[str]] = []

    def _run(
        *,
        argv: list[str],
        timeout: float,
        operation: str,
        path: Path,
    ) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(audio_module, "_run", _run)
    return calls


def _no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ffprobe look missing."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)


def test_alac_codec_label(ffmpeg_samples_dir: Path) -> None:
    """ALAC in an M4A file is labelled alac, not aac."""
    path = write_ffmpeg_sample(ffmpeg_samples_dir, "m4a-alac")

    assert_that(extract_audio_metadata(path).codec).is_equal_to("alac")


def test_opus_reports_48k_sample_rate(ffmpeg_samples_dir: Path) -> None:
    """Opus reports the 48 kHz rate it always decodes at."""
    path = write_ffmpeg_sample(ffmpeg_samples_dir, "opus")

    assert_that(extract_audio_metadata(path).sample_rate).is_equal_to(48000)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_adts_aac_uses_ffprobe(ffmpeg_samples_dir: Path) -> None:
    """Raw ADTS AAC, which tinytag cannot read, is read by ffprobe."""
    path = write_ffmpeg_sample(ffmpeg_samples_dir, "adts-aac")

    assert_that(audio_module._metadata_from_tinytag(path=path)).is_none()
    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("aac")
    assert_that(metadata.duration_seconds).is_not_none()
    assert_that(float(metadata.duration_seconds or 0.0)).is_greater_than(0.3)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_compressed_wav_is_not_labelled_pcm(ffmpeg_samples_dir: Path) -> None:
    """A WAV holding MS ADPCM gets its real codec from ffprobe, not pcm."""
    path = write_ffmpeg_sample(ffmpeg_samples_dir, "wav-adpcm")

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("adpcm_ms")
    assert_that(metadata.bit_depth).is_equal_to(4)


def test_compressed_wav_without_ffprobe_has_no_codec(
    ffmpeg_samples_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ffprobe a compressed WAV keeps tinytag's fields and no codec."""
    path = write_ffmpeg_sample(ffmpeg_samples_dir, "wav-adpcm")
    _no_ffprobe(monkeypatch)

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_none()
    assert_that(metadata.sample_rate).is_equal_to(8000)
    assert_that(float(metadata.duration_seconds or 0.0)).is_close_to(0.3, 0.002)


def test_audio_without_ffprobe_degrades(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ffprobe, what tinytag reads is returned as is."""
    _no_ffprobe(monkeypatch)

    metadata = extract_audio_metadata(fixtures_dir / "sample.flac")

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.sample_rate).is_equal_to(44100)


def test_audio_without_any_backend_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file neither tinytag nor a missing ffprobe can read raises MediaError."""
    raw_aac = tmp_path / "stream.aac"
    raw_aac.write_bytes(b"\xff\xf1not-really-adts")
    _no_ffprobe(monkeypatch)

    with pytest.raises(MediaError):
        extract_audio_metadata(raw_aac)


def test_tinytag_exception_still_reaches_ffprobe(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported-format error from tinytag falls through to ffprobe."""

    def _raise(_path: Path) -> None:
        raise TinyTagException("No tag reader found to support file type")

    monkeypatch.setattr(TinyTag, "get", staticmethod(_raise))
    calls = _fake_ffprobe(monkeypatch, _FFPROBE_WAV)

    metadata = extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(calls).is_length(1)
    assert_that(metadata.codec).is_equal_to("pcm")
    assert_that(metadata.bit_depth).is_equal_to(16)


def test_ffprobe_fills_only_fields_tinytag_left_unknown(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known duration does not stop ffprobe filling a missing codec."""
    monkeypatch.setattr(
        audio_module,
        "_metadata_from_tinytag",
        lambda *, path: MediaMetadata(duration_seconds=0.25, channels=1),
    )
    _fake_ffprobe(monkeypatch, _FFPROBE_WAV)

    metadata = extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(metadata.duration_seconds).is_equal_to(0.25)
    assert_that(metadata.codec).is_equal_to("pcm")
    assert_that(metadata.sample_rate).is_equal_to(8000)


def test_ffprobe_failure_keeps_tinytag_fields(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ffprobe fails, the fields tinytag read are still returned."""
    monkeypatch.setattr(
        audio_module,
        "_metadata_from_tinytag",
        lambda *, path: MediaMetadata(duration_seconds=0.25, channels=1),
    )
    _fake_ffprobe(monkeypatch, "", returncode=1)

    metadata = extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(metadata.duration_seconds).is_equal_to(0.25)
    assert_that(metadata.codec).is_none()


def test_ffprobe_launch_failure_is_not_fatal(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ffprobe that cannot start or times out is treated as missing."""

    def _run(**_kwargs: object) -> None:
        raise MediaError("FFmpeg command timed out")

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(audio_module, "_run", _run)

    assert_that(
        audio_module._metadata_from_ffprobe(path=fixtures_dir / "sample.wav")
    ).is_none()


def test_zero_length_stream_is_kept(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parsed stream with zero duration is a real empty recording."""
    fake = SimpleNamespace(
        samplerate=44100,
        channels=2,
        duration=0.0,
        bitrate=0.0,
        bitdepth=16,
        mime_type="audio/flac",
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda _path: fake))
    _no_ffprobe(monkeypatch)

    metadata = extract_audio_metadata(fixtures_dir / "sample.flac")

    assert_that(metadata.duration_seconds).is_equal_to(0.0)
    assert_that(metadata.sample_rate).is_equal_to(44100)


def test_no_parsed_stream_is_not_a_result(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tinytag output without sample rate or channels is no stream at all."""
    fake = SimpleNamespace(
        samplerate=None,
        channels=None,
        duration=0.0,
        bitrate=None,
        bitdepth=None,
        mime_type="audio/mpeg",
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda _path: fake))

    result = audio_module._metadata_from_tinytag(path=fixtures_dir / "sample.mp3")

    assert_that(result).is_none()


def test_old_tinytag_without_mime_type_gets_codec_from_ffprobe(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tinytag before 2.3.0 had no stream MIME type; ffprobe fills the codec."""
    fake = SimpleNamespace(
        samplerate=8000,
        channels=1,
        duration=0.2,
        bitrate=128.0,
        bitdepth=16,
        mime_type=None,
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda _path: fake))
    _fake_ffprobe(monkeypatch, _FFPROBE_WAV)

    metadata = extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(metadata.codec).is_equal_to("pcm")


def _version_tuple(text: str) -> tuple[int, ...]:
    """Return the leading numeric release of a version string."""
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def test_tinytag_floor_has_stream_mime_type() -> None:
    """The declared tinytag floor is 2.3.0, the first with ``mime_type``."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    floors = [
        dependency.split(">=", 1)[1]
        for dependency in dependencies
        if dependency.startswith("tinytag>=")
    ]

    assert_that(floors).is_length(1)
    floor = _version_tuple(floors[0])
    assert_that(floor >= (2, 3, 0)).is_true()
    assert_that(_version_tuple(version("tinytag")) >= floor).is_true()
    assert_that(hasattr(TinyTag(), "mime_type")).is_true()


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ('audio/mp4; codecs="mp4a.40.2"', "aac"),
        ('audio/mp4; codecs="mp4a.40.5"', "aac"),
        ("audio/mp4; codecs=mp4a.67", "aac"),
        ('audio/mp4; codecs="alac"', "alac"),
        ('audio/mp4; codecs="mp4a.6B"', "mp3"),
        ('audio/mp4; codecs="fLaC"', "flac"),
        ('audio/mp4; codecs="Opus"', "opus"),
        ('audio/mp4; codecs="ec-3"', "eac3"),
        ('audio/mp4; codecs="avc1.64001f, mp4a.40.2"', "aac"),
        ("audio/mp4; foo=bar; codecs='alac'", "alac"),
        ("audio/mp4", None),
        ('audio/mp4; codecs="unknown"', None),
        ("audio/mpeg", "mp3"),
        ("audio/flac", "flac"),
        ("audio/x-flac", "flac"),
        ('audio/ogg; codecs="vorbis"', "vorbis"),
        ('audio/ogg; codecs="opus"', "opus"),
        ('audio/ogg; codecs="flac"', "flac"),
        ("audio/ogg", None),
        ('audio/wav; codecs="1"', "pcm"),
        ('audio/wav; codecs="3"', "pcm"),
        ('audio/x-wav; codecs="1"', "pcm"),
        ('audio/wav; codecs="2"', None),
        ('audio/wav; codecs="6"', None),
        ('audio/wav; codecs="55"', None),
        ('audio/wav; codecs="65534"', None),
        ("audio/wav", None),
        ('audio/aiff; codecs="NONE"', "pcm"),
        ('audio/x-aiff; codecs="sowt"', "pcm"),
        ('audio/aiff; codecs="fl32"', "pcm"),
        ('audio/aiff; codecs="raw "', "pcm"),
        ('audio/aiff; codecs="alaw"', None),
        ('audio/aiff; codecs="ima4"', None),
        ("", None),
        (None, None),
    ],
)
def test_codec_from_mime_type(mime_type: str | None, expected: str | None) -> None:
    """MIME types and codecs parameters map to winnow's codec labels."""
    assert_that(audio_module._codec_from_mime_type(mime_type)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("codec_name", "expected"),
    [
        ("pcm_s16le", "pcm"),
        ("pcm_f32be", "pcm"),
        ("pcm_alaw", "alaw"),
        ("pcm_mulaw", "mulaw"),
        ("adpcm_ms", "adpcm_ms"),
        ("aac", "aac"),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_codec_from_ffprobe(codec_name: object, expected: str | None) -> None:
    """ffprobe codec names map to winnow's codec labels."""
    assert_that(audio_module._codec_from_ffprobe(codec_name)).is_equal_to(expected)


def test_ffprobe_audio_ignores_attached_picture_stream() -> None:
    """Cover art stored as a video stream is not taken for the audio."""
    payload = json.dumps(
        {
            "streams": [
                {"codec_type": "video", "codec_name": "mjpeg", "duration": "9"},
                {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100"},
            ],
            "format": {"duration": "1.5", "bit_rate": "128000"},
        },
    )

    metadata = audio_module._parse_ffprobe_audio(payload)

    assert_that(metadata).is_equal_to(
        MediaMetadata(
            codec="mp3",
            duration_seconds=1.5,
            bitrate=128000,
            sample_rate=44100,
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        json.dumps({"streams": [{"codec_type": "video"}]}),
        json.dumps({"streams": "oops"}),
    ],
    ids=["invalid-json", "not-an-object", "no-audio-stream", "bad-streams"],
)
def test_ffprobe_audio_rejects_unusable_payloads(payload: str) -> None:
    """Payloads without an audio stream give no metadata."""
    assert_that(audio_module._parse_ffprobe_audio(payload)).is_none()


def test_ffprobe_audio_tolerates_missing_format() -> None:
    """A payload without a format object still yields the stream fields."""
    payload = json.dumps(
        {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": []},
    )

    metadata = audio_module._parse_ffprobe_audio(payload)

    assert_that(metadata).is_equal_to(MediaMetadata(codec="opus"))


def test_read_audio_tags_reads_wav_info(fixtures_dir: Path) -> None:
    """WAV RIFF INFO tags are returned."""
    tags = read_audio_tags(fixtures_dir / "sample.wav")

    assert_that(tags).contains_entry({"title": "Winnow Sample"})
    assert_that(tags).contains_entry({"artist": "Winnow"})
    assert_that(tags).contains_entry({"album": "Fixtures"})


def test_read_audio_tags_skips_aac_it_cannot_read(tmp_path: Path) -> None:
    """A format tinytag does not support gives no tags."""
    raw_aac = tmp_path / "stream.aac"
    raw_aac.write_bytes(b"\xff\xf1not-really-adts")

    assert_that(read_audio_tags(raw_aac)).is_empty()


def test_read_audio_tags_joins_multiple_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several values for one tag are joined, empty values dropped."""
    fake = SimpleNamespace(
        as_dict=lambda: {
            "artist": ["A", "B"],
            "track": 3,
            "comment": [""],
            "tracknumber": ["7"],
        },
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda _path: fake))

    tags = read_audio_tags(Path("song.mp3"))

    assert_that(tags).is_equal_to({"artist": "A, B", "track": "7"})


_ID3_TINYTAG_FIELDS: dict[str, dict[str, list[str]]] = {
    # What tinytag itself reports, before winnow's key aliases.
    "v2.4": {
        "year": ["2019-07-04T10:10:10"],
        "tdor": ["2001-02-03"],
        "tdrl": ["2002-03-04"],
    },
    "v2.3": {"year": ["2019"], "tdat": ["0407"], "time": ["1010"], "tory": ["2001"]},
    "v2.3-conflict": {"year": ["2019", "2020-01-01"]},
    "v2.4-conflict": {"year": ["2019", "2018"], "tdor": ["2001"]},
}

_ID3_FFPROBE_TAGS: dict[str, dict[str, str]] = {
    # ffprobe format tags, keys lowercased by read_video_tags. They lose frame
    # identity: TDRL wins over TDRC as "date", TYER+TDAT+TIME merge into one
    # "date", and of TYER and TDRC only one survives.
    "v2.4": {"title": "Tone", "date": "2002-03-04", "tdor": "2001-02-03"},
    "v2.3": {"title": "Tone", "date": "2019-07-04 10:10", "tory": "2001"},
    "v2.3-conflict": {"title": "Tone", "date": "2019"},
    "v2.4-conflict": {"title": "Tone", "date": "2018", "tdor": "2001"},
}


@pytest.mark.parametrize("case", sorted(_ID3_CASES))
def test_tinytag_reads_id3_dates(
    tmp_path: Path,
    fixtures_dir: Path,
    case: str,
) -> None:
    """tinytag keeps every ID3 date frame; the year joins TYER and TDRC."""
    frame_version, frames = _ID3_CASES[case]
    path = write_id3_sample(
        tmp_path / "dated.mp3",
        mp3_path=fixtures_dir / "sample.mp3",
        frames=frames,
        version=frame_version,
    )

    fields = TinyTag.get(path).as_dict()

    for key, values in _ID3_TINYTAG_FIELDS[case].items():
        assert_that(fields).contains_entry({key: values})


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
@pytest.mark.parametrize("case", sorted(_ID3_CASES))
def test_ffprobe_id3_date_mapping(
    tmp_path: Path,
    fixtures_dir: Path,
    case: str,
) -> None:
    """Record which ID3 date frames survive in ffprobe's format tags."""
    from winnow.media.video import read_video_tags

    frame_version, frames = _ID3_CASES[case]
    path = write_id3_sample(
        tmp_path / "dated.mp3",
        mp3_path=fixtures_dir / "sample.mp3",
        frames=frames,
        version=frame_version,
    )

    tags = read_video_tags(path)

    assert_that(tags).is_equal_to(_ID3_FFPROBE_TAGS[case])
