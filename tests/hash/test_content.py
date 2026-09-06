"""Tests for exact content hashing (``winnow.hash.content``)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from assertpy import assert_that

from winnow.exceptions import HashError
from winnow.hash import (
    CONTENT_ALGORITHMS,
    PERCEPTUAL_ALGORITHMS,
    ContentHash,
    ContentHasher,
    ContentHasherProtocol,
)
from winnow.models.config import WinnowConfig
from winnow.models.enums import HashAlgorithm

if TYPE_CHECKING:
    from pathlib import Path

_THREE_MIB = 3 * 1024 * 1024


@pytest.mark.parametrize(
    ("algorithm", "expected"),
    [
        pytest.param(
            HashAlgorithm.SHA256,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            id="sha256",
        ),
        pytest.param(
            HashAlgorithm.MD5,
            "900150983cd24fb0d6963f7d28e17f72",
            id="md5",
        ),
    ],
)
def test_hash_bytes_known_vectors(algorithm: HashAlgorithm, expected: str) -> None:
    """``hash_bytes`` reproduces the published digests of ``b"abc"``."""
    result = ContentHasher(algorithm=algorithm).hash_bytes(b"abc")
    assert_that(result.algorithm).is_same_as(algorithm)
    assert_that(result.digest).is_equal_to(expected)


def test_hash_file_streams_large_file_in_chunks(tmp_path: Path) -> None:
    """A 3 MiB file hashed in 1 KiB blocks matches a one-shot ``hashlib`` digest."""
    data = bytes(range(256)) * (_THREE_MIB // 256)
    target = tmp_path / "large.bin"
    target.write_bytes(data)

    result = ContentHasher(chunk_size=1024).hash_file(target)

    assert_that(result.digest).is_equal_to(hashlib.sha256(data).hexdigest())


def test_hash_file_identical_content_equal_and_one_byte_change_differs(
    tmp_path: Path,
) -> None:
    """Byte-identical files hash equal; flipping one byte changes the digest."""
    data = b"winnow" * 1000
    first = tmp_path / "a.jpg"
    second = tmp_path / "renamed.mov"
    third = tmp_path / "edited.bin"
    first.write_bytes(data)
    second.write_bytes(data)
    third.write_bytes(data[:-1] + b"X")
    hasher = ContentHasher()

    assert_that(hasher.hash_file(first)).is_equal_to(hasher.hash_file(second))
    assert_that(hasher.hash_file(third).digest).is_not_equal_to(
        hasher.hash_file(first).digest,
    )


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_hash_file_unreadable_path_raises_hash_error(
    tmp_path: Path,
    kind: str,
) -> None:
    """Missing paths and directories surface as chained ``HashError``."""
    target = tmp_path / "missing.bin" if kind == "missing" else tmp_path

    with pytest.raises(HashError) as excinfo:
        ContentHasher().hash_file(target)

    assert_that(excinfo.value.context.operation).is_equal_to("hash_content")
    assert_that(excinfo.value.context.file_path).is_equal_to(target)
    assert_that(excinfo.value.__cause__).is_not_none()


def test_perceptual_algorithm_rejected() -> None:
    """A perceptual algorithm cannot configure a content hasher."""
    with pytest.raises(HashError) as excinfo:
        ContentHasher(algorithm=HashAlgorithm.PHASH)

    assert_that(excinfo.value.context.operation).is_equal_to(
        "configure_content_hasher",
    )
    assert_that(excinfo.value.context.details).contains_entry(
        {"algorithm": "phash"},
        {"supported": ["md5", "sha256"]},
    )


def test_chunk_size_below_one_rejected() -> None:
    """``chunk_size=0`` is rejected at construction time."""
    with pytest.raises(HashError) as excinfo:
        ContentHasher(chunk_size=0)

    assert_that(excinfo.value.context.operation).is_equal_to(
        "configure_content_hasher",
    )


def test_from_config_uses_configured_algorithm() -> None:
    """``from_config`` reads ``hash_algorithm`` and defaults to SHA-256."""
    assert_that(ContentHasher.from_config(WinnowConfig()).algorithm).is_same_as(
        HashAlgorithm.SHA256,
    )
    assert_that(ContentHasher.from_config(None).algorithm).is_same_as(
        HashAlgorithm.SHA256,
    )
    assert_that(
        ContentHasher.from_config(
            WinnowConfig(hash_algorithm=HashAlgorithm.MD5),
        ).algorithm,
    ).is_same_as(HashAlgorithm.MD5)


def test_from_config_perceptual_algorithm_rejected() -> None:
    """A perceptual ``hash_algorithm`` in config raises a named ``HashError``."""
    with pytest.raises(HashError, match="not a content hash algorithm") as excinfo:
        ContentHasher.from_config(WinnowConfig(hash_algorithm=HashAlgorithm.AHASH))

    assert_that(str(excinfo.value)).contains("ahash")


def test_cache_algorithm_encodes_algorithm() -> None:
    """The cache identity is ``content-<algorithm>``."""
    assert_that(ContentHasher().cache_algorithm).is_equal_to("content-sha256")
    assert_that(
        ContentHasher(algorithm=HashAlgorithm.MD5).cache_algorithm,
    ).is_equal_to("content-md5")


@pytest.mark.parametrize("algorithm", sorted(CONTENT_ALGORITHMS))
def test_serialize_deserialize_round_trip(algorithm: HashAlgorithm) -> None:
    """``deserialize(serialize())`` returns an equal ``ContentHash``."""
    original = ContentHasher(algorithm=algorithm).hash_bytes(b"round trip")

    serialized = original.serialize()

    assert_that(serialized).starts_with(f"{algorithm.value}:")
    assert_that(ContentHash.deserialize(serialized)).is_equal_to(original)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("phash:8:00", id="perceptual_algorithm"),
        pytest.param("sha256:zz", id="non_hex_digest"),
        pytest.param("sha256:abc", id="wrong_length"),
        pytest.param("nocolon", id="no_separator"),
        pytest.param("sha256:" + "A" * 64, id="uppercase_hex"),
        pytest.param("bogus:" + "0" * 64, id="unknown_algorithm"),
    ],
)
def test_deserialize_rejects_malformed(value: str) -> None:
    """Malformed or non-content serialized values raise ``HashError``."""
    with pytest.raises(HashError) as excinfo:
        ContentHash.deserialize(value)

    assert_that(excinfo.value.context.operation).is_equal_to(
        "deserialize_content_hash",
    )


def test_content_hasher_satisfies_protocol() -> None:
    """``ContentHasher`` satisfies ``ContentHasherProtocol`` at runtime."""
    hasher: ContentHasherProtocol = ContentHasher()
    assert_that(isinstance(hasher, ContentHasherProtocol)).is_true()
    assert_that(isinstance(object(), ContentHasherProtocol)).is_false()


def test_every_algorithm_is_perceptual_or_content() -> None:
    """Each ``HashAlgorithm`` member belongs to exactly one family."""
    assert_that(PERCEPTUAL_ALGORITHMS | CONTENT_ALGORITHMS).is_equal_to(
        frozenset(HashAlgorithm),
    )
    assert_that(PERCEPTUAL_ALGORITHMS & CONTENT_ALGORITHMS).is_empty()
