"""Characterization of how ``WINNOW_*`` environment values are cast.

Each row records what the loader produces for one raw environment string, so the
parser behind ``_load_env_data`` can change without silently changing the
values users get.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import cast

import pytest
from assertpy import assert_that

from winnow.config import CONFIG_FILE_NAME, load_config
from winnow.config._env import _load_env_data
from winnow.exceptions import ConfigError

_KEPT_FORMS: list[tuple[str, object]] = [
    # Booleans: TOML lower case, plus the capitalized Python spelling.
    ("true", True),
    ("false", False),
    ("True", True),
    ("False", False),
    (" True ", True),
    # Anything else that looks boolean stays a string; Pydantic decides later.
    ("TRUE", "TRUE"),
    ("yes", "yes"),
    ("off", "off"),
    # Integers in TOML syntax.
    ("0", 0),
    ("42", 42),
    ("-7", -7),
    ("+5", 5),
    ("1_000", 1000),
    ("0x10", 16),
    ("0o7", 7),
    ("0b11", 3),
    # Floats in TOML syntax.
    ("3.14", 3.14),
    ("1e3", 1000.0),
    ("-1.5e-3", -0.0015),
    ("inf", math.inf),
    ("+inf", math.inf),
    # Not TOML numbers, so they stay strings.
    (".5", ".5"),
    ("12:30", "12:30"),
    # Arrays and inline tables.
    ("[1, 2]", [1, 2]),
    ('["a", "b"]', ["a", "b"]),
    ('[1, "a", true]', [1, "a", True]),
    ("[a, b]", "[a, b]"),
    ("{a = 1}", {"a": 1}),
    ('{"a": 1}', '{"a": 1}'),
    # Quoted strings lose their quotes; bare text is kept verbatim.
    ('"quoted"', "quoted"),
    ("'single'", "single"),
    ('""', ""),
    ('" "', " "),
    ('"""x"""', "x"),
    ("plain", "plain"),
    ("hello world", "hello world"),
    ("a=b", "a=b"),
    ("", ""),
    ("  ", "  "),
    ("null", "null"),
    ("None", "None"),
    ("~", "~"),
    ("/srv/winnow-output", "/srv/winnow-output"),
    # TOML dates and date-times.
    ("2024-01-01", date(2024, 1, 1)),
    ("2024-01-01T10:00:00", datetime(2024, 1, 1, 10, 0)),
    # Explicit cast markers.
    ("@int 5", 5),
    ("@float 1.5", 1.5),
    ("@bool true", True),
    ("@bool yes", True),
    ("@bool maybe", False),
    ("@str 5", "5"),
    ("@json [1, 2]", [1, 2]),
    ('@json {"a": 1}', {"a": 1}),
    ("@none", None),
    ("@null", None),
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    _KEPT_FORMS,
    ids=[repr(raw) for raw, _ in _KEPT_FORMS],
)
def test_env_value_casting(raw: str, expected: object) -> None:
    """Each environment string is cast to the recorded value and type."""
    data, _ = _load_env_data({"WINNOW_SETTING": raw})

    value = data["setting"]

    assert_that(value).is_equal_to(expected)
    assert_that(value).is_instance_of(type(expected))
    assert_that(isinstance(value, bool)).is_equal_to(isinstance(expected, bool))


def test_env_value_casting_nan() -> None:
    """``nan`` becomes a float NaN, which never equals itself."""
    data, _ = _load_env_data({"WINNOW_SETTING": "nan"})

    value = data["setting"]

    assert_that(value).is_instance_of(float)
    assert_that(math.isnan(cast("float", value))).is_true()


@pytest.mark.parametrize("raw", ["@int abc", "@float x", "@json {bad"])
def test_env_value_invalid_cast_marker_fails_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """A cast marker whose value cannot be cast stops the config load."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n", encoding="utf-8")
    monkeypatch.setenv("WINNOW_WORKERS", raw)

    error = _load_error(tmp_path)

    # On main the parser's own exception escapes instead of ConfigError.
    assert_that(isinstance(error, ConfigError)).is_false()


def _load_error(tmp_path: Path) -> Exception:
    """Return the exception ``load_config`` raises for ``tmp_path``.

    Args:
        tmp_path: Working directory holding the config file.

    Returns:
        The exception raised while loading.
    """
    try:
        load_config(cwd=tmp_path, home_config_dir=tmp_path / "home")
    except Exception as exc:  # the type itself is under test
        return exc
    pytest.fail("expected load_config to fail")


# Dynaconf string utilities and placeholders: plain values on main.
_DYNACONF_ONLY_PLAIN_FORMS: list[tuple[str, object]] = [
    ("@upper abc", "ABC"),
    ("@lower ABC", "abc"),
    ("@title ab cd", "Ab Cd"),
    ("@capitalize ab", "Ab"),
    ("@strip  x ", "x"),
    ("@lstrip  x", "x"),
    ("@rstrip x ", "x"),
    ("@casefold AB", "ab"),
    ("@swapcase aB", "Ab"),
    ("@split a b", ["a", "b"]),
    ("@note x", None),
    ("@comment x", None),
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    _DYNACONF_ONLY_PLAIN_FORMS,
    ids=[repr(raw) for raw, _ in _DYNACONF_ONLY_PLAIN_FORMS],
)
def test_env_value_dynaconf_only_markers(raw: str, expected: object) -> None:
    """Dynaconf's string-utility and placeholder markers, as cast on main."""
    data, _ = _load_env_data({"WINNOW_SETTING": raw})

    assert_that(data["setting"]).is_equal_to(expected)


_DYNACONF_ONLY_OBJECT_FORMS: list[str] = [
    "@empty",
    "@format {this}",
    "@jinja x",
    "@get foo",
    "@read_file /etc/hosts",
    "@del",
    "@merge [1]",
    "@merge_unique [1]",
    "@insert 0 x",
]


@pytest.mark.parametrize("raw", _DYNACONF_ONLY_OBJECT_FORMS)
def test_env_value_dynaconf_only_objects(raw: str) -> None:
    """Template, file and merge markers become Dynaconf objects on main."""
    data, _ = _load_env_data({"WINNOW_SETTING": raw})

    assert_that(type(data["setting"]).__module__).starts_with("dynaconf.")
