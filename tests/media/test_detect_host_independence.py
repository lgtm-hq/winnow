"""The default extension fallback does not depend on the host (#464).

Each test writes bytes no content rule recognises, so rule 6 (the
extension) decides, then runs :func:`~winnow.media._detect.detect` while
the host MIME table answers from different simulated tables: the host's
own, CPython's built-in table alone, an empty table, and a table that gives
every extension a different media type. Both the process-wide
``mimetypes.guess_type`` and the registry's private opt-in table are
replaced. With the default registry every result must be the same.

The table may only make existing name answers host-independent, never make
a file media by name that winnow left unknown; the membership tests derive
what may be in it from that rule.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import struct
import sys
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that

from tests.script_runner import run_script
from winnow.media import registry as registry_module
from winnow.media._detect import DetectionResult, detect
from winnow.media._extension_table import (
    EXCLUDED_EXTENSIONS,
    EXTENSION_MEDIA_TYPES,
    EXTENSION_TABLE_NAME,
    EXTENSION_TABLE_VERSION,
    HOST_DEPENDENT_ADDITIONS,
)
from winnow.media._format_checks import PLAYLIST_EXTENSIONS
from winnow.media.registry import (
    DEFAULT_FORMATS,
    DEFAULT_USE_MIME_FALLBACK,
    ExtensionLookup,
    FormatRegistry,
    create_default_format_registry,
    media_type_for_extension,
)
from winnow.models.config import WinnowConfig
from winnow.models.media import MediaType
from winnow.models.pipeline import PipelineResult, RunMetadata
from winnow.pipeline import DiscoveryStep, PipelineContext, RunState

GuessType = Callable[..., tuple[str | None, str | None]]
DetectUnderEachTable = Callable[[Path], dict[str, DetectionResult]]

IMAGE = MediaType.IMAGE
AUDIO = MediaType.AUDIO
VIDEO = MediaType.VIDEO

OPAQUE_BYTES = bytes(range(0x80, 0x100)) * 2
"""256 bytes no content rule recognises and that are not text."""

HOST_DEPENDENT_EXTENSIONS = (
    ".caf",
    ".mka",
    ".m2ts",
    ".mts",
    ".weba",
    ".opus",
    ".m4a",
    ".flac",
    ".mkv",
    ".webp",
    ".3gp",
)
"""The extensions the issue measured changing between hosts and Pythons."""

RULE_2_ADDITIONS: dict[str, MediaType] = {
    # Each was media by name only on some hosts or Pythons, names one media
    # format with no competing use, and has a content rule. Adding an entry
    # to the table's HOST_DEPENDENT_ADDITIONS needs a matching line here.
    "flac": AUDIO,
    "m4a": AUDIO,
    "m4p": AUDIO,
    "midi": AUDIO,
    "rmi": AUDIO,
    "mka": AUDIO,
    "weba": AUDIO,
    "mpga": AUDIO,
    "ogg": AUDIO,
    "oga": AUDIO,
    "s3m": AUDIO,
    "xm": AUDIO,
    "wma": AUDIO,
    "djv": IMAGE,
    "djvu": IMAGE,
    "heics": IMAGE,
    "heifs": IMAGE,
    "jp2": IMAGE,
    "pcx": IMAGE,
    "psd": IMAGE,
    "qtif": IMAGE,
    "sgi": IMAGE,
    "webp": IMAGE,
    "wmv": VIDEO,
    "dv": VIDEO,
    "flv": VIDEO,
    "f4v": VIDEO,
    "m2t": VIDEO,
    "m2ts": VIDEO,
    "m2v": VIDEO,
    "m4v": VIDEO,
    "mkv": VIDEO,
    "mk3d": VIDEO,
    "ogv": VIDEO,
}
"""The deliberate rule 2 entries, written out independently of the table."""

REPRESENTATIVE_ANSWERS: dict[str, MediaType | None] = {
    "jpg": IMAGE,
    "svgz": IMAGE,
    "nef": IMAGE,
    "eps": IMAGE,
    "mp4": VIDEO,
    "webm": VIDEO,
    "mpa": VIDEO,
    "fli": VIDEO,
    "mp3": AUDIO,
    "3gp": AUDIO,
    "3gpp": AUDIO,
    "ass": AUDIO,
    "669": AUDIO,
    "mkv": VIDEO,
    "flac": AUDIO,
    "webp": IMAGE,
    "fit": None,
    "bw": None,
    "ac3": None,
    "xcf": None,
    "caf": None,
    "mts": None,
    "dds": None,
    "emf": None,
    "wmf": None,
    "wdp": None,
    "asf": None,
    "mng": None,
    "ogg": AUDIO,
    "djvu": IMAGE,
    "m4v": VIDEO,
}
"""Expected default answers, written out independently of the table."""

_HOST_GUESS_TYPE: GuessType = mimetypes.guess_type
_BUILTIN_ONLY = mimetypes.MimeTypes(filenames=())
_ROTATED_PREFIX = {"audio": "video", "video": "image", "image": "audio"}
_MEDIA_PREFIXES = {"audio": AUDIO, "image": IMAGE, "video": VIDEO}


def _empty_table(url: str, strict: bool = True) -> tuple[str | None, str | None]:
    """Answer like a host whose MIME table knows no extension.

    Args:
        url: File name to type.
        strict: Unused; kept for the ``mimetypes.guess_type`` signature.

    Returns:
        No type and no encoding.
    """
    del url, strict
    return None, None


def _rotated_table(url: str, strict: bool = True) -> tuple[str | None, str | None]:
    """Answer with a media type other than the host's for every extension.

    Args:
        url: File name to type.
        strict: Passed to the host table.

    Returns:
        The host's answer moved to another media type; ``image`` where the
        host has no media answer.
    """
    mime_type, encoding = _HOST_GUESS_TYPE(url, strict=strict)
    prefix, _, subtype = (mime_type or "").partition("/")
    rotated = _ROTATED_PREFIX.get(prefix)
    if rotated is None:
        return "image/x-winnow-test", encoding
    return f"{rotated}/{subtype}", encoding


SIMULATED_TABLES: dict[str, GuessType] = {
    "host": _HOST_GUESS_TYPE,
    "builtin-only": _BUILTIN_ONLY.guess_type,
    "empty": _empty_table,
    "rotated": _rotated_table,
}
"""``mimetypes.guess_type`` replacements, by name."""


def _use_table(monkeypatch: pytest.MonkeyPatch, guess_type: GuessType) -> None:
    """Make ``guess_type`` the host MIME table everywhere winnow could ask.

    Args:
        monkeypatch: Applies the replacement.
        guess_type: The simulated table.
    """
    monkeypatch.setattr(mimetypes, "guess_type", guess_type)
    host_table = SimpleNamespace(guess_type=guess_type)
    monkeypatch.setattr(registry_module, "_host_mime_types", lambda: host_table)


@pytest.fixture
def detect_under_each_table(monkeypatch: pytest.MonkeyPatch) -> DetectUnderEachTable:
    """Return a function that detects a file under every simulated table.

    Args:
        monkeypatch: Replaces the host MIME table for each run.

    Returns:
        The function, mapping each table's name to its result.
    """

    def run(path: Path) -> dict[str, DetectionResult]:
        results = {}
        for name, guess_type in SIMULATED_TABLES.items():
            _use_table(monkeypatch, guess_type)
            results[name] = detect(path)
        monkeypatch.undo()
        return results

    return run


def _opaque(directory: Path, extension: str) -> Path:
    """Write the opaque bytes under ``extension``.

    Args:
        directory: Where to write.
        extension: Extension with its dot.

    Returns:
        The file.
    """
    path = directory / f"opaque{extension}"
    path.write_bytes(OPAQUE_BYTES)
    return path


def _decisions(result: DetectionResult) -> list[str]:
    """Return the details of a result's deciding signals.

    Args:
        result: A detection result.

    Returns:
        Every signal detail that starts with ``decision=``.
    """
    return [
        signal.detail
        for signal in result.signals
        if signal.detail.startswith("decision=")
    ]


def _assert_all_equal(results: dict[str, DetectionResult]) -> None:
    """Assert every simulated table gave the host's result.

    Args:
        results: Results by table name.
    """
    for name, result in results.items():
        assert_that(result).described_as(name).is_equal_to(results["host"])


def _builtin_media_type(extension: str) -> MediaType | None:
    """Type an extension with this Python's built-in MIME table alone.

    Args:
        extension: Extension without the dot.

    Returns:
        The media type CPython's built-in table gives, or ``None``.
    """
    mime_type, _ = _BUILTIN_ONLY.guess_type(f"file.{extension}")
    return _MEDIA_PREFIXES.get((mime_type or "").partition("/")[0])


@pytest.mark.parametrize("extension", HOST_DEPENDENT_EXTENSIONS)
def test_default_fallback_ignores_host_mime_table(
    tmp_path: Path,
    extension: str,
    detect_under_each_table: DetectUnderEachTable,
) -> None:
    """Every simulated MIME table gives the same full detection result."""
    results = detect_under_each_table(_opaque(tmp_path, extension))

    assert_that(_decisions(results["host"])).is_equal_to(
        ["decision=extension fallback"],
    )
    _assert_all_equal(results)


@pytest.mark.parametrize("extension", sorted(EXTENSION_MEDIA_TYPES))
def test_every_table_extension_is_host_independent(
    tmp_path: Path,
    extension: str,
    detect_under_each_table: DetectUnderEachTable,
) -> None:
    """Each extension in winnow's table is typed the same under every host."""
    results = detect_under_each_table(_opaque(tmp_path, f".{extension}"))

    _assert_all_equal(results)
    assert_that(results["host"].extension_media_type).is_equal_to(
        EXTENSION_MEDIA_TYPES[extension],
    )


@pytest.mark.parametrize(
    "extension",
    [".ktx", ".sid", ".ts", ".rm", ".caf", ".mts", ".mid", ".fit", ".bw", ".ac3"],
)
def test_extension_outside_the_table_is_unknown_on_every_host(
    tmp_path: Path,
    extension: str,
    detect_under_each_table: DetectUnderEachTable,
) -> None:
    """An extension the table leaves out gives no type, and says so."""
    results = detect_under_each_table(_opaque(tmp_path, extension))

    _assert_all_equal(results)
    assert_that(results["host"].media_type).is_none()
    assert_that(results["host"].signals[-1].detail).is_equal_to(
        f"suffix={extension} table=none",
    )


def test_table_entries_follow_the_membership_rule() -> None:
    """Each entry is a stable built-in answer, a registry format or rule 2."""
    for extension, media_type in EXTENSION_MEDIA_TYPES.items():
        expected: MediaType | None
        if extension in RULE_2_ADDITIONS:
            expected = RULE_2_ADDITIONS[extension]
        elif extension in DEFAULT_FORMATS:
            expected = DEFAULT_FORMATS[extension]
        else:
            expected = _builtin_media_type(extension)
        assert_that(media_type).described_as(extension).is_equal_to(expected)


def test_builtin_media_names_are_in_the_table_or_excluded() -> None:
    """Every name this Python's built-in table types as media is accounted for."""
    builtin_extensions = {
        extension.lstrip(".") for extension in _BUILTIN_ONLY.types_map[True]
    }
    for extension in sorted(builtin_extensions):
        media_type = _builtin_media_type(extension)
        if media_type is None or extension in EXCLUDED_EXTENSIONS:
            continue
        assert_that(EXTENSION_MEDIA_TYPES.get(extension)).described_as(
            extension,
        ).is_equal_to(media_type)


def test_host_dependent_additions_are_deliberate() -> None:
    """The table's rule 2 entries are exactly the ones this test lists."""
    assert_that(dict(HOST_DEPENDENT_ADDITIONS)).is_equal_to(RULE_2_ADDITIONS)


@pytest.mark.parametrize(
    ("extension", "expected"),
    list(REPRESENTATIVE_ANSWERS.items()),
)
def test_representative_default_answers(
    extension: str,
    expected: MediaType | None,
) -> None:
    """Representative names get the answer main gave on a stable host."""
    assert_that(media_type_for_extension(extension)).is_equal_to(expected)


def test_extension_table_leaves_out_excluded_and_playlist_extensions() -> None:
    """Excluded and playlist extensions are not in the table."""
    assert_that(EXTENSION_MEDIA_TYPES).does_not_contain_key(*EXCLUDED_EXTENSIONS)
    assert_that(EXTENSION_MEDIA_TYPES).does_not_contain_key(*PLAYLIST_EXTENSIONS)
    assert_that(EXTENSION_TABLE_NAME).is_equal_to(
        f"winnow/{EXTENSION_TABLE_VERSION}",
    )


@pytest.mark.parametrize("extension", sorted(PLAYLIST_EXTENSIONS))
def test_playlist_extensions_stay_non_media(
    tmp_path: Path,
    extension: str,
    detect_under_each_table: DetectUnderEachTable,
) -> None:
    """A playlist extension is non-media whatever the host table says."""
    results = detect_under_each_table(_opaque(tmp_path, f".{extension}"))

    _assert_all_equal(results)
    assert_that(results["host"].media_type).is_none()
    assert_that(results["host"].container).is_equal_to("playlist")


# --- Real non-media files that share a media name ---------------------------


def _fit_activity() -> bytes:
    """Build a Garmin FIT file: its 14-byte header, then binary records.

    Returns:
        The file.
    """
    records = bytes(range(0x40, 0x80)) * 4
    header = struct.pack("<BBHI4sH", 14, 0x20, 2132, len(records), b".FIT", 0)
    return header + records + b"\x00\x00"


def _bigwig() -> bytes:
    """Build the start of a BigWig file: its magic and header fields.

    Returns:
        The file.
    """
    header = struct.pack(
        "<IHHQQQHHQQIQ",
        0x888FFC26,
        4,
        1,
        64,
        128,
        256,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return header + bytes(range(0x80, 0x100)) * 2


def _ole_word_document() -> bytes:
    """Build a legacy OLE compound file header as Word 97 to 2003 writes it.

    Returns:
        One header sector and one empty sector.
    """
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
    header += struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6)
    header += b"\x00" * 6 + struct.pack("<IIIIIIII", 0, 1, 1, 0, 4096, 2, 1, 0)
    header = header.ljust(512, b"\xff")
    return header + b"\x00" * 512


def _ooxml_document() -> bytes:
    """Build a minimal OOXML word-processing package.

    Returns:
        The ZIP package.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        package.writestr("word/document.xml", '<?xml version="1.0"?><document/>')
    return buffer.getvalue()


def _zip_archive() -> bytes:
    """Build a ZIP archive as a Widelands map is distributed.

    Returns:
        The archive.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("map/elemental", "[global]\npacket_version=1\n")
    return buffer.getvalue()


MICROEMACS_MACRO = b"""; MicroEmacs macro file
define-macro hello-world
    ml-write "Hello, world"
!emacro
"""
"""A MicroEmacs macro, which uses the Enhanced Metafile's ``.emf``."""

ACCLAIM_SKELETON = b"""# AST/ASF file generated using VICON BodyLanguage
:version 1.10
:name VICON
:units
  mass 1.0
  length 0.45
  angle deg
:root
   order TX TY TZ RX RY RZ
   axis XYZ
"""
"""An Acclaim Skeleton File, the motion-capture text format named ``.asf``."""

NON_MEDIA_SAMPLES: dict[str, bytes] = {
    "activity.fit": _fit_activity(),
    "signal.bw": _bigwig(),
    "legacy-word.dds": _ole_word_document(),
    "ooxml-word.dds": _ooxml_document(),
    "hello.emf": MICROEMACS_MACRO,
    "snail.wmf": _zip_archive(),
    "skeleton.asf": ACCLAIM_SKELETON,
}
"""Non-media files named with extensions a media format also uses."""


@pytest.mark.parametrize("file_name", sorted(NON_MEDIA_SAMPLES))
def test_non_media_file_with_a_media_name_stays_unknown(
    tmp_path: Path,
    file_name: str,
) -> None:
    """A FIT, BigWig or Word file keeps no media type by name."""
    path = tmp_path / file_name
    path.write_bytes(NON_MEDIA_SAMPLES[file_name])

    result = detect(path)

    assert_that(result.media_type).is_none()
    assert_that(result.extension_media_type).is_none()
    assert_that(media_type_for_extension(file_name)).is_none()


def test_ooxml_named_dds_is_non_media_by_signature(tmp_path: Path) -> None:
    """An OOXML package named .dds is decided non-media by its ZIP signature."""
    path = tmp_path / "ooxml-word.dds"
    path.write_bytes(NON_MEDIA_SAMPLES["ooxml-word.dds"])

    result = detect(path)

    assert_that(result.container).is_equal_to("zip")
    assert_that(_decisions(result)).is_equal_to(
        ["decision=zip non-media signature"],
    )


def test_non_media_files_with_media_names_do_not_enter_discovery(
    tmp_path: Path,
) -> None:
    """Discovery admits none of the non-media samples."""
    for file_name, data in NON_MEDIA_SAMPLES.items():
        (tmp_path / file_name).write_bytes(data)
    result = PipelineResult(
        run=RunMetadata(started_at=datetime.now(tz=UTC), winnow_version="0.0.0"),
    )
    state = RunState(source=tmp_path, destination=tmp_path / "out", result=result)

    DiscoveryStep().run(state, context=PipelineContext.from_config(WinnowConfig()))

    assert_that(state.files).is_empty()


# --- Registry behaviour -----------------------------------------------------


def test_default_is_switchable_in_one_place() -> None:
    """Every way to build a registry takes the one default for the host table."""
    assert_that(DEFAULT_USE_MIME_FALLBACK).is_false()
    assert_that(FormatRegistry().use_mime_fallback).is_false()
    assert_that(create_default_format_registry().use_mime_fallback).is_false()
    assert_that(FormatRegistry.from_config().use_mime_fallback).is_false()
    assert_that(registry_module.DEFAULT_FORMAT_REGISTRY.use_mime_fallback).is_false()


def _stub_host_table(monkeypatch: pytest.MonkeyPatch, **types: str) -> None:
    """Replace the opt-in host table with one that knows only ``types``.

    Args:
        monkeypatch: Applies the replacement.
        **types: MIME type by extension without the dot.
    """
    host_table = mimetypes.MimeTypes(filenames=())
    host_table.types_map = ({}, {})
    for extension, mime_type in types.items():
        host_table.add_type(mime_type, f".{extension}")
    monkeypatch.setattr(registry_module, "_host_mime_types", lambda: host_table)


def test_mime_fallback_opt_in_uses_host_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a registry built with use_mime_fallback=True asks the host table."""
    _stub_host_table(monkeypatch, zzq="video/x-winnow-test")
    opaque = _opaque(tmp_path, ".zzq")

    opted_in = detect(opaque, registry=FormatRegistry(use_mime_fallback=True))
    default = detect(opaque)

    assert_that(opted_in.media_type).is_equal_to(VIDEO)
    assert_that(opted_in.signals[-1].detail).is_equal_to(
        "suffix=.zzq table=host-mime",
    )
    assert_that(default.media_type).is_none()
    assert_that(default.signals[-1].detail).is_equal_to("suffix=.zzq table=none")


def test_winnow_table_outranks_opted_in_host_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With defaults on, a conflicting host entry never replaces the table's."""
    _stub_host_table(monkeypatch, mkv="audio/x-winnow-test", mp4="image/x-test")
    registry = FormatRegistry(use_mime_fallback=True)

    assert_that(registry.lookup_with_table("clip.mkv")).is_equal_to(
        ExtensionLookup(media_type=VIDEO, table=EXTENSION_TABLE_NAME),
    )
    assert_that(registry.lookup_with_table("clip.mp4")).is_equal_to(
        ExtensionLookup(media_type=VIDEO, table=EXTENSION_TABLE_NAME),
    )


def test_mime_fallback_opt_in_reads_the_real_host_table() -> None:
    """The opt-in path reaches the host's own table, not only a stub."""
    registry = FormatRegistry(include_defaults=False, use_mime_fallback=True)

    assert_that(registry.lookup_with_table("photo.JPG")).is_equal_to(
        ExtensionLookup(media_type=IMAGE, table="host-mime"),
    )
    assert_that(registry.lookup_with_table("photo.dcr")).is_equal_to(
        ExtensionLookup(media_type=IMAGE, table="host-mime"),
    )


class _ActivityAudioRegistry(FormatRegistry):
    """A registry subclass that types ``.fit`` as audio and ``.mp3`` as nothing."""

    __slots__ = ()

    def lookup(self, extension: str) -> MediaType | None:
        """Type ``.fit`` as audio, ``.mp3`` as unknown, and defer the rest.

        Args:
            extension: Extension, file name or path.

        Returns:
            Audio for ``.fit``, ``None`` for ``.mp3``; otherwise the base
            registry's answer.
        """
        if extension.casefold().endswith("fit"):
            return AUDIO
        if extension.casefold().endswith("mp3"):
            return None
        return super().lookup(extension)


@pytest.mark.parametrize(
    ("file_name", "expected_type", "expected_detail"),
    [
        pytest.param("opaque.fit", AUDIO, "suffix=.fit table=registry", id="own"),
        pytest.param(
            "opaque.mkv",
            VIDEO,
            f"suffix=.mkv table={EXTENSION_TABLE_NAME}",
            id="deferred",
        ),
        pytest.param("opaque.zzq", None, "suffix=.zzq table=none", id="unknown"),
        pytest.param("opaque.mp3", None, "suffix=.mp3 table=none", id="suppressed"),
    ],
)
def test_subclass_lookup_still_types_detection(
    tmp_path: Path,
    file_name: str,
    expected_type: MediaType | None,
    expected_detail: str,
) -> None:
    """Detection honours a subclass's overridden lookup, as on main."""
    path = tmp_path / file_name
    path.write_bytes(OPAQUE_BYTES)

    result = detect(path, registry=_ActivityAudioRegistry())

    assert_that(result.media_type).is_equal_to(expected_type)
    assert_that(result.extension_media_type).is_equal_to(expected_type)
    assert_that(result.signals[-1].detail).is_equal_to(expected_detail)


@pytest.mark.parametrize(
    ("file_name", "registry_factory", "expected_detail", "expected_type"),
    [
        pytest.param(
            "opaque.MKV",
            FormatRegistry,
            f"suffix=.mkv table={EXTENSION_TABLE_NAME}",
            VIDEO,
            id="winnow-table",
        ),
        pytest.param(
            "opaque.nef",
            FormatRegistry,
            "suffix=.nef table=registry",
            IMAGE,
            id="registry-default",
        ),
        pytest.param(
            "opaque.clip",
            lambda: FormatRegistry(formats={"clip": "video"}),
            "suffix=.clip table=registry",
            VIDEO,
            id="user-registration",
        ),
        pytest.param(
            "opaque.zzq",
            FormatRegistry,
            "suffix=.zzq table=none",
            None,
            id="unknown",
        ),
        pytest.param(
            "opaque",
            FormatRegistry,
            "suffix=(none)",
            None,
            id="no-suffix",
        ),
        pytest.param(
            "taildot.",
            FormatRegistry,
            "suffix=(none)",
            None,
            id="trailing-dot",
        ),
    ],
)
def test_extension_signal_names_table(
    tmp_path: Path,
    file_name: str,
    registry_factory: Callable[[], FormatRegistry],
    expected_detail: str,
    expected_type: MediaType | None,
) -> None:
    """The extension signal says which table typed the extension."""
    path = tmp_path / file_name
    path.write_bytes(OPAQUE_BYTES)

    result = detect(path, registry=registry_factory())

    extension_signal = result.signals[-1]
    assert_that(extension_signal.source).is_equal_to("extension")
    assert_that(extension_signal.detail).is_equal_to(expected_detail)
    assert_that(extension_signal.media_type).is_equal_to(expected_type)
    assert_that(result.extension_media_type).is_equal_to(expected_type)


def test_trailing_dot_name_matches_a_name_without_extension(tmp_path: Path) -> None:
    """``name.`` and ``name`` give the same result on every Python version."""
    with_dot = tmp_path / "taildot."
    without = tmp_path / "taildot"
    with_dot.write_bytes(OPAQUE_BYTES)
    without.write_bytes(OPAQUE_BYTES)

    different_files = {"snapshot"}
    assert_that(detect(with_dot).model_dump(exclude=different_files)).is_equal_to(
        detect(without).model_dump(exclude=different_files),
    )


def test_import_does_not_mutate_global_mimetypes() -> None:
    """Importing winnow.media leaves the process-wide MIME table unchanged."""
    script = (
        "import json, mimetypes\n"
        "def snapshot():\n"
        "    return [mimetypes.guess_type('x.dcr'), dict(mimetypes.types_map)]\n"
        "before = snapshot()\n"
        "import winnow.media\n"
        "print(json.dumps(before == snapshot()))\n"
    )

    completed = run_script(
        [sys.executable, "-c", script],
        env=os.environ,
        cwd=Path(__file__).resolve().parents[2],
        timeout=60,
        check=True,
    )

    assert_that(json.loads(completed.stdout)).is_true()
