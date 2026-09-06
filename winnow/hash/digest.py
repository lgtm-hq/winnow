"""Perceptual-hash string parsing and Hamming distance.

Perceptual hashes travel as strings in one of two canonical forms:

- **Bare** — the hex digest from :attr:`PerceptualHash.digest` (optionally
  prefixed with ``0x``); every character encodes four bits.
- **Self-describing** — ``"{algorithm}:{hash_size}:{hex}"`` as produced by
  :meth:`PerceptualHash.serialize`.

:func:`parse_digest` accepts both and is the single reader for perceptual-hash
strings; :func:`hamming_distance` compares two such strings bit-for-bit.
"""

from __future__ import annotations

from string import hexdigits

from winnow.exceptions import HashError

_HEX_PREFIXES: tuple[str, ...] = ("0x", "0X")
_BITS_PER_HEX_DIGIT = 4
_PARSE_OPERATION = "parse_digest"


def parse_digest(value: str) -> tuple[int, int]:
    """Decode a perceptual-hash string into ``(value, bit_length)``.

    Accepts a bare hex digest (optional ``0x`` prefix) or the
    ``"{algorithm}:{hash_size}:{hex}"`` form produced by
    :meth:`PerceptualHash.serialize`; the serialized form is validated with
    :meth:`PerceptualHash.deserialize` and its digest decoded.

    Args:
        value: Perceptual-hash string in either canonical form.

    Returns:
        Tuple of ``(integer_value, bit_length)``.

    Raises:
        HashError: If the string is empty, contains non-hex digits, or is a
            malformed serialized form (including a digest whose length does
            not match ``hash_size ** 2``).
    """
    hash_value, bit_length, _ = _decode(value)
    return hash_value, bit_length


def hamming_distance(left: str, right: str) -> int:
    """Return the Hamming distance between two perceptual-hash strings.

    When both digests are in the self-describing form their algorithms must
    match, mirroring :meth:`PerceptualHash.distance`. A bare digest carries no
    algorithm and is compared on bit length alone.

    Args:
        left: First digest, in any form accepted by :func:`parse_digest`.
        right: Second digest, in any form accepted by :func:`parse_digest`.

    Returns:
        Number of differing bits between the two hashes.

    Raises:
        HashError: If either digest is invalid, the two hashes decode to
            different bit lengths, or both name different algorithms.
    """
    left_value, left_bits, left_algorithm = _decode(left)
    right_value, right_bits, right_algorithm = _decode(right)
    if (
        left_algorithm is not None
        and right_algorithm is not None
        and left_algorithm != right_algorithm
    ):
        raise HashError(
            "cannot compare perceptual hashes of different algorithms",
            operation="hamming_distance",
            details={
                "left_algorithm": left_algorithm,
                "right_algorithm": right_algorithm,
            },
        )
    if left_bits != right_bits:
        raise HashError(
            "cannot compare perceptual hashes of different sizes",
            operation="hamming_distance",
            details={"left_bits": left_bits, "right_bits": right_bits},
        )
    return (left_value ^ right_value).bit_count()


def _decode(value: str) -> tuple[int, int, str | None]:
    """Decode either canonical form, keeping the algorithm when present.

    Args:
        value: Perceptual-hash string in either canonical form.

    Returns:
        Tuple of ``(integer_value, bit_length, algorithm)``; ``algorithm`` is
        ``None`` for a bare hex digest.

    Raises:
        HashError: If the string is empty or malformed in either form.
    """
    candidate = value.strip()
    if not candidate:
        raise HashError(
            "perceptual hash string is empty",
            operation=_PARSE_OPERATION,
        )
    if ":" in candidate:
        return _parse_serialized(candidate)
    hash_value, bit_length = _parse_hex(candidate)
    return hash_value, bit_length, None


def _parse_serialized(candidate: str) -> tuple[int, int, str]:
    """Decode a ``PerceptualHash.serialize()`` string.

    Args:
        candidate: Whitespace-stripped serialized hash string.

    Returns:
        Tuple of ``(integer_value, bit_length, algorithm)``.

    Raises:
        HashError: If the serialized form is malformed or the digest length
            does not match ``hash_size ** 2`` bits.
    """
    # Local import: image_hasher imports hamming_distance from this module.
    from winnow.hash.image_hasher import PerceptualHash

    parsed = PerceptualHash.deserialize(candidate)
    hash_value, bit_length = _parse_hex(parsed.digest)
    expected_bits = parsed.hash_size**2
    if bit_length != expected_bits:
        raise HashError(
            "perceptual hash digest length does not match hash_size",
            operation=_PARSE_OPERATION,
            details={
                "value": candidate,
                "digest_bits": bit_length,
                "expected_bits": expected_bits,
            },
        )
    return hash_value, bit_length, parsed.algorithm.value


def _parse_hex(candidate: str) -> tuple[int, int]:
    """Decode a bare hex digest, optionally prefixed with ``0x``.

    Args:
        candidate: Whitespace-stripped hex string.

    Returns:
        Tuple of ``(integer_value, bit_length)``.

    Raises:
        HashError: If the string has no digits or contains non-hex characters.
    """
    digits = candidate[2:] if candidate.startswith(_HEX_PREFIXES) else candidate
    if not digits or any(char not in hexdigits for char in digits):
        raise HashError(
            "invalid perceptual hash digest",
            operation=_PARSE_OPERATION,
            details={"value": candidate},
        )
    return int(digits, 16), len(digits) * _BITS_PER_HEX_DIGIT


__all__ = ["hamming_distance", "parse_digest"]
