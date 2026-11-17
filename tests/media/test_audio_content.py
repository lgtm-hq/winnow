"""Audio is identified by its content, never its file name."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that
from tinytag import TinyTag

from tests.media.test_audio import (
    _AUDIO_FIXTURES,
    _COMMITTED_EXPECTED,
    _FFPROBE_WAV,
    _HAS_FFPROBE,
    _fake_ffprobe,
    _no_ffprobe,
)
from winnow.exceptions import MediaError
from winnow.media import _audio_sniff as sniff_module
from winnow.media import audio as audio_module
from winnow.media._audio_sniff import (
    AudioContainer,
    sniff_audio_container,
    wave_subformat,
)
from winnow.media.audio import extract_audio_metadata, read_audio_tags

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

_ALL_AUDIO: list[Path] = [
    *(_AUDIO_FIXTURES / name for name in sorted(_COMMITTED_EXPECTED)),
    *(_FIXTURES / name for name in ("sample.mp3", "sample.flac", "sample.ogg")),
    _FIXTURES / "sample.wav",
]

_FFPROBE_ONLY = sorted(
    name for name, fields in _COMMITTED_EXPECTED.items() if fields.needs_ffprobe
)
"""Fixtures tinytag does not read or does not fully trust."""

_NOT_TINYTAG = ["tone.aac", "tone.ac3", "tone.tta", "tone.wv"]
"""Fixtures whose container tinytag does not parse at all."""


def _renamed_copies(source: Path, directory: Path) -> list[Path]:
    """Copy ``source`` under a wrong extension, no extension and ``.bin``."""
    wrong = ".wav" if source.suffix == ".mp3" else ".mp3"
    copies = []
    for name in (f"copy{wrong}", "copy", "copy.bin"):
        target = directory / name
        shutil.copyfile(source, target)
        copies.append(target)
    return copies


def _read(path: Path) -> tuple[object, dict[str, str]]:
    """Return the metadata (or the error message) and the tags of one file."""
    metadata: object
    try:
        metadata = extract_audio_metadata(path).model_dump()
    except MediaError as exc:
        metadata = f"MediaError: {exc.message}"
    return metadata, read_audio_tags(path)


@pytest.mark.parametrize("source", _ALL_AUDIO, ids=lambda path: path.name)
def test_name_does_not_change_the_result(source: Path, tmp_path: Path) -> None:
    """A copy under a wrong, missing or ``.bin`` extension reads the same."""
    expected = _read(source)

    for copy in _renamed_copies(source, tmp_path):
        assert_that(_read(copy)).described_as(copy.name).is_equal_to(expected)


@pytest.mark.parametrize("source", _ALL_AUDIO, ids=lambda path: path.name)
def test_name_does_not_change_the_result_without_ffprobe(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ffprobe, renamed copies still read the same, or fail the same."""
    _no_ffprobe(monkeypatch)
    expected = _read(source)

    for copy in _renamed_copies(source, tmp_path):
        assert_that(_read(copy)).described_as(copy.name).is_equal_to(expected)


def test_wav_named_mp3_is_not_parsed_as_mpeg(tmp_path: Path) -> None:
    """The reproduced case: a WAV named ``.mp3`` keeps its real fields."""
    path = tmp_path / "tone.mp3"
    shutil.copyfile(_AUDIO_FIXTURES / "tone.wav", path)

    metadata = extract_audio_metadata(path)

    assert_that(metadata.codec).is_equal_to("pcm")
    assert_that(metadata.sample_rate).is_equal_to(8000)
    assert_that(metadata.channels).is_equal_to(1)
    assert_that(float(metadata.duration_seconds or 0.0)).is_close_to(0.3, 0.002)


@pytest.mark.parametrize("filename", _NOT_TINYTAG)
def test_unknown_container_without_ffprobe_names_ffprobe(
    filename: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A format tinytag does not parse raises MediaError naming ffprobe."""
    _no_ffprobe(monkeypatch)

    with pytest.raises(MediaError, match="ffprobe") as raised:
        extract_audio_metadata(_AUDIO_FIXTURES / filename)

    assert_that(raised.value.message).contains("cannot be established")


def test_tinytag_failure_without_ffprobe_names_ffprobe(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known container tinytag fails on raises MediaError naming ffprobe."""
    monkeypatch.setattr(audio_module, "_metadata_from_tinytag", lambda *, path: None)
    _no_ffprobe(monkeypatch)

    with pytest.raises(MediaError, match="ffprobe") as raised:
        extract_audio_metadata(fixtures_dir / "sample.wav")

    assert_that(raised.value.message).contains("tinytag cannot read")


def test_zero_duration_from_tinytag_is_unknown_without_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLAC in Ogg: tinytag's 0.0 s is not reported; the duration is unknown."""
    _no_ffprobe(monkeypatch)

    metadata = extract_audio_metadata(_AUDIO_FIXTURES / "flac.oga")

    assert_that(metadata.codec).is_equal_to("flac")
    assert_that(metadata.sample_rate).is_equal_to(8000)
    assert_that(metadata.duration_seconds).is_none()
    assert_that(metadata.bitrate).is_none()


@pytest.mark.skipif(not _HAS_FFPROBE, reason="ffprobe not installed")
@pytest.mark.parametrize("filename", _FFPROBE_ONLY)
def test_ffprobe_rows_have_a_positive_duration(filename: str) -> None:
    """Every file that needs ffprobe gets a real, non-zero duration from it."""
    metadata = extract_audio_metadata(_AUDIO_FIXTURES / filename)

    assert_that(float(metadata.duration_seconds or 0.0)).is_greater_than(0.25)


# --- a path starting with "-" is never an ffprobe option ---------------------


def test_ffprobe_gets_an_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative path starting with ``-`` reaches ffprobe as an absolute path."""
    monkeypatch.chdir(tmp_path)
    shutil.copyfile(_AUDIO_FIXTURES / "tone.aac", tmp_path / "-i.aac")
    calls = _fake_ffprobe(monkeypatch, _FFPROBE_WAV)

    extract_audio_metadata(Path("-i.aac"))

    assert_that(calls).is_length(1)
    assert_that(calls[0][-1]).is_equal_to(str(tmp_path / "-i.aac"))
    assert_that(Path(calls[0][-1]).is_absolute()).is_true()


@pytest.mark.skipif(not _HAS_FFPROBE, reason="ffprobe not installed")
def test_real_ffprobe_reads_a_dash_named_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffprobe reads a file named like an option instead of failing on it."""
    monkeypatch.chdir(tmp_path)
    shutil.copyfile(_AUDIO_FIXTURES / "tone.ac3", tmp_path / "-version")

    metadata = extract_audio_metadata(Path("-version"))

    assert_that(metadata.codec).is_equal_to("ac3")


# --- tags ---------------------------------------------------------------------


def test_clashing_extra_field_merges_into_its_tag(
    fixtures_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tinytag's ``_title`` artefact merges into ``title``, keeping new values."""
    fake = SimpleNamespace(
        as_dict=lambda: {
            "title": ["Tone"],
            "_title": ["Tone", "Other"],
            "_custom": ["kept"],
            "_track": ["7"],
        },
    )
    monkeypatch.setattr(TinyTag, "get", staticmethod(lambda *_args, **_kwargs: fake))

    tags = read_audio_tags(fixtures_dir / "sample.mp3")

    assert_that(tags).is_equal_to(
        {"title": "Tone, Other", "_custom": "kept", "track": "7"},
    )


def test_wma_has_no_underscore_title() -> None:
    """The WMA fixture's duplicate title descriptor is not a separate tag."""
    tags = read_audio_tags(_AUDIO_FIXTURES / "tone.wma")

    assert_that(tags).does_not_contain_key("_title")
    assert_that(tags).contains_entry({"title": "Tone"})


def test_tags_of_unknown_container_are_empty(tmp_path: Path) -> None:
    """A file whose container is not established gives no tags."""
    path = tmp_path / "noise.mp3"
    path.write_bytes(b"\x00" * 64)

    assert_that(read_audio_tags(path)).is_empty()


# --- codec labels from tinytag's MIME type ------------------------------------


@pytest.mark.parametrize(
    ("mime_type", "subformat", "expected"),
    [
        ('application/vnd.ms-asf; codecs="161"', None, "wmav2"),
        ('application/vnd.ms-asf; codecs="160"', None, "wmav1"),
        ('application/vnd.ms-asf; codecs="162"', None, "wmapro"),
        ('application/vnd.ms-asf; codecs="163"', None, "wmalossless"),
        ('application/vnd.ms-asf; codecs="55"', None, None),
        ("application/vnd.ms-asf", None, None),
        ('audio/wav; codecs="FFFE"', 1, "pcm"),
        ('audio/wav; codecs="FFFE"', 3, "pcm"),
        ('audio/wav; codecs="FFFE"', 2, None),
        ('audio/wav; codecs="FFFE"', None, None),
    ],
)
def test_codec_from_mime_type_asf_and_extensible(
    mime_type: str,
    subformat: int | None,
    expected: str | None,
) -> None:
    """ASF format codes and extensible WAV subformats map to codec labels."""
    assert_that(
        audio_module._codec_from_mime_type(mime_type, subformat=subformat),
    ).is_equal_to(expected)


def _extensible_wav(path: Path, *, subformat: int) -> Path:
    """Write a 0.1 s mono 8 kHz 16-bit ``WAVE_FORMAT_EXTENSIBLE`` file."""
    samples = b"\x00\x01" * 800
    fmt = (
        (0xFFFE).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (8000).to_bytes(4, "little")
        + (16000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + (22).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + (4).to_bytes(4, "little")
        + subformat.to_bytes(2, "little")
        + bytes.fromhex("000000001000800000aa00389b71")
    )
    junk = b"JUNK" + (3).to_bytes(4, "little") + b"abc\x00"
    body = (
        b"WAVE"
        + junk
        + b"fmt "
        + len(fmt).to_bytes(4, "little")
        + fmt
        + b"data"
        + len(samples).to_bytes(4, "little")
        + samples
    )
    path.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)
    return path


def test_extensible_pcm_wav_is_pcm_without_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PCM ``WAVE_FORMAT_EXTENSIBLE`` file is labelled pcm and timed by tinytag."""
    path = _extensible_wav(tmp_path / "ext.bin", subformat=1)
    _no_ffprobe(monkeypatch)

    metadata = extract_audio_metadata(path)

    assert_that(wave_subformat(path)).is_equal_to(1)
    assert_that(metadata.codec).is_equal_to("pcm")
    assert_that(float(metadata.duration_seconds or 0.0)).is_close_to(0.1, 0.002)


def test_wave_subformat_rejects_other_files(tmp_path: Path) -> None:
    """Only an extensible ``fmt `` chunk has a subformat."""
    plain = _AUDIO_FIXTURES / "tone.wav"
    not_wave = _AUDIO_FIXTURES / "tone.aiff"
    truncated = tmp_path / "truncated.wav"
    truncated.write_bytes(b"RIFF\x10\x00\x00\x00WAVEfmt ")
    no_fmt = tmp_path / "no_fmt.wav"
    no_fmt.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")

    assert_that(wave_subformat(plain)).is_none()
    assert_that(wave_subformat(not_wave)).is_none()
    assert_that(wave_subformat(truncated)).is_none()
    assert_that(wave_subformat(no_fmt)).is_none()
    assert_that(wave_subformat(tmp_path / "missing.wav")).is_none()


# --- the content sniffer --------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("tone.mp3", AudioContainer.MPEG),
        ("tone.flac", AudioContainer.FLAC),
        ("aac.m4a", AudioContainer.MP4),
        ("tone.opus", AudioContainer.OGG),
        ("flac.oga", AudioContainer.OGG),
        ("tone.wav", AudioContainer.WAVE),
        ("tone.wma", AudioContainer.ASF),
        ("tone.aiff", AudioContainer.AIFF),
        ("ima4.aifc", AudioContainer.AIFF),
        ("tone.aac", None),
        ("tone.ac3", None),
        ("tone.tta", None),
        ("tone.wv", None),
    ],
)
def test_sniff_committed_fixtures(
    filename: str,
    expected: AudioContainer | None,
) -> None:
    """Each committed fixture is identified from its bytes."""
    assert_that(sniff_audio_container(_AUDIO_FIXTURES / filename)).is_equal_to(
        expected,
    )


def _syncsafe(value: int) -> bytes:
    return bytes(
        [(value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F],
    )


def _id3(body: bytes, *, footer: bool = False) -> bytes:
    flags = 0x10 if footer else 0
    header = b"ID3\x04\x00" + bytes([flags]) + _syncsafe(len(body))
    return header + body + (b"3DI" + header[3:] if footer else b"")


def _mpeg_frames() -> bytes:
    """Return the MPEG frames of the committed MP3 fixture (after its tag)."""
    data = (_AUDIO_FIXTURES / "tone.mp3").read_bytes()
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    return data[10 + size :]


_MPEG_FRAMES = _mpeg_frames()
_FIRST_FRAME_LENGTH = sniff_module._mpeg_frame_length(_MPEG_FRAMES[:4]) or 0


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_id3(b"\x00" * 20) + b"fLaC" + b"\x00" * 40, AudioContainer.FLAC),
        (_id3(b"\x00" * 20) + b"\x00" * 100 + _mpeg_frames(), AudioContainer.MPEG),
        (_id3(b"\x00" * 20, footer=True) + _mpeg_frames(), AudioContainer.MPEG),
        (_id3(b"\x00" * 20) + (_AUDIO_FIXTURES / "tone.aac").read_bytes(), None),
        (b"ID3\x04\x00\x00\x80\x00\x00\x00" + b"\x00" * 40, None),
        (b"ID3\x04", None),
        (_mpeg_frames(), AudioContainer.MPEG),
        (_MPEG_FRAMES[:_FIRST_FRAME_LENGTH], AudioContainer.MPEG),
        (b"\xff\xfb\x90\x64" + b"\x00" * 40, None),
        (b"\xff\xfb\x00\x64" + b"\x00" * 40, AudioContainer.MPEG),
        (b"\xff\xfb\xf0\x64" + b"\x00" * 40, None),
        (b"\xff\xfb\x9c\x64" + b"\x00" * 40, None),
        (b"\xff\xeb\x90\x64" + b"\x00" * 40, None),
        (b"\xff", None),
        (b"", None),
        (b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 40, None),
        (b"FORM\x00\x00\x00\x008SVX" + b"\x00" * 40, None),
    ],
    ids=[
        "id3-flac",
        "id3-padding-mpeg",
        "id3-footer-mpeg",
        "id3-adts",
        "id3-bad-size",
        "id3-truncated",
        "mpeg",
        "mpeg-single-frame",
        "mpeg-unconfirmed",
        "mpeg-free-format",
        "mpeg-bad-bitrate",
        "mpeg-bad-rate",
        "mpeg-reserved-version",
        "one-byte",
        "empty",
        "riff-avi",
        "form-8svx",
    ],
)
def test_sniff_content(tmp_path: Path, content: bytes, expected: object) -> None:
    """ID3-prefixed, bare MPEG and look-alike headers are told apart."""
    path = tmp_path / "audio.bin"
    path.write_bytes(content)

    assert_that(sniff_audio_container(path)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("header", "length"),
    [
        (b"\xff\xfb\x90\x64", 417),
        (b"\xff\xfb\x92\x64", 418),
        (b"\xff\xf3\x90\x64", 261),
        (b"\xff\xfd\x90\x64", 522),
        (b"\xff\xff\x90\x64", 312),
        (b"\xff\xf7\x90\x64", 312),
        (b"\xff\xe3\x90\x64", 522),
    ],
    ids=["v1-l3", "v1-l3-padded", "v2-l3", "v1-l2", "v1-l1", "v2-l1", "v25-l3"],
)
def test_mpeg_frame_length(header: bytes, length: int) -> None:
    """Frame lengths follow the MPEG audio tables for each version and layer."""
    assert_that(sniff_module._mpeg_frame_length(header)).is_equal_to(length)


def test_sniff_unreadable_path(tmp_path: Path) -> None:
    """A path that cannot be opened is not a container."""
    assert_that(sniff_audio_container(tmp_path)).is_none()
