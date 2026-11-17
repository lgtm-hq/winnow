"""Characterization of how the config file is read into a raw mapping.

These tests pin the mapping the reader hands to validation, so the YAML backend
behind it can change without changing what a config file means.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import CONFIG_FILE_NAME, load_config
from winnow.config._reader import _load_config_data
from winnow.exceptions import ConfigError


def _read(tmp_path: Path, text: str) -> dict[str, object]:
    """Write ``text`` to a config file and read it without the environment.

    Args:
        tmp_path: Directory for the config file.
        text: YAML document to write.

    Returns:
        The normalized raw mapping the reader returns.
    """
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text(text, encoding="utf-8")
    return _load_config_data(config_path, load_env=False)


def _read_error(tmp_path: Path, text: str) -> Exception:
    """Return the exception the reader raises for ``text``.

    Args:
        tmp_path: Directory for the config file.
        text: YAML document to write.

    Returns:
        The exception raised while reading.
    """
    try:
        _read(tmp_path, text)
    except Exception as exc:  # the type itself is under test
        return exc
    pytest.fail("expected the reader to fail")


@pytest.mark.parametrize(
    "text",
    ["", "\n\n", "# only a comment\n# and another\n", "---\n"],
    ids=["empty", "blank-lines", "comments", "empty-document"],
)
def test_reader_returns_empty_mapping_for_documents_without_settings(
    tmp_path: Path,
    text: str,
) -> None:
    """A file with no settings reads as an empty mapping."""
    assert_that(_read(tmp_path, text)).is_empty()


def test_reader_without_file_returns_empty_mapping() -> None:
    """No config file reads as an empty mapping."""
    assert_that(_load_config_data(None, load_env=False)).is_empty()


def test_reader_keeps_nested_keys(tmp_path: Path) -> None:
    """Nested mappings and lists are returned as plain nested data."""
    data = _read(
        tmp_path,
        "cache:\n"
        "  enabled: false\n"
        "  directory: /srv/winnow-cache\n"
        "paths:\n"
        "  include_patterns:\n"
        "    - '*.jpg'\n"
        "    - '*.png'\n"
        "routing:\n"
        "  screenshots: Shots\n",
    )

    assert_that(data).is_equal_to(
        {
            "cache": {"enabled": False, "directory": "/srv/winnow-cache"},
            "paths": {"include_patterns": ["*.jpg", "*.png"]},
            "routing": {"screenshots": "Shots"},
        },
    )


def test_reader_lowercases_mixed_case_keys(tmp_path: Path) -> None:
    """Keys are lowercased at every level."""
    data = _read(tmp_path, "Workers: 3\nCACHE:\n  Enabled: false\n")

    assert_that(data).is_equal_to({"workers": 3, "cache": {"enabled": False}})


def test_reader_drops_unknown_top_level_keys(tmp_path: Path) -> None:
    """Keys that are not settings are dropped, including Dynaconf's old ones."""
    data = _read(tmp_path, "bogus: 1\ndynaconf_merge: true\nworkers: 2\n")

    assert_that(data).is_equal_to({"workers": 2})


def test_reader_resolves_anchors_and_aliases(tmp_path: Path) -> None:
    """YAML aliases resolve to the anchored value."""
    data = _read(tmp_path, "unused: &count 3\nworkers: *count\n")

    assert_that(data).is_equal_to({"workers": 3})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # YAML 1.2 core schema: only true/false are booleans.
        ("dry_run: true\n", {"dry_run": True}),
        ("dry_run: yes\n", {"dry_run": "yes"}),
        ("dry_run: 'true'\n", {"dry_run": "true"}),
        # YAML 1.2 integers: a leading zero is decimal, not octal.
        ("workers: 010\n", {"workers": 10}),
        ("workers: 0o10\n", {"workers": 8}),
        ("workers: 0x10\n", {"workers": 16}),
        ("workers: 1:30\n", {"workers": "1:30"}),
        ("min_similarity: 0.5\n", {"min_similarity": 0.5}),
        ("paths:\n  output_dir: null\n", {"paths": {"output_dir": None}}),
        ("paths:\n  output_dir: ~\n", {"paths": {"output_dir": None}}),
        (
            "routing:\n  screenshots: 2024-01-01\n",
            {"routing": {"screenshots": date(2024, 1, 1)}},
        ),
    ],
    ids=[
        "true",
        "yes",
        "quoted-true",
        "leading-zero",
        "octal",
        "hex",
        "sexagesimal",
        "float",
        "null",
        "tilde",
        "date",
    ],
)
def test_reader_scalar_types(
    tmp_path: Path,
    text: str,
    expected: dict[str, object],
) -> None:
    """Scalars are typed by YAML 1.2 rules."""
    assert_that(_read(tmp_path, text)).is_equal_to(expected)


def test_reader_reads_infinity(tmp_path: Path) -> None:
    """``.inf`` reads as a float infinity."""
    data = _read(tmp_path, "min_similarity: .inf\n")

    assert_that(data["min_similarity"]).is_equal_to(math.inf)


@pytest.mark.parametrize(
    "text",
    ["workers: [1, 2\n", "workers:\t2\n", "cache: {enabled: true\n"],
    ids=["unclosed-list", "tab-indent", "unclosed-mapping"],
)
def test_reader_wraps_invalid_documents(tmp_path: Path, text: str) -> None:
    """An invalid document raises ConfigError naming the file."""
    error = _read_error(tmp_path, text)

    assert_that(error).is_instance_of(ConfigError)
    assert_that(str(error)).contains("load_config")
    assert_that(str(error)).contains(str(tmp_path / CONFIG_FILE_NAME))


@pytest.mark.parametrize(
    "text",
    ["workers: 2\nworkers: 3\n", "- a\n- b\n", "just a string\n"],
    ids=["duplicate-key", "list-top-level", "scalar-top-level"],
)
def test_reader_rejects_documents_that_are_not_one_mapping(
    tmp_path: Path,
    text: str,
) -> None:
    """Duplicate keys and a non-mapping top level raise ConfigError.

    Before Dynaconf was removed these escaped as the backend's own errors
    (``DuplicateKeyError``, ``AttributeError``) instead of ``ConfigError``.
    """
    error = _read_error(tmp_path, text)

    assert_that(error).is_instance_of(ConfigError)
    assert_that(str(error)).contains("load_config")
    assert_that(str(error)).contains(str(tmp_path / CONFIG_FILE_NAME))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('workers: "@int 4"\n', {"workers": "@int 4"}),
        (
            'routing:\n  screenshots: "@upper shots"\n',
            {"routing": {"screenshots": "@upper shots"}},
        ),
    ],
    ids=["int-marker", "upper-marker"],
)
def test_reader_keeps_marker_strings_in_files(
    tmp_path: Path,
    text: str,
    expected: dict[str, object],
) -> None:
    """``@``-markers inside YAML strings are plain text.

    Dynaconf applied them in files too (``"@int 4"`` read as ``4``). YAML has
    its own types, so the file reader no longer interprets them.
    """
    assert_that(_read(tmp_path, text)).is_equal_to(expected)


def test_existing_config_round_trips_through_load(tmp_path: Path) -> None:
    """A full config file loads into the expected validated settings."""
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "hash_algorithm: phash\n"
        "dry_run: false\n"
        "workers: 4\n"
        "media_categories:\n"
        "  - photos\n"
        "source_dirs:\n"
        "  - /srv/winnow-src\n"
        "routing:\n"
        "  min_confidence: 0.5\n",
        encoding="utf-8",
    )

    config = load_config(
        cwd=tmp_path, home_config_dir=tmp_path / "home", load_env=False
    )

    assert_that(config.dry_run).is_false()
    assert_that(config.workers).is_equal_to(4)
    assert_that(config.source_dirs).is_equal_to([Path("/srv/winnow-src")])
    assert_that(config.routing.min_confidence).is_equal_to(0.5)
