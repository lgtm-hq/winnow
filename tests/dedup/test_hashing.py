"""Tests for perceptual-hash parsing and Hamming-distance comparison."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.dedup.hashing import HashFormat, hamming_distance, parse_hash
from winnow.exceptions import DuplicateError


@pytest.mark.parametrize(
    ("value", "expected_int", "expected_bits"),
    [
        ("ff", 255, 8),
        ("0xff", 255, 8),
        ("f00f", 0xF00F, 16),
        ("0x00", 0, 8),
    ],
    ids=["hex", "hex_prefixed", "hex_word", "hex_zero"],
)
def test_parse_hash_decodes_hex(
    value: str,
    expected_int: int,
    expected_bits: int,
) -> None:
    """Hex strings decode to their integer value and four bits per digit."""
    assert_that(parse_hash(value)).is_equal_to((expected_int, expected_bits))


@pytest.mark.parametrize(
    ("value", "expected_int", "expected_bits"),
    [
        ("1100", 12, 4),
        ("0000", 0, 4),
        ("1", 1, 1),
        ("10000000", 128, 8),
    ],
    ids=["nibble", "zeros", "single_bit", "byte"],
)
def test_parse_hash_decodes_bitstring(
    value: str,
    expected_int: int,
    expected_bits: int,
) -> None:
    """Auto-detection treats all-binary strings as one bit per character."""
    assert_that(parse_hash(value)).is_equal_to((expected_int, expected_bits))


def test_auto_detection_prefers_bitstring_for_binary_only_strings() -> None:
    """An ambiguous all-binary string is decoded as a bitstring, not hex."""
    assert_that(parse_hash("00")).is_equal_to((0, 2))


def test_parse_hash_respects_explicit_hex_format() -> None:
    """An explicit hex format decodes an ambiguous binary-looking string as hex."""
    assert_that(parse_hash("1100", hash_format=HashFormat.HEX)).is_equal_to(
        (0x1100, 16),
    )


def test_parse_hash_respects_explicit_bitstring_format() -> None:
    """An explicit bitstring format rejects non-binary digits."""
    with pytest.raises(DuplicateError, match="invalid bitstring"):
        parse_hash("ff", hash_format=HashFormat.BITSTRING)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "0x"],
    ids=["empty", "whitespace", "prefix_only"],
)
def test_parse_hash_rejects_empty_input(value: str) -> None:
    """Empty or digit-less hash strings raise a duplicate error."""
    with pytest.raises(DuplicateError):
        parse_hash(value)


def test_parse_hash_rejects_invalid_hex() -> None:
    """A string with non-hex characters raises a duplicate error."""
    with pytest.raises(DuplicateError, match="invalid hex"):
        parse_hash("xyz")


@pytest.mark.parametrize(
    ("hash_a", "hash_b", "expected"),
    [
        ("ff", "ff", 0),
        ("f0", "f3", 2),
        ("0x00", "ff", 8),
        ("1100", "1010", 2),
    ],
    ids=["identical", "hex_two_bits", "hex_all_bits", "bitstring_two_bits"],
)
def test_hamming_distance_counts_differing_bits(
    hash_a: str,
    hash_b: str,
    expected: int,
) -> None:
    """Hamming distance counts differing bits for equal-width hashes."""
    assert_that(hamming_distance(hash_a, hash_b)).is_equal_to(expected)


def test_hamming_distance_rejects_mismatched_bit_lengths() -> None:
    """Comparing hashes of different bit lengths raises a duplicate error."""
    with pytest.raises(DuplicateError, match="different bit lengths"):
        hamming_distance("ff", "ffff")
