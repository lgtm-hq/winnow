"""``@`` cast markers in config file values and ``WINNOW_*`` environment values.

Dynaconf, which winnow used to read its configuration until #391, applied a
leading ``@marker`` to every string value in the config file and in the
environment: ``workers: "@int 4"`` became the integer 4. winnow keeps the
markers that are plain casts or string transforms, with Dynaconf's exact
semantics, and rejects the ones that need Dynaconf's machinery (lazy
formatting, other settings, files, merging) with an error that names them
instead of keeping the text.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from typing import Final

_TRUE_WORDS: Final = frozenset({"t", "true", "enabled", "1", "on", "yes"})
"""Words the ``@bool`` marker reads as true; anything else is false."""


class UnsupportedMarkerError(ValueError):
    """A value uses a Dynaconf marker winnow does not support."""


def _cast_bool(text: str) -> bool:
    """Read a ``@bool`` marker value."""
    return text.strip().lower() in _TRUE_WORDS


def _cast_none(_text: str) -> None:
    """Read a marker that always gives ``None``, whatever follows it."""


def _json_objects(text: str) -> Iterator[object]:
    """Yield every JSON object embedded in ``text``, as Dynaconf did."""
    decoder = json.JSONDecoder()
    position = 0
    while (start := text.find("{", position)) != -1:
        try:
            result, length = decoder.raw_decode(text[start:])
        except ValueError:
            position = start + 1
            continue
        yield result
        position = start + length


_PYTHON_LITERALS: Final = {
    ": True": ": true",
    ":True": ": true",
    ": False": ": false",
    ":False": ": false",
    ": None": ": null",
    ":None": ": null",
}


def _cast_json(text: str) -> object:
    """Read a ``@json`` marker value the way Dynaconf did.

    Standard JSON first; then the first JSON object found in the text after
    replacing Python's ``True``, ``False`` and ``None`` after a colon; then a
    Python literal (``ast.literal_eval``, which evaluates no code).

    Raises:
        ValueError: If the text is none of these.
    """
    with suppress(json.JSONDecodeError):
        return json.loads(text)
    replaced = text
    for old, new in _PYTHON_LITERALS.items():
        replaced = replaced.replace(old, new)
    for found in _json_objects(replaced):
        return found
    with suppress(ValueError, SyntaxError, TypeError):
        return ast.literal_eval(text)
    message = f"cannot parse as JSON: {text}"
    raise ValueError(message)


_CASTS: Final[dict[str, Callable[[str], object]]] = {
    "@str": str,
    "@int": int,
    "@float": float,
    "@bool": _cast_bool,
    "@json": _cast_json,
    "@none": _cast_none,
    "@null": _cast_none,
    "@note": _cast_none,
    "@comment": _cast_none,
    "@upper": str.upper,
    "@lower": str.lower,
    "@title": str.title,
    "@capitalize": str.capitalize,
    "@strip": str.strip,
    "@lstrip": str.lstrip,
    "@rstrip": str.rstrip,
    "@split": str.split,
    "@casefold": str.casefold,
    "@swapcase": str.swapcase,
}
"""Markers winnow applies, each with Dynaconf's semantics."""

UNSUPPORTED_MARKERS: Final = frozenset(
    {
        "@format",
        "@jinja",
        "@get",
        "@read_file",
        "@reset",
        "@del",
        "@merge",
        "@merge_unique",
        "@insert",
        "@empty",
    },
)
"""Dynaconf markers that need Dynaconf itself; a value using one is an error."""

_COMBINED: Final = re.compile(r"^@\w+ (@(?:jinja|format|read_file|get))\b")
"""Dynaconf's combined form, such as ``@int @format {this.x}``."""


def cast_marker(raw: str) -> tuple[bool, object]:
    """Apply a leading ``@`` marker to a string value.

    Args:
        raw: The value as written.

    Returns:
        ``(True, value)`` when ``raw`` starts with a supported marker, else
        ``(False, raw)``.

    Raises:
        UnsupportedMarkerError: If ``raw`` uses a Dynaconf marker winnow does
            not support.
        ValueError: If the marker's argument cannot be cast.
    """
    marker, _, rest = raw.partition(" ")
    if marker in UNSUPPORTED_MARKERS:
        raise UnsupportedMarkerError(_unsupported_message(marker))
    cast = _CASTS.get(marker)
    if cast is None:
        return False, raw
    combined = _COMBINED.match(raw)
    if combined is not None:
        raise UnsupportedMarkerError(_unsupported_message(combined.group(1)))
    try:
        return True, cast(rest)
    except ValueError as exc:
        message = f"cannot cast {rest!r} with {marker}"
        raise ValueError(message) from exc


def _unsupported_message(marker: str) -> str:
    """Say that a marker is not supported."""
    return (
        f"the {marker} marker is not supported; winnow applies only "
        f"{', '.join(sorted(_CASTS))}"
    )


def cast_markers_in(value: object) -> object:
    """Apply markers to every string in a config value, recursively.

    Args:
        value: A value read from a config file.

    Returns:
        The value with every marked string cast. Mappings and lists are
        rebuilt; other values are returned unchanged.
    """
    if isinstance(value, str):
        return cast_marker(value)[1]
    if isinstance(value, Mapping):
        return {key: cast_markers_in(item) for key, item in value.items()}
    if isinstance(value, list):
        return [cast_markers_in(item) for item in value]
    return value
