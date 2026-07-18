"""Tests for perceptual image hashing.

Synthetic Pillow images are generated in-process so these tests never depend on
media discovery or the format processors from other epics.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from assertpy import assert_that
from PIL import Image

from winnow.exceptions import HashError
from winnow.hash import (
    DEFAULT_HASH_SIZE,
    PERCEPTUAL_ALGORITHMS,
    ImageHasher,
    PerceptualHash,
    hamming_distance,
    hash_image,
)
from winnow.models.enums import HashAlgorithm

PERCEPTUAL_CASES: list[HashAlgorithm] = sorted(
    PERCEPTUAL_ALGORITHMS,
    key=lambda algorithm: algorithm.value,
)


def _base_image() -> Image.Image:
    """Return a deterministic vertical-gradient RGB image.

    Returns:
        A 256x256 image whose brightness increases top to bottom.
    """
    return Image.linear_gradient("L").convert("RGB")


def _similar_image() -> Image.Image:
    """Return a near-duplicate of :func:`_base_image`.

    Returns:
        The base gradient after a mild brightness and contrast adjustment, the
        kind of change a re-encoded or lightly edited photo would show.
    """
    return _base_image().point(lambda value: min(255, int(value * 0.95) + 6))


def _different_image() -> Image.Image:
    """Return an image structurally unlike :func:`_base_image`.

    Returns:
        A radial-gradient image whose global structure differs sharply from a
        linear gradient.
    """
    return Image.radial_gradient("L").convert("RGB")


@pytest.mark.parametrize("algorithm", PERCEPTUAL_CASES)
def test_hash_is_deterministic_and_describes_parameters(
    algorithm: HashAlgorithm,
) -> None:
    """Hashing the same image twice yields identical, parameterized results."""
    hasher = ImageHasher(algorithm=algorithm)
    image = _base_image()

    first = hasher.hash(image)
    second = hasher.hash(image)

    assert_that(first).is_equal_to(second)
    assert_that(first.algorithm).is_equal_to(algorithm)
    assert_that(first.hash_size).is_equal_to(DEFAULT_HASH_SIZE)
    assert_that(first.digest).is_not_empty()
    assert_that(first.distance(second)).is_equal_to(0)


@pytest.mark.parametrize("algorithm", PERCEPTUAL_CASES)
def test_similar_images_are_closer_than_different_images(
    algorithm: HashAlgorithm,
) -> None:
    """Near-duplicate images hash closer than structurally different ones."""
    hasher = ImageHasher(algorithm=algorithm)
    base = hasher.hash(_base_image())
    similar = hasher.hash(_similar_image())
    different = hasher.hash(_different_image())

    assert_that(base.distance(similar)).is_less_than(base.distance(different))


@pytest.mark.parametrize("algorithm", PERCEPTUAL_CASES)
def test_serialization_round_trips(algorithm: HashAlgorithm) -> None:
    """A serialized hash deserializes back into an equal hash."""
    original = ImageHasher(algorithm=algorithm).hash(_base_image())

    restored = PerceptualHash.deserialize(original.serialize())

    assert_that(restored).is_equal_to(original)
    assert_that(restored.distance(original)).is_equal_to(0)


def test_serialize_format_is_algorithm_size_digest() -> None:
    """Serialized hashes use an ``algorithm:size:digest`` layout."""
    hashed = ImageHasher(algorithm=HashAlgorithm.PHASH, hash_size=8).hash(
        _base_image(),
    )

    serialized = hashed.serialize()

    assert_that(serialized).starts_with("phash:8:")
    assert_that(serialized).is_equal_to(f"phash:8:{hashed.digest}")


def test_hamming_distance_helper_matches_hash_distance() -> None:
    """The free ``hamming_distance`` helper agrees with hash comparison."""
    hasher = ImageHasher(algorithm=HashAlgorithm.DHASH)
    base = hasher.hash(_base_image())
    different = hasher.hash(_different_image())

    assert_that(hamming_distance(base.digest, different.digest)).is_equal_to(
        base.distance(different),
    )
    assert_that(hamming_distance(base.digest, base.digest)).is_equal_to(0)


def test_is_similar_uses_inclusive_threshold() -> None:
    """``is_similar`` treats hashes within the threshold as matches."""
    hasher = ImageHasher(algorithm=HashAlgorithm.PHASH)
    base = hasher.hash(_base_image())
    similar = hasher.hash(_similar_image())
    different = hasher.hash(_different_image())

    distance = base.distance(different)

    assert_that(base.is_similar(similar, threshold=8)).is_true()
    assert_that(base.is_similar(different, threshold=distance)).is_true()
    assert_that(base.is_similar(different, threshold=distance - 1)).is_false()


def test_configurable_hash_size_changes_digest_length() -> None:
    """A larger hash size produces a proportionally longer digest."""
    small = ImageHasher(algorithm=HashAlgorithm.PHASH, hash_size=8).hash(
        _base_image(),
    )
    large = ImageHasher(algorithm=HashAlgorithm.PHASH, hash_size=16).hash(
        _base_image(),
    )

    assert_that(len(small.digest)).is_equal_to(16)
    assert_that(len(large.digest)).is_equal_to(64)


def test_defaults_use_phash_and_default_size() -> None:
    """The hasher defaults to pHash and the module default hash size."""
    hasher = ImageHasher()

    assert_that(hasher.algorithm).is_equal_to(HashAlgorithm.PHASH)
    assert_that(hasher.hash_size).is_equal_to(DEFAULT_HASH_SIZE)
    assert_that(DEFAULT_HASH_SIZE).is_equal_to(8)


def test_hash_image_helper_matches_hasher() -> None:
    """The ``hash_image`` convenience matches an explicit hasher."""
    image = _base_image()

    helper_result = hash_image(image, algorithm=HashAlgorithm.AHASH, hash_size=8)
    hasher_result = ImageHasher(algorithm=HashAlgorithm.AHASH, hash_size=8).hash(image)

    assert_that(helper_result).is_equal_to(hasher_result)


def test_hash_file_matches_in_memory_hash(tmp_path: Path) -> None:
    """Hashing a file yields the same result as hashing the loaded image."""
    image = _base_image()
    image_path = tmp_path / "gradient.png"
    image.save(image_path)
    hasher = ImageHasher(algorithm=HashAlgorithm.PHASH)

    from_file = hasher.hash_file(image_path)
    from_memory = hasher.hash(image)

    assert_that(from_file).is_equal_to(from_memory)


def test_configuring_non_perceptual_algorithm_raises() -> None:
    """A content-hash algorithm is rejected by the perceptual hasher."""
    with pytest.raises(HashError, match="not a perceptual hash algorithm"):
        ImageHasher(algorithm=HashAlgorithm.MD5)


def test_configuring_tiny_hash_size_raises() -> None:
    """A hash size below two is rejected."""
    with pytest.raises(HashError, match="at least 2"):
        ImageHasher(algorithm=HashAlgorithm.PHASH, hash_size=1)


def test_whash_requires_power_of_two_hash_size() -> None:
    """wHash rejects hash sizes that are not powers of two."""
    with pytest.raises(HashError, match="power-of-two"):
        ImageHasher(algorithm=HashAlgorithm.WHASH, hash_size=6)


def test_hashing_non_image_raises() -> None:
    """Passing a non-image object raises a domain error."""
    hasher = ImageHasher()

    with pytest.raises(HashError, match="must be a PIL.Image.Image"):
        hasher.hash(cast("Image.Image", "not-an-image"))


def test_hash_file_missing_path_raises(tmp_path: Path) -> None:
    """Hashing a missing file raises a domain error with the path recorded."""
    hasher = ImageHasher()
    missing = tmp_path / "missing.png"

    with pytest.raises(HashError, match="failed to read image file"):
        hasher.hash_file(missing)


def test_hash_file_non_image_raises(tmp_path: Path) -> None:
    """Hashing a non-image file raises a domain error."""
    hasher = ImageHasher()
    text_path = tmp_path / "note.txt"
    text_path.write_text("not an image", encoding="utf-8")

    with pytest.raises(HashError, match="failed to read image file"):
        hasher.hash_file(text_path)


@pytest.mark.parametrize(
    "value",
    [
        "phash",
        "phash:8",
        "phash:notanint:aabb",
    ],
)
def test_deserialize_rejects_malformed_values(value: str) -> None:
    """Malformed serialized strings raise a domain error."""
    with pytest.raises(HashError, match="malformed serialized perceptual hash"):
        PerceptualHash.deserialize(value)


def test_deserialize_preserves_digest_with_maxsplit() -> None:
    """Parsing splits on the first two separators only, keeping the digest."""
    restored = PerceptualHash.deserialize("phash:8:aabbccddeeff0011")

    assert_that(restored.digest).is_equal_to("aabbccddeeff0011")


def test_deserialize_rejects_tiny_hash_size() -> None:
    """A serialized hash size below two is rejected like the constructor."""
    with pytest.raises(HashError, match="at least 2"):
        PerceptualHash.deserialize("phash:1:00")


def test_deserialize_rejects_whash_non_power_of_two_size() -> None:
    """A serialized wHash with a non-power-of-two size is rejected."""
    with pytest.raises(HashError, match="power-of-two"):
        PerceptualHash.deserialize("whash:6:aabbccddeeff0011")


def test_deserialize_rejects_unknown_algorithm() -> None:
    """An unrecognized algorithm token raises a domain error."""
    with pytest.raises(HashError, match="unknown perceptual hash algorithm"):
        PerceptualHash.deserialize("mystery:8:aabbccdd")


def test_deserialize_rejects_non_perceptual_algorithm() -> None:
    """A valid but non-perceptual algorithm token is rejected."""
    with pytest.raises(HashError, match="not a perceptual hash algorithm"):
        PerceptualHash.deserialize("md5:8:aabbccdd")


def test_distance_between_different_algorithms_raises() -> None:
    """Comparing hashes from different algorithms raises a domain error."""
    image = _base_image()
    phash = ImageHasher(algorithm=HashAlgorithm.PHASH).hash(image)
    dhash = ImageHasher(algorithm=HashAlgorithm.DHASH).hash(image)

    with pytest.raises(HashError, match="different algorithms or sizes"):
        phash.distance(dhash)


def test_hamming_distance_between_sizes_raises() -> None:
    """Comparing digests of different sizes raises a domain error."""
    image = _base_image()
    small = ImageHasher(algorithm=HashAlgorithm.PHASH, hash_size=8).hash(image)
    large = ImageHasher(algorithm=HashAlgorithm.PHASH, hash_size=16).hash(image)

    with pytest.raises(HashError, match="different sizes"):
        hamming_distance(small.digest, large.digest)


def test_is_similar_rejects_negative_threshold() -> None:
    """A negative similarity threshold raises a domain error."""
    image = _base_image()
    hashed = ImageHasher().hash(image)

    with pytest.raises(HashError, match="must not be negative"):
        hashed.is_similar(hashed, threshold=-1)


def test_hamming_distance_rejects_invalid_digest() -> None:
    """A non-hexadecimal digest raises a domain error."""
    with pytest.raises(HashError, match="invalid perceptual hash digest"):
        hamming_distance("zzzz", "0000")
