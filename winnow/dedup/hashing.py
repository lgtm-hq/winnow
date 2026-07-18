"""Perceptual-hash parsing and Hamming-distance comparison.

Perceptual hashes are represented as strings in one of two textual encodings:

- **Hex** — e.g. ``"f00fa5"`` (optionally prefixed with ``0x``); every character
  encodes four bits.
- **Bitstring** — e.g. ``"110010"``; every character encodes a single bit.

Both encodings decode to an integer value plus a fixed bit length. Two hashes are
comparable only when they decode to the same bit length; comparing hashes of
different widths is meaningless and raises :class:`~winnow.exceptions.DuplicateError`.
"""

from __future__ import annotations

from enum import StrEnum, auto

from winnow.exceptions import DuplicateError

_HEX_PREFIXES: tuple[str, ...] = ("0x", "0X")


class HashFormat(StrEnum):
    """Textual encoding of a perceptual-hash string."""

    AUTO = auto()
    HEX = auto()
    BITSTRING = auto()


def parse_hash(
    value: str,
    *,
    hash_format: HashFormat = HashFormat.AUTO,
) -> tuple[int, int]:
    """Decode a perceptual-hash string into an integer value and bit length.

    Args:
        value: Hex or bitstring encoded perceptual hash.
        hash_format: Encoding to assume. ``AUTO`` treats a string containing only
            ``0`` and ``1`` as a bitstring and any other valid hex string as hex.

    Returns:
        Tuple of ``(integer_value, bit_length)``.

    Raises:
        DuplicateError: If the string is empty or not valid for the resolved
            encoding.
    """
    candidate = value.strip()
    if not candidate:
        raise DuplicateError(
            "perceptual hash string is empty",
            operation="parse_hash",
        )

    resolved_format = _resolve_format(candidate=candidate, hash_format=hash_format)
    if resolved_format is HashFormat.BITSTRING:
        return _parse_bitstring(candidate)
    return _parse_hex(candidate)


def hamming_distance(
    hash_a: str,
    hash_b: str,
    *,
    hash_format: HashFormat = HashFormat.AUTO,
) -> int:
    """Compute the Hamming distance between two perceptual-hash strings.

    Args:
        hash_a: First perceptual hash.
        hash_b: Second perceptual hash.
        hash_format: Encoding to assume for both hashes.

    Returns:
        Number of differing bits between the two hashes.

    Raises:
        DuplicateError: If either hash is invalid or the two hashes decode to
            different bit lengths.
    """
    value_a, bits_a = parse_hash(hash_a, hash_format=hash_format)
    value_b, bits_b = parse_hash(hash_b, hash_format=hash_format)
    if bits_a != bits_b:
        raise DuplicateError(
            "cannot compare perceptual hashes of different bit lengths",
            operation="hamming_distance",
            details={"bits_a": bits_a, "bits_b": bits_b},
        )
    return (value_a ^ value_b).bit_count()


def _resolve_format(*, candidate: str, hash_format: HashFormat) -> HashFormat:
    """Resolve an explicit or automatic hash format for a candidate string.

    Args:
        candidate: Whitespace-stripped hash string.
        hash_format: Requested format, possibly ``AUTO``.

    Returns:
        Either ``HashFormat.HEX`` or ``HashFormat.BITSTRING``.
    """
    if hash_format is not HashFormat.AUTO:
        return hash_format
    if _has_hex_prefix(candidate):
        return HashFormat.HEX
    if all(char in "01" for char in candidate):
        return HashFormat.BITSTRING
    return HashFormat.HEX


def _has_hex_prefix(candidate: str) -> bool:
    """Return whether the candidate carries an explicit hex ``0x`` prefix.

    Args:
        candidate: Whitespace-stripped hash string.

    Returns:
        ``True`` when the string starts with ``0x`` or ``0X``.
    """
    return candidate.startswith(_HEX_PREFIXES)


def _parse_bitstring(candidate: str) -> tuple[int, int]:
    """Decode a bitstring hash into an integer value and bit length.

    Args:
        candidate: Bitstring containing only ``0`` and ``1``.

    Returns:
        Tuple of ``(integer_value, bit_length)``.

    Raises:
        DuplicateError: If the string contains non-binary characters.
    """
    if not all(char in "01" for char in candidate):
        raise DuplicateError(
            "invalid bitstring perceptual hash",
            operation="parse_hash",
            details={"value": candidate},
        )
    return int(candidate, 2), len(candidate)


def _parse_hex(candidate: str) -> tuple[int, int]:
    """Decode a hex hash into an integer value and bit length.

    Args:
        candidate: Hex string, optionally prefixed with ``0x``.

    Returns:
        Tuple of ``(integer_value, bit_length)``.

    Raises:
        DuplicateError: If the string is empty or contains non-hex characters.
    """
    digits = candidate[2:] if _has_hex_prefix(candidate) else candidate
    if not digits:
        raise DuplicateError(
            "hex perceptual hash has no digits",
            operation="parse_hash",
            details={"value": candidate},
        )
    try:
        value = int(digits, 16)
    except ValueError as error:
        raise DuplicateError(
            "invalid hex perceptual hash",
            operation="parse_hash",
            details={"value": candidate},
        ) from error
    return value, len(digits) * 4


__all__ = [
    "HashFormat",
    "hamming_distance",
    "parse_hash",
]
