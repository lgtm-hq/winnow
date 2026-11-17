"""One fixed corpus of ``WINNOW_*`` values, cast the same on every Python.

``env_corpus.json`` holds 292 raw environment strings: booleans, integers,
floats, dates and times (with and without seconds), quoting and escapes,
comments, arrays, inline tables, nested forms, Windows paths, bare text,
markers and whitespace. ``main`` is what ``main`` (Dynaconf 3.3.5) produced
for each, recorded identically on Python 3.11 and 3.14; any error counts as
``["error"]``. Where winnow differs on purpose the entry also has ``winnow``
(what winnow produces) and ``why``. CI runs this file on every Python version
in the test matrix, so the casting cannot depend on the interpreter's
``tomllib``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config._env import _load_env_data

_CORPUS: list[dict[str, object]] = json.loads(
    (Path(__file__).resolve().parent / "env_corpus.json").read_text(encoding="utf-8"),
)


_SCALARS: tuple[tuple[type, str], ...] = (
    (bool, "bool"),
    (int, "int"),
    (float, "float"),
    (str, "str"),
)


def _canonical(value: object) -> object:
    """Encode a cast value as JSON-comparable ``[type, value]`` data."""
    for scalar_type, name in _SCALARS:
        if isinstance(value, scalar_type):
            encoded: object = value if scalar_type in (bool, str) else repr(value)
            return [name, encoded]
    if value is None:
        return ["none"]
    if isinstance(value, datetime | date | time):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, Mapping):
        return ["dict", [[str(key), _canonical(item)] for key, item in value.items()]]
    if isinstance(value, list):
        return ["list", [_canonical(item) for item in value]]
    return ["other", type(value).__name__]


def _cast(raw: str) -> object:
    """Cast one raw value as winnow does, any error read as ``["error"]``."""
    try:
        data, _ = _load_env_data({"WINNOW_VALUE": raw})
    except ValueError:
        return ["error"]
    return _canonical(data["value"])


def test_corpus_is_large_enough() -> None:
    """The corpus holds at least 250 distinct values."""
    raws = [entry["raw"] for entry in _CORPUS]

    assert_that(len(set(raws))).is_greater_than_or_equal_to(250)
    assert_that(raws).is_length(len(set(raws)))


def test_every_difference_says_why() -> None:
    """Each entry that differs from main explains the difference."""
    for entry in _CORPUS:
        if "winnow" in entry:
            assert_that(entry).contains_key("why")
            assert_that(entry["winnow"]).is_not_equal_to(entry["main"])


@pytest.mark.parametrize(
    "entry",
    _CORPUS,
    ids=[repr(entry["raw"])[:40] for entry in _CORPUS],
)
def test_env_value_matches_corpus(entry: dict[str, object]) -> None:
    """Each value casts to what main gave, or to the listed difference."""
    expected = entry.get("winnow", entry["main"])

    assert_that(_cast(str(entry["raw"]))).is_equal_to(expected)
