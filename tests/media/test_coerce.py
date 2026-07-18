"""Tests for the shared numeric coercion helpers."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.media._coerce import (
    coerce_non_negative_float,
    coerce_non_negative_int,
)


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", object()],
    ids=["none", "string", "object"],
)
def test_coerce_int_rejects_invalid(value: object) -> None:
    """Non-numeric or missing values coerce to None for integers."""
    assert_that(coerce_non_negative_int(value)).is_none()


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", object()],
    ids=["none", "string", "object"],
)
def test_coerce_float_rejects_invalid(value: object) -> None:
    """Non-numeric or missing values coerce to None for floats."""
    assert_that(coerce_non_negative_float(value)).is_none()


def test_coerce_int_rejects_negative() -> None:
    """Negative integers are rejected as None."""
    assert_that(coerce_non_negative_int(-5)).is_none()


def test_coerce_float_rejects_negative() -> None:
    """Negative floats are rejected as None."""
    assert_that(coerce_non_negative_float(-0.5)).is_none()


def test_coerce_int_rejects_decimal_string_by_default() -> None:
    """Decimal strings are invalid without via_float (mutagen semantics)."""
    assert_that(coerce_non_negative_int("12.5")).is_none()


def test_coerce_int_truncates_decimal_string_via_float() -> None:
    """Decimal strings truncate to integers with via_float (ffprobe)."""
    assert_that(coerce_non_negative_int("12.5", via_float=True)).is_equal_to(12)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), ("48000", 48000), (2.9, 2)],
    ids=["zero", "int-string", "float"],
)
def test_coerce_int_accepts_valid(value: object, expected: int) -> None:
    """Valid non-negative values parse to integers."""
    assert_that(coerce_non_negative_int(value)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0.0), ("12.5", 12.5), (3.25, 3.25)],
    ids=["zero", "string", "float"],
)
def test_coerce_float_accepts_valid(value: object, expected: float) -> None:
    """Valid non-negative values parse to floats."""
    assert_that(coerce_non_negative_float(value)).is_equal_to(expected)
