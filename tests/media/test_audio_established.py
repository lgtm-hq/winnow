"""Audio values are reported only when the stream they come from is established.

Each test here reproduces a case a review found returning a wrong value as a
fact, on files built in ``tmp_path`` from the committed fixtures (see
``fixtures/audio/README.md``). No test runs ffmpeg; the few that check what a
real ffprobe says skip without it.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that
from tinytag import TinyTag

from tests.media.audio_samples import id3_tag, mpeg_frames, syncsafe
from tests.media.test_audio import (
    _AUDIO_FIXTURES,
    _COMMITTED_EXPECTED,
    _HAS_FFPROBE,
    _fake_ffprobe,
    _no_ffprobe,
)
from winnow.exceptions import MediaError
from winnow.media import _audio_sniff as sniff_module
from winnow.media import audio as audio_module
from winnow.media._audio_sniff import (
    AudioContainer,
    mp4_audio_track_count,
    sniff_audio,
    sniff_audio_container,
    wave_fact_duration,
)
from winnow.media.audio import extract_audio_metadata, read_audio_tags
from winnow.models.media import MediaMetadata

_TONE_MP3 = _AUDIO_FIXTURES / "tone.mp3"


def _read(path: Path) -> MediaMetadata | None:
    """Return the metadata of ``path``, or ``None`` on ``MediaError``."""
    try:
        return extract_audio_metadata(path)
    except MediaError:
        return None


def _cbr_frames() -> bytes:
    """Return CBR MPEG frames without a Xing/Info header.

    ``tone.mp3``'s first frame is LAME's Info frame; the frames after it are
    plain 64 kbit/s frames, so a reader has to count or estimate.
    """
    frames = mpeg_frames(_TONE_MP3)
    first = sniff_module._mpeg_frame_length(frames[:4]) or 0
    return frames[first:]


# --- 1. the container tinytag parses is the one sniffed ----------------------


@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("ffprobe", [True, False], ids=["ffprobe", "no-ffprobe"])
def test_flac_after_an_empty_id3_tag_is_read_as_flac(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    ffprobe: bool,
) -> None:
    """An empty ID3v2 tag in front of FLAC no longer makes tinytag read MPEG.

    tinytag skips an ID3v2 tag only when its size is above zero, so it parsed
    the ``ID3`` bytes as MPEG: mp3, 16 000 Hz, 0.024 s.
    """
    if not ffprobe:
        _no_ffprobe(monkeypatch)
    path = tmp_path / "tagged.flac"
    flac = (_AUDIO_FIXTURES / "tone.flac").read_bytes()
    path.write_bytes(id3_tag(version=version) + flac)

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.sample_rate).is_equal_to(44100)
    assert_that(metadata.channels).is_equal_to(2)
    assert_that(float(metadata.duration_seconds or 0.0)).is_close_to(0.3, 0.002)


def _id3_prefixed(source: Path, directory: Path, version: int) -> Path:
    """Copy ``source`` with an ID3v2 tag holding one TIT2 frame in front."""
    payload = b"\x00Title"
    size = syncsafe(len(payload)) if version == 4 else len(payload).to_bytes(4, "big")
    frame = b"TIT2" + size + b"\x00\x00" + payload
    target = directory / f"{source.name}.id3v2{version}"
    target.write_bytes(id3_tag(frame, version=version) + source.read_bytes())
    return target


def _assert_no_new_value(
    tagged: MediaMetadata | None,
    bare: MediaMetadata | None,
    name: str,
) -> None:
    """Check a tagged file's reading against the bare file's.

    MPEG and FLAC, which may carry an ID3v2 tag, read exactly as the bare file
    does. Any other container behind an ID3v2 tag is malformed: it raises
    ``MediaError``, or reads with the bare file's codec and stream parameters
    and a duration within 10 ms of the bare file's (ffprobe's own reading of
    a file whose bare copy tinytag read).
    """
    if name.endswith((".mp3", ".flac")):
        assert_that(tagged).described_as(name).is_equal_to(bare)
        return
    if tagged is None:
        return
    assert_that(bare).described_as(name).is_not_none()
    if bare is None:
        return
    for field in ("codec", "sample_rate", "channels", "bit_depth"):
        assert_that(getattr(tagged, field)).described_as(f"{name} {field}").is_equal_to(
            getattr(bare, field),
        )
    if tagged.duration_seconds is not None:
        assert_that(bare.duration_seconds).described_as(name).is_not_none()
        assert_that(tagged.duration_seconds).described_as(name).is_close_to(
            bare.duration_seconds or 0.0,
            0.01,
        )


@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("ffprobe", [True, False], ids=["installed", "no-ffprobe"])
@pytest.mark.parametrize("filename", sorted(_COMMITTED_EXPECTED))
def test_id3_tag_before_any_fixture_adds_no_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    ffprobe: bool,
    version: int,
) -> None:
    """Every committed fixture with an ID3v2.3 or v2.4 tag in front.

    Before, tinytag read anything behind ``ID3`` as MPEG and invented values.
    The ``installed`` rows use ffprobe where it is installed (and so repeat the
    ``no-ffprobe`` rows on a runner without it).
    """
    if not ffprobe:
        _no_ffprobe(monkeypatch)
    source = _AUDIO_FIXTURES / filename

    tagged = _read(_id3_prefixed(source, tmp_path, version))

    _assert_no_new_value(tagged, _read(source), filename)


def test_stacked_id3_tags_before_mpeg(tmp_path: Path) -> None:
    """An MP3 whose tag sits behind another ID3v2 tag reads like the bare MP3."""
    path = tmp_path / "stacked.mp3"
    path.write_bytes(id3_tag(version=3) + _TONE_MP3.read_bytes())

    assert_that(sniff_audio_container(path)).is_equal_to(AudioContainer.MPEG)
    assert_that(_read(path)).is_equal_to(_read(_TONE_MP3))


def test_too_many_stacked_id3_tags_are_not_established(tmp_path: Path) -> None:
    """The ID3v2 tag chain is followed only so far."""
    path = tmp_path / "stacked.mp3"
    path.write_bytes(id3_tag() * 9 + mpeg_frames(_TONE_MP3))

    assert_that(sniff_audio(path)).is_none()


def test_parser_other_than_the_sniffed_one_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If tinytag parses the file as another container, none of it is used."""
    fake = SimpleNamespace(
        samplerate=16000,
        channels=2,
        duration=0.024,
        bitrate=32.0,
        bitdepth=None,
        mime_type="audio/mpeg",
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda *_args, **_kwargs: fake))
    _no_ffprobe(monkeypatch)
    path = _AUDIO_FIXTURES / "tone.flac"

    assert_that(audio_module._metadata_from_tinytag(path=path)).is_none()
    with pytest.raises(MediaError, match="tinytag cannot read"):
        extract_audio_metadata(path)


def test_parser_without_a_mime_type_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tinytag result that cannot say which parser made it is not used."""
    fake = SimpleNamespace(
        samplerate=44100,
        channels=2,
        duration=0.3,
        bitrate=128.0,
        bitdepth=16,
        mime_type=None,
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda *_args, **_kwargs: fake))

    path = _AUDIO_FIXTURES / "tone.flac"

    assert_that(audio_module._metadata_from_tinytag(path=path)).is_none()


def test_tags_of_a_mismatched_parser_are_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tags tinytag read with another container's parser are dropped too."""
    fake = SimpleNamespace(
        mime_type="audio/mpeg",
        as_dict=lambda: {"title": ["Wrong"]},
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda *_args, **_kwargs: fake))

    assert_that(read_audio_tags(_AUDIO_FIXTURES / "tone.flac")).is_empty()


def test_tags_of_flac_after_an_id3_tag_are_the_flac_tags(tmp_path: Path) -> None:
    """FLAC behind an ID3v2 tag gives its own Vorbis comments."""
    path = tmp_path / "tagged.flac"
    flac = _AUDIO_FIXTURES / "tone.flac"
    path.write_bytes(id3_tag() + flac.read_bytes())

    assert_that(read_audio_tags(path)).is_equal_to(read_audio_tags(flac))


def test_stream_view_behaves_like_a_file(tmp_path: Path) -> None:
    """The view tinytag reads starts at the offset and ends with the file."""
    path = tmp_path / "data.bin"
    path.write_bytes(b"0123456789")

    with path.open("rb") as handle:
        view = audio_module._StreamView(handle, 4)
        assert_that(view.readable()).is_true()
        assert_that(view.seekable()).is_true()
        assert_that(view.seek(0, io.SEEK_END)).is_equal_to(6)
        assert_that(view.seek(-2, io.SEEK_CUR)).is_equal_to(4)
        assert_that(view.read(10)).is_equal_to(b"89")
        assert_that(view.read(10)).is_equal_to(b"")
        assert_that(view.seek(1)).is_equal_to(1)
        assert_that(view.tell()).is_equal_to(1)
        assert_that(view.read(2)).is_equal_to(b"56")
        with pytest.raises(OSError, match="negative"):
            view.seek(-1)


# --- 2. MS ADPCM WAV duration from the fact chunk ----------------------------


def _adpcm_with(**changes: int) -> bytes:
    """Return ``adpcm.wav`` with fields of its fmt and fact chunks replaced.

    Keyword names: ``fact`` (samples), ``format`` (format code),
    ``samples_per_block`` and ``extension`` (cbSize).
    """
    data = bytearray((_AUDIO_FIXTURES / "adpcm.wav").read_bytes())
    fmt = data.find(b"fmt ") + 8
    fact = data.find(b"fact") + 8
    places = {
        "format": (fmt, 2),
        "extension": (fmt + 16, 2),
        "samples_per_block": (fmt + 18, 2),
        "fact": (fact, 4),
    }
    for name, value in changes.items():
        offset, width = places[name]
        data[offset : offset + width] = value.to_bytes(width, "little")
    return bytes(data)


def test_adpcm_wav_duration_is_its_fact_chunk() -> None:
    """2400 samples at 8000 Hz, within 2 blocks of 2036 samples: 0.3 s.

    The trust rule dropped this and reported ffprobe's 0.512 s, which counts
    both whole blocks.
    """
    path = _AUDIO_FIXTURES / "adpcm.wav"

    assert_that(wave_fact_duration(path)).is_equal_to(0.3)
    assert_that(extract_audio_metadata(path).duration_seconds).is_equal_to(0.3)


def test_adpcm_fact_duration_wins_over_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe's whole-block duration does not replace the fact chunk's."""
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "adpcm_ms",
                    "sample_rate": "8000",
                    "channels": 1,
                    "duration": "0.512000",
                    "bit_rate": "32184",
                },
            ],
        },
    )
    _fake_ffprobe(monkeypatch, payload)

    metadata = extract_audio_metadata(_AUDIO_FIXTURES / "adpcm.wav")

    assert_that(metadata.duration_seconds).is_equal_to(0.3)
    assert_that(metadata.codec).is_equal_to("adpcm_ms")
    assert_that(metadata.bitrate).is_equal_to(32184)


@pytest.mark.parametrize(
    "changes",
    [
        {"fact": 4073},
        {"fact": 0},
        {"format": 0x0001},
        {"format": 0x0006},
        {"samples_per_block": 0},
        {"extension": 0},
    ],
    ids=[
        "more-than-blocks-hold",
        "zero-samples",
        "pcm",
        "alaw",
        "no-samples-per-block",
        "no-extension",
    ],
)
def test_adpcm_fact_chunk_is_not_trusted_otherwise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, int],
) -> None:
    """A fact chunk is used only for MS/IMA ADPCM, and only when it fits."""
    path = tmp_path / "changed.wav"
    path.write_bytes(_adpcm_with(**changes))
    _no_ffprobe(monkeypatch)

    assert_that(wave_fact_duration(path)).is_none()
    metadata = _read(path)
    if metadata is not None and metadata.codec is None:
        assert_that(metadata.duration_seconds).is_none()


def test_ima_adpcm_fact_chunk_is_used(tmp_path: Path) -> None:
    """IMA ADPCM (format 0x11) stores samples per block the same way."""
    path = tmp_path / "ima.wav"
    path.write_bytes(_adpcm_with(format=0x0011))

    assert_that(wave_fact_duration(path)).is_equal_to(0.3)


@pytest.mark.parametrize(
    "content",
    [
        b"RIFF\x00\x00\x00\x00WAVE",
        b"RIFF\x00\x00\x00\x00WAVEfmt \x04\x00\x00\x00\x02\x00\x01\x00",
        b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 40,
    ],
    ids=["no-chunks", "short-fmt", "not-wave"],
)
def test_fact_duration_of_incomplete_files(tmp_path: Path, content: bytes) -> None:
    """Files without the chunks the rule needs have no fact duration."""
    path = tmp_path / "short.wav"
    path.write_bytes(content)

    assert_that(wave_fact_duration(path)).is_none()


def test_fact_duration_needs_a_data_chunk(tmp_path: Path) -> None:
    """Without a data chunk the fact count cannot be checked, so it is unused."""
    data = _adpcm_with()
    path = tmp_path / "nodata.wav"
    path.write_bytes(data[: data.find(b"data")])

    assert_that(wave_fact_duration(path)).is_none()


def test_fact_duration_of_a_missing_file(tmp_path: Path) -> None:
    """A path that cannot be read has no fact duration."""
    assert_that(wave_fact_duration(tmp_path / "missing.wav")).is_none()


def test_aifc_frame_count_is_not_used_as_samples() -> None:
    """IMA4 AIFF-C declares packets of 64 samples, not samples: no fact rule."""
    assert_that(wave_fact_duration(_AUDIO_FIXTURES / "ima4.aifc")).is_none()


# --- 3. MP4 with more than one audio track -----------------------------------


@pytest.mark.parametrize(
    ("filename", "count"),
    [("aac.m4a", 1), ("alac.m4a", 1), ("two_tracks.m4a", 2), ("tone.wav", None)],
)
def test_mp4_audio_track_count(filename: str, count: int | None) -> None:
    """Audio tracks are counted from their ``hdlr`` boxes."""
    assert_that(mp4_audio_track_count(_AUDIO_FIXTURES / filename)).is_equal_to(count)


def _box(box_type: bytes, body: bytes, *, large: bool = False) -> bytes:
    if large:
        return (
            (1).to_bytes(4, "big")
            + box_type
            + (len(body) + 16).to_bytes(8, "big")
            + body
        )
    return (len(body) + 8).to_bytes(4, "big") + box_type + body


def _audio_track() -> bytes:
    hdlr = _box(b"hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 12)
    return _box(sniff_module._MP4_TRACK_BOX, _box(b"mdia", hdlr))


@pytest.mark.parametrize(
    ("content", "count"),
    [
        (_box(b"moov", _audio_track(), large=True), 1),
        (_box(b"ftyp", b"M4A ") + (0).to_bytes(4, "big") + b"moov" + _audio_track(), 1),
        (_box(b"moov", _audio_track() * 2), 2),
        (b"\x00\x00\x00\x40moov" + _audio_track(), None),
        (b"\x00\x00\x00\x04moov", None),
        (_box(b"free", b"") * 5000, None),
    ],
    ids=["largesize", "to-end-of-file", "two", "overrun", "too-small", "too-many"],
)
def test_mp4_audio_track_count_of_built_boxes(
    tmp_path: Path,
    content: bytes,
    count: int | None,
) -> None:
    """64-bit and to-the-end sizes are followed; malformed boxes give ``None``."""
    path = tmp_path / "built.mp4"
    path.write_bytes(content)

    assert_that(mp4_audio_track_count(path)).is_equal_to(count)


def test_mp4_audio_track_count_of_a_missing_file(tmp_path: Path) -> None:
    """A path that cannot be read cannot be counted."""
    assert_that(mp4_audio_track_count(tmp_path / "missing.m4a")).is_none()


def test_two_track_mp4_does_not_mix_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """AAC 44.1 kHz stereo plus ALAC 8 kHz mono: never fields of both.

    tinytag reported alac, 16-bit, 2 channels, 44 100 Hz. The file is read by
    ffprobe, whose first audio stream is the AAC one.
    """
    path = _AUDIO_FIXTURES / "two_tracks.m4a"
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "44100",
                    "channels": 2,
                    "duration": "0.300000",
                    "bit_rate": "63807",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "alac",
                    "sample_rate": "8000",
                    "channels": 1,
                    "bits_per_raw_sample": "16",
                },
            ],
        },
    )
    _fake_ffprobe(monkeypatch, payload)

    assert_that(audio_module._metadata_from_tinytag(path=path)).is_none()
    assert_that(extract_audio_metadata(path)).is_equal_to(
        MediaMetadata(
            codec="aac",
            sample_rate=44100,
            channels=2,
            duration_seconds=0.3,
            bitrate=63807,
        ),
    )


def test_two_track_mp4_without_ffprobe_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ffprobe no single stream can be read from it."""
    _no_ffprobe(monkeypatch)

    with pytest.raises(MediaError, match="ffprobe"):
        extract_audio_metadata(_AUDIO_FIXTURES / "two_tracks.m4a")


# --- 4. an MPEG stream must be confirmed --------------------------------------


def test_free_format_header_and_random_bytes_are_not_mpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A free-format header followed by random bytes is never established.

    206 of 400 such files read as mp3 with an invented rate, bitrate and
    duration.
    """
    _no_ffprobe(monkeypatch)
    path = tmp_path / "noise.bin"
    established = []
    for index in range(400):
        noise = _noise(index, 64 + index * 10)
        header = bytes([0xFF, (0xFB, 0xFA, 0xF3, 0xE3)[index % 4], 0x00, 0x44])
        path.write_bytes(header + noise)
        if sniff_audio(path) is not None:
            established.append(index)

    assert_that(established).is_empty()
    with pytest.raises(MediaError):
        extract_audio_metadata(path)


def _noise(seed: int, size: int) -> bytes:
    """Return ``size`` deterministic pseudo-random bytes."""
    blocks = (
        hashlib.sha256(f"{seed}:{block}".encode()).digest()
        for block in range(size // 32 + 1)
    )
    return b"".join(blocks)[:size]


def _layer1_frame(second: int = 0xFE) -> bytes:
    """Return one 192-byte MPEG-1 Layer I frame (128 kbit/s, 32 kHz)."""
    header = bytes([0xFF, second, 0x48, 0x00])
    length = sniff_module._mpeg_frame_length(header) or 0
    return header + b"\x00" * (length - 4)


@pytest.mark.parametrize(
    "text",
    [
        "x" * 95,
        "Hello, this is a note saved as UTF-16 text by a text editor." * 4,
        ("y" * 95 + "﻿") * 3,
    ],
    ids=["one-frame-to-end", "prose", "bom-every-frame"],
)
def test_utf16_text_with_a_bom_is_not_mpeg(tmp_path: Path, text: str) -> None:
    """``FF FE`` text looks like an MPEG-1 Layer I header with a CRC.

    ``"\\ufeff" + "x" * 95`` is exactly one 192-byte "frame" ending at the end
    of the file, which used to confirm it.
    """
    path = tmp_path / "note.txt"
    path.write_bytes(("﻿" + text).encode("utf-16-le"))

    assert_that(sniff_audio(path)).is_none()


def test_layer1_stream_starting_like_a_bom_needs_three_frames(tmp_path: Path) -> None:
    """Real MPEG-1 Layer I frames with a CRC are still MPEG when three agree."""
    path = tmp_path / "layer1.mp1"
    path.write_bytes(_layer1_frame() * 3)

    assert_that(sniff_audio_container(path)).is_equal_to(AudioContainer.MPEG)
    path.write_bytes(_layer1_frame() * 2)
    assert_that(sniff_audio(path)).is_none()


def test_second_frame_must_match_the_first(tmp_path: Path) -> None:
    """A following header of another layer does not confirm the frame."""
    path = tmp_path / "mixed.bin"
    first = _layer1_frame(0xFF)
    second = bytes([0xFF, 0xFB, 0x90, 0x64]) + b"\x00" * 600
    path.write_bytes(first + second)

    assert_that(sniff_audio(path)).is_none()


# --- 5. zero or absent ffprobe values are unknown -----------------------------


@pytest.mark.parametrize(
    "stream",
    [
        {"sample_rate": "0", "channels": 2},
        {"sample_rate": "44100", "channels": 0},
        {"sample_rate": "-1", "channels": 2},
        {"channels": 2},
        {"sample_rate": "44100"},
    ],
    ids=["zero-rate", "zero-channels", "negative-rate", "no-rate", "no-channels"],
)
def test_ffprobe_stream_without_parameters_is_no_reading(
    stream: dict[str, object],
) -> None:
    """A stream without both a sample rate and channel count above 0 is unusable."""
    payload = json.dumps(
        {
            "streams": [
                {"codec_type": "audio", "codec_name": "aac", "duration": "1", **stream},
            ],
        },
    )

    assert_that(audio_module._parse_ffprobe_audio(payload)).is_none()


def test_truncated_adts_with_a_zero_sample_rate_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe's ``sample_rate`` 0 for 12 bytes of ADTS is not passed through."""
    path = tmp_path / "cut.aac"
    path.write_bytes((_AUDIO_FIXTURES / "tone.aac").read_bytes()[:12])
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "0",
                    "channels": 2,
                },
            ],
            "format": {"duration": "0.023220"},
        },
    )
    _fake_ffprobe(monkeypatch, payload)

    with pytest.raises(MediaError, match="unsupported or corrupt"):
        extract_audio_metadata(path)


def test_zero_ffprobe_numbers_are_unknown() -> None:
    """Zero durations, bitrates and bit depths from ffprobe are ``None``."""
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "44100",
                    "channels": 2,
                    "duration": "0.000000",
                    "bit_rate": "0",
                    "bits_per_sample": 0,
                },
            ],
            "format": {"duration": "0", "bit_rate": "0"},
        },
    )

    assert_that(audio_module._parse_ffprobe_audio(payload)).is_equal_to(
        MediaMetadata(codec="aac", sample_rate=44100, channels=2),
    )


# --- 6. a duration ffprobe estimated from the bitrate is not a fact -----------


@pytest.mark.parametrize(
    "prefix",
    [
        b"\x00" * 100_000,
        id3_tag(b"\x00" * 70_000),
        id3_tag() + b"\x00" * 70_000,
    ],
    ids=["zeros", "id3-with-padding", "id3-then-zeros"],
)
@pytest.mark.parametrize("ffprobe", [True, False], ids=["ffprobe", "no-ffprobe"])
def test_mp3_behind_padding_reads_like_the_bare_mp3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: bytes,
    ffprobe: bool,
) -> None:
    """Zero padding before the first frame no longer sends the file to ffprobe.

    ffprobe estimated 7.295 s and 5.42 s for a 1.045 s CBR MP3 behind 100 kB
    of zeros and behind an ID3 tag with 70 kB of padding; main (and tinytag on
    the bare frames) read the frames.
    """
    if not ffprobe:
        _no_ffprobe(monkeypatch)
    bare = tmp_path / "bare.mp3"
    bare.write_bytes(_cbr_frames())
    padded = tmp_path / "padded.mp3"
    padded.write_bytes(prefix + _cbr_frames())

    expected = extract_audio_metadata(bare)

    assert_that(sniff_audio_container(padded)).is_equal_to(AudioContainer.MPEG)
    assert_that(extract_audio_metadata(padded)).is_equal_to(expected)
    assert_that(float(expected.duration_seconds or 0.0)).is_close_to(0.3135, 0.03)


def test_ffprobe_bitrate_estimate_is_not_a_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ffprobe's ``Estimating duration from bitrate`` makes the duration unknown."""
    path = tmp_path / "junk.mp3"
    path.write_bytes(b"\x01" * 1000 + _cbr_frames())
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "sample_rate": "44100",
                    "channels": 2,
                    "duration": "0.437551",
                    "bit_rate": "64000",
                },
            ],
        },
    )
    _fake_ffprobe(
        monkeypatch,
        payload,
        stderr="[mp3 @ 0x1] Estimating duration from bitrate, this may be inaccurate\n",
    )

    metadata = extract_audio_metadata(path)

    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_equal_to(64000)
    assert_that(metadata.codec).is_equal_to("mp3")


@pytest.mark.skipif(not _HAS_FFPROBE, reason="ffprobe not installed")
def test_real_ffprobe_estimate_behind_junk_is_unknown(tmp_path: Path) -> None:
    """Non-zero junk before the frames: ffprobe reads it, estimating the duration."""
    path = tmp_path / "junk.mp3"
    path.write_bytes(b"\x01" * 1000 + _cbr_frames())

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("mp3")
    assert_that(metadata.sample_rate).is_equal_to(44100)
    assert_that(metadata.duration_seconds).is_none()


@pytest.mark.parametrize("chunk", [65536, 512])
def test_padding_longer_than_the_limit_is_not_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunk: int,
) -> None:
    """Zeros are skipped only up to a limit, so a zero file is not scanned whole."""
    monkeypatch.setattr(sniff_module, "_MAX_ZERO_PADDING", 1000)
    monkeypatch.setattr(sniff_module, "_READ_CHUNK", chunk)
    path = tmp_path / "padded.mp3"
    for zeros in (1001, 2000):
        path.write_bytes(b"\x00" * zeros + _cbr_frames())
        assert_that(sniff_audio(path)).described_as(str(zeros)).is_none()
    path.write_bytes(b"\x00" * 1000 + _cbr_frames())
    assert_that(sniff_audio_container(path)).is_equal_to(AudioContainer.MPEG)


def test_only_zeros_are_not_audio(tmp_path: Path) -> None:
    """A file of nothing but zeros has no stream to find."""
    path = tmp_path / "zeros.bin"
    path.write_bytes(b"\x00" * 4096)

    assert_that(sniff_audio(path)).is_none()
