"""Tests for the ``PerceptualHasher`` protocol and hasher cache identities."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from winnow.hash import DEFAULT_HASH_SIZE, ImageHasher, PerceptualHasher
from winnow.models.enums import HashAlgorithm


def test_image_hasher_satisfies_protocol() -> None:
    """``ImageHasher`` is a ``PerceptualHasher`` both statically and at runtime."""
    hasher: PerceptualHasher = ImageHasher()
    assert_that(isinstance(hasher, PerceptualHasher)).is_true()


def test_non_hasher_does_not_satisfy_protocol() -> None:
    """Objects lacking the protocol members are rejected by the runtime check."""
    assert_that(isinstance(object(), PerceptualHasher)).is_false()


def test_default_image_hasher_cache_algorithm() -> None:
    """The default hasher identity encodes both algorithm and hash size."""
    assert_that(ImageHasher().cache_algorithm).is_equal_to("phash:8")


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            ImageHasher(),
            ImageHasher(hash_size=16),
            id="differs_by_hash_size",
        ),
        pytest.param(
            ImageHasher(algorithm=HashAlgorithm.PHASH),
            ImageHasher(algorithm=HashAlgorithm.DHASH),
            id="differs_by_algorithm",
        ),
    ],
)
def test_cache_algorithm_is_distinct_per_parameters(
    first: ImageHasher,
    second: ImageHasher,
) -> None:
    """Hashers whose digests are not comparable have distinct identities."""
    assert_that(first.cache_algorithm).is_not_equal_to(second.cache_algorithm)


def test_cache_algorithm_uses_configured_values() -> None:
    """The identity string is built from the hasher's own fields."""
    hasher = ImageHasher(algorithm=HashAlgorithm.AHASH, hash_size=DEFAULT_HASH_SIZE)
    assert_that(hasher.cache_algorithm).is_equal_to(
        f"{HashAlgorithm.AHASH.value}:{DEFAULT_HASH_SIZE}",
    )
