"""Tests for perceptual-hash digest parsing and Hamming distance."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.exceptions import HashError
from winnow.hash import PerceptualHash, hamming_distance, parse_digest
from winnow.models.enums import HashAlgorithm


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ff", (255, 8)),
        ("0xff", (255, 8)),
        ("0XFF", (255, 8)),
        ("  ff  ", (255, 8)),
        ("phash:8:8000000000000000", (1 << 63, 64)),
        ("0101", (0x101, 16)),
    ],
    ids=[
        "bare_hex",
        "0x_prefix",
        "0X_prefix_uppercase",
        "surrounding_whitespace",
        "serialized_form",
        "binary_looking_string_is_hex",
    ],
)
def test_parse_digest_decodes_value_and_bit_length(
    value: str,
    expected: tuple[int, int],
) -> None:
    """Bare hex and serialized forms decode to ``(value, bit_length)``."""
    assert_that(parse_digest(value)).is_equal_to(expected)


def test_parse_digest_accepts_serialize_output() -> None:
    """The output of ``PerceptualHash.serialize()`` round-trips through the parser."""
    hashed = PerceptualHash(
        algorithm=HashAlgorithm.DHASH,
        hash_size=8,
        digest="d1c3a5e7f0b29648",
    )

    assert_that(parse_digest(hashed.serialize())).is_equal_to(
        parse_digest(hashed.digest),
    )


@pytest.mark.parametrize(
    "value",
    ["", "  ", "zz", "0x", "phash:8:00", "sha256:ab", "phash:x:00", "phash:8:zz"],
    ids=[
        "empty",
        "whitespace_only",
        "non_hex",
        "prefix_without_digits",
        "digest_shorter_than_hash_size",
        "content_hash_algorithm",
        "non_integer_hash_size",
        "serialized_non_hex_digest",
    ],
)
def test_parse_digest_rejects_invalid_input(value: str) -> None:
    """Invalid strings raise ``HashError`` rather than a builtin exception."""
    with pytest.raises(HashError):
        parse_digest(value)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("ff", "00", 8),
        ("phash:8:8000000000000000", "0000000000000000", 1),
        ("abcdef0123456789", "abcdef0123456789", 0),
        ("0x00ff", "0xff00", 16),
    ],
    ids=["all_bits", "serialized_vs_bare", "identical", "prefixed"],
)
def test_hamming_distance_counts_differing_bits(
    left: str,
    right: str,
    expected: int,
) -> None:
    """Hamming distance counts differing bits across both string forms."""
    assert_that(hamming_distance(left, right)).is_equal_to(expected)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("d1c3a5e7f0b29648", "d1c3a5e7f0b29649", 1),
        ("0f0f0f0f0f0f0f0f", "f0f0f0f0f0f0f0f0", 64),
        ("00ff", "ff00", 16),
        ("a1b7c618e91a6f64", "f4c3934db4473a31", 34),
        (
            "a1f6b2a4c6ca18f2e18e1a3a6fe264fb5589e0dce6cb3b0e8ca2349dc8b730d8",
            "f423c7f1930f4d25b4db47673ab731a600dcb589b31e665b51f541c89d226585",
            142,
        ),
    ],
    ids=["one_bit", "all_bits_64", "all_bits_16", "phash_8", "phash_16"],
)
def test_hamming_distance_matches_pre_refactor_values(
    left: str,
    right: str,
    expected: int,
) -> None:
    """Distances pinned from the pre-refactor implementations are unchanged.

    The expected values were computed on ``main @ 410ca86`` with both
    ``winnow.hash.hamming_distance`` and ``winnow.dedup.hamming_distance``,
    which agreed on every pair.
    """
    assert_that(hamming_distance(left, right)).is_equal_to(expected)


def test_hamming_distance_rejects_mismatched_bit_lengths() -> None:
    """Digests of different widths raise with both bit lengths in details."""
    with pytest.raises(HashError, match="different sizes") as error:
        hamming_distance("ff", "ffff")

    assert_that(error.value.context.operation).is_equal_to("hamming_distance")
    assert_that(error.value.context.details["left_bits"]).is_equal_to(8)
    assert_that(error.value.context.details["right_bits"]).is_equal_to(16)


def test_hamming_distance_rejects_mismatched_algorithms() -> None:
    """Two self-describing digests naming different algorithms do not compare."""
    with pytest.raises(HashError, match="different algorithms") as error:
        hamming_distance("ahash:8:8000000000000000", "phash:8:8000000000000000")

    assert_that(error.value.context.operation).is_equal_to("hamming_distance")
    assert_that(error.value.context.details["left_algorithm"]).is_equal_to("ahash")
    assert_that(error.value.context.details["right_algorithm"]).is_equal_to("phash")


def test_hamming_distance_compares_bare_digest_with_any_algorithm() -> None:
    """A bare digest carries no algorithm, so it compares on bit length alone."""
    assert_that(
        hamming_distance("ahash:8:8000000000000000", "0000000000000000"),
    ).is_equal_to(1)
