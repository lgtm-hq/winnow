"""The default extension fallback does not depend on the host (#464).

Each test writes bytes no content rule recognises, so rule 6 (the
extension) decides, then runs :func:`~winnow.media._detect.detect` while
the host MIME table answers from different simulated tables: the host's
own, CPython's built-in table alone, an empty table, and a table that gives
every extension a different media type. Both the process-wide
``mimetypes.guess_type`` and the registry's private opt-in table are
replaced. With the default registry every result must be the same.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from assertpy import assert_that

from tests.media.library_support import LIBRARY_SAMPLES
from tests.script_runner import run_script
from winnow.media import registry as registry_module
from winnow.media._detect import DetectionResult, detect
from winnow.media._extension_table import (
    EXCLUDED_EXTENSIONS,
    EXTENSION_MEDIA_TYPES,
    EXTENSION_TABLE_NAME,
    EXTENSION_TABLE_VERSION,
)
from winnow.media._format_checks import PLAYLIST_EXTENSIONS
from winnow.media.registry import (
    DEFAULT_FORMATS,
    DEFAULT_USE_MIME_FALLBACK,
    ExtensionLookup,
    FormatRegistry,
    create_default_format_registry,
)
from winnow.models.media import MediaType

GuessType = Callable[..., tuple[str | None, str | None]]
DetectUnderEachTable = Callable[[Path], dict[str, DetectionResult]]

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

_HOST_GUESS_TYPE: GuessType = mimetypes.guess_type
_BUILTIN_ONLY = mimetypes.MimeTypes(filenames=())
_ROTATED_PREFIX = {"audio": "video", "video": "image", "image": "audio"}


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


@pytest.mark.parametrize("extension", [".ktx", ".ass", ".sid", ".ts", ".rm"])
def test_extension_outside_the_table_is_unknown_on_every_host(
    tmp_path: Path,
    extension: str,
    detect_under_each_table: DetectUnderEachTable,
) -> None:
    """An extension only a host table knows gives no type, and says so."""
    results = detect_under_each_table(_opaque(tmp_path, extension))

    _assert_all_equal(results)
    assert_that(results["host"].media_type).is_none()
    assert_that(results["host"].signals[-1].detail).is_equal_to(
        f"suffix={extension} table=none",
    )


_CONTENT_RULE_EXTENSIONS: dict[str, MediaType] = {
    # Matroska and WebM (_matroska.py).
    "mkv": MediaType.VIDEO,
    "mk3d": MediaType.VIDEO,
    "webm": MediaType.VIDEO,
    "mka": MediaType.AUDIO,
    "weba": MediaType.AUDIO,
    # ISO-BMFF brands (_detect_bmff.py).
    "mp4": MediaType.VIDEO,
    "m4v": MediaType.VIDEO,
    "mov": MediaType.VIDEO,
    "3gp": MediaType.VIDEO,
    "3g2": MediaType.VIDEO,
    "crm": MediaType.VIDEO,
    "m4a": MediaType.AUDIO,
    "m4b": MediaType.AUDIO,
    "m4p": MediaType.AUDIO,
    "f4a": MediaType.AUDIO,
    "f4b": MediaType.AUDIO,
    "heic": MediaType.IMAGE,
    "heif": MediaType.IMAGE,
    "hif": MediaType.IMAGE,
    "heics": MediaType.IMAGE,
    "avif": MediaType.IMAGE,
    "avifs": MediaType.IMAGE,
    "cr3": MediaType.IMAGE,
    # Audio format names ffprobe facts are accepted for (_audio_rules.py).
    "mp3": MediaType.AUDIO,
    "aac": MediaType.AUDIO,
    "ac3": MediaType.AUDIO,
    "eac3": MediaType.AUDIO,
    "flac": MediaType.AUDIO,
    "wv": MediaType.AUDIO,
    "tta": MediaType.AUDIO,
    "wav": MediaType.AUDIO,
    "aiff": MediaType.AUDIO,
    "ogg": MediaType.AUDIO,
    "wma": MediaType.AUDIO,
    "asf": MediaType.VIDEO,
    "avi": MediaType.VIDEO,
    "m2ts": MediaType.VIDEO,
    "mpg": MediaType.VIDEO,
    "flv": MediaType.VIDEO,
    "mj2": MediaType.VIDEO,
    # Signature formats with no library sample (_signatures.py).
    "jpg": MediaType.IMAGE,
    "png": MediaType.IMAGE,
    "gif": MediaType.IMAGE,
    "bmp": MediaType.IMAGE,
    "tif": MediaType.IMAGE,
    "webp": MediaType.IMAGE,
    "ico": MediaType.IMAGE,
    "jp2": MediaType.IMAGE,
    "svg": MediaType.IMAGE,
    "caf": MediaType.AUDIO,
    "au": MediaType.AUDIO,
    "opus": MediaType.AUDIO,
    "dts": MediaType.AUDIO,
}
"""Extensions of formats the content rules recognise, with their types."""

_NO_CONVENTIONAL_EXTENSION = frozenset({"xcur"})
"""Library sample extensions no format convention uses (X cursors have none)."""


def test_extension_table_matches_documented_formats() -> None:
    """Every format the content rules recognise has its extension typed."""
    expected = dict(_CONTENT_RULE_EXTENSIONS)
    for _, extension, _, _, media_type in LIBRARY_SAMPLES:
        if extension in EXCLUDED_EXTENSIONS or extension in _NO_CONVENTIONAL_EXTENSION:
            continue
        expected[extension] = media_type
    expected.update(DEFAULT_FORMATS)

    for extension, media_type in expected.items():
        assert_that(EXTENSION_MEDIA_TYPES.get(extension)).described_as(
            extension,
        ).is_equal_to(media_type)


def test_extension_table_leaves_out_excluded_and_playlist_extensions() -> None:
    """Ambiguous and playlist extensions are not in the table."""
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


def test_default_is_switchable_in_one_place() -> None:
    """Every way to build a registry takes the one default for the host table."""
    assert_that(DEFAULT_USE_MIME_FALLBACK).is_false()
    assert_that(FormatRegistry().use_mime_fallback).is_false()
    assert_that(create_default_format_registry().use_mime_fallback).is_false()
    assert_that(FormatRegistry.from_config().use_mime_fallback).is_false()
    assert_that(registry_module.DEFAULT_FORMAT_REGISTRY.use_mime_fallback).is_false()


def test_mime_fallback_opt_in_uses_host_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a registry built with use_mime_fallback=True asks the host table."""
    host_table = mimetypes.MimeTypes(filenames=())
    host_table.add_type("video/x-winnow-test", ".zzq")
    monkeypatch.setattr(registry_module, "_host_mime_types", lambda: host_table)
    opaque = _opaque(tmp_path, ".zzq")

    opted_in = detect(opaque, registry=FormatRegistry(use_mime_fallback=True))
    default = detect(opaque)

    assert_that(opted_in.media_type).is_equal_to(MediaType.VIDEO)
    assert_that(opted_in.signals[-1].detail).is_equal_to(
        "suffix=.zzq table=host-mime",
    )
    assert_that(default.media_type).is_none()
    assert_that(default.signals[-1].detail).is_equal_to("suffix=.zzq table=none")


def test_mime_fallback_opt_in_reads_the_real_host_table() -> None:
    """The opt-in path reaches the host's own table, not only a stub."""
    registry = FormatRegistry(include_defaults=False, use_mime_fallback=True)

    assert_that(registry.lookup_with_table("photo.JPG")).is_equal_to(
        ExtensionLookup(media_type=MediaType.IMAGE, table="host-mime"),
    )
    assert_that(registry.lookup_with_table("photo.dcr")).is_equal_to(
        ExtensionLookup(media_type=MediaType.IMAGE, table="host-mime"),
    )


@pytest.mark.parametrize(
    ("file_name", "registry_factory", "expected_detail", "expected_type"),
    [
        pytest.param(
            "opaque.MKV",
            FormatRegistry,
            f"suffix=.mkv table={EXTENSION_TABLE_NAME}",
            MediaType.VIDEO,
            id="winnow-table",
        ),
        pytest.param(
            "opaque.nef",
            FormatRegistry,
            "suffix=.nef table=registry",
            MediaType.IMAGE,
            id="registry-default",
        ),
        pytest.param(
            "opaque.clip",
            lambda: FormatRegistry(formats={"clip": "video"}),
            "suffix=.clip table=registry",
            MediaType.VIDEO,
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
