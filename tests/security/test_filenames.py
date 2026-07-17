"""Tests for filename sanitization helpers."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.exceptions import SecurityError
from winnow.security.filenames import DEFAULT_MAX_LENGTH, sanitize_filename


def test_sanitize_filename_passes_through_safe_name() -> None:
    """A clean filename is returned unchanged."""
    assert_that(sanitize_filename("holiday_photo.jpg")).is_equal_to("holiday_photo.jpg")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a/b.jpg", "a_b.jpg"),
        ("a\\b.jpg", "a_b.jpg"),
        ("nested/deep/name.mov", "nested_deep_name.mov"),
    ],
    ids=["forward_slash", "backslash", "multiple_separators"],
)
def test_sanitize_filename_replaces_path_separators(raw: str, expected: str) -> None:
    """Path separators are replaced so the result is a single component."""
    assert_that(sanitize_filename(raw)).is_equal_to(expected)


def test_sanitize_filename_replaces_nul_byte() -> None:
    """NUL bytes are replaced with the replacement string."""
    assert_that(sanitize_filename("na\x00me.png")).is_equal_to("na_me.png")


def test_sanitize_filename_replaces_control_characters() -> None:
    """Control characters such as newlines and tabs are replaced."""
    assert_that(sanitize_filename("na\tme\nfile.png")).is_equal_to("na_me_file.png")


def test_sanitize_filename_strips_leading_and_trailing_dots_and_spaces() -> None:
    """Surrounding dots and whitespace are stripped from the result."""
    assert_that(sanitize_filename("  ..photo.jpg..  ")).is_equal_to("photo.jpg")


def test_sanitize_filename_uses_custom_replacement() -> None:
    """A custom replacement string substitutes forbidden characters."""
    assert_that(
        sanitize_filename("a/b.jpg", replacement="-"),
    ).is_equal_to("a-b.jpg")


def test_sanitize_filename_preserves_unicode() -> None:
    """Unicode characters are retained and normalized to NFC form."""
    result = sanitize_filename("café_ünïcode.jpg")

    assert_that(result).is_equal_to("café_ünïcode.jpg")


def test_sanitize_filename_truncates_to_max_length() -> None:
    """Overlong filenames are truncated to the maximum length."""
    raw = "x" * 400

    result = sanitize_filename(raw)

    assert_that(len(result)).is_equal_to(DEFAULT_MAX_LENGTH)


def test_sanitize_filename_respects_custom_max_length() -> None:
    """A custom maximum length bounds the returned filename."""
    result = sanitize_filename("abcdefgh.jpg", max_length=4)

    assert_that(result).is_equal_to("abcd")


@pytest.mark.parametrize(
    "raw",
    ["", "   ", ".", "..", "...", " . . "],
    ids=["empty", "spaces", "dot", "dotdot", "dots", "dots_and_spaces"],
)
def test_sanitize_filename_rejects_unsafe_input(raw: str) -> None:
    """Inputs that cannot yield a safe filename raise SecurityError."""
    with pytest.raises(SecurityError, match="cannot be sanitized"):
        sanitize_filename(raw)


@pytest.mark.parametrize(
    "raw",
    ["/", "\x00"],
    ids=["separator", "nul"],
)
def test_sanitize_filename_reduces_forbidden_only_input_to_replacement(
    raw: str,
) -> None:
    """Input made solely of forbidden characters collapses to the replacement."""
    assert_that(sanitize_filename(raw)).is_equal_to("_")


def test_sanitize_filename_rejects_unsafe_replacement() -> None:
    """A replacement containing a path separator is rejected."""
    with pytest.raises(SecurityError, match="replacement"):
        sanitize_filename("name.jpg", replacement="/")


def test_sanitize_filename_rejects_non_positive_max_length() -> None:
    """A non-positive max_length is rejected."""
    with pytest.raises(SecurityError, match="max_length"):
        sanitize_filename("name.jpg", max_length=0)


def test_sanitize_filename_trims_trailing_dot_after_truncation() -> None:
    """Truncation removes any trailing dot exposed by the cut."""
    assert_that(sanitize_filename("ab.cdef", max_length=3)).is_equal_to("ab")
