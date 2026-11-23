"""How deep a config value nests, and whether it contains itself (#451).

winnow walks config values with recursive functions. Each such walk only
ever receives a value this module has measured: a parsed file, a value read
from it after cast markers are applied and flat keys are split, or a value
given to ``config set``. :func:`children` is the one rule for what counts as
a collection; every recursive walker in :mod:`winnow.config` descends only
into mappings, lists, tuples or sets, which it counts, so a measured value
bounds every walk. Text read by ``json`` is measured by :func:`text_depth`
before it is parsed, as ``json`` recurses once per level.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from typing import Final

import tomli

MAX_DEPTH: Final = 100
"""How many collections deep a config file, or a value set in one, may nest."""
_OPENERS: Final = "[{"
_CLOSERS: Final = "]}"


class NestingError(ValueError):
    """Text a parser refuses because it nests too deeply."""


def load_toml(text: str) -> dict[str, object]:
    """Parse TOML text, turning tomli's refusal of deep nesting into an error.

    tomli limits how deep inline arrays and tables nest and how many parts
    a key has, and its documented way to refuse past those limits is
    ``RecursionError``, raised by tomli itself before Python's stack runs
    out. This is the only ``RecursionError`` winnow handles.

    Raises:
        NestingError: If tomli refuses the text as nested too deeply.
        tomli.TOMLDecodeError: If the text is not valid TOML.
    """
    try:
        return tomli.loads(text)
    except RecursionError as exc:
        message = f"the value nests more than tomli's limit ({exc})"
        raise NestingError(message) from exc


def children(value: object) -> list[object] | None:
    """Return a collection's keys and items, or ``None`` for a scalar.

    A mapping, and a sequence or set that is not text or bytes, is a
    collection.
    """
    if isinstance(value, Mapping):
        return [*value.keys(), *value.values()]
    if isinstance(value, Sequence | Set) and not isinstance(
        value,
        str | bytes | bytearray,
    ):
        return list(value)
    return None


def nesting_depth(value: object) -> int | None:
    """Return how many collections deep a value nests, without recursing.

    A scalar is 0 deep and a collection one more than its deepest item. A
    value reached twice through an alias is measured once.

    Returns:
        The depth, or ``None`` when the value contains itself.
    """
    depths: dict[int, int] = {}
    open_ids: set[int] = set()
    pending: list[tuple[object, bool]] = [(value, False)]
    while pending:
        item, finished = pending.pop()
        items = children(item)
        if items is None:
            continue
        key = id(item)
        if finished:
            open_ids.discard(key)
            depths[key] = 1 + max(
                (depths.get(id(child), 0) for child in items),
                default=0,
            )
        elif key in open_ids:
            return None
        elif key not in depths:
            open_ids.add(key)
            pending.append((item, True))
            pending.extend((child, False) for child in items)
    return depths.get(id(value), 0)


def text_depth(text: str, start: int = 0) -> int:
    """Return how deep JSON text nests its brackets, from ``start`` on.

    Brackets inside double-quoted strings do not count. The count stops
    once the brackets opened from ``start`` are all closed, where a JSON
    value starting there ends.

    Args:
        text: The text.
        start: Where the JSON value starts.

    Returns:
        The deepest bracket nesting found; ``json`` recurses no deeper.
    """
    depth = deepest = 0
    in_string = escaped = False
    for char in text[start:]:
        if escaped:
            escaped = False
        elif in_string:
            escaped = char == "\\"
            in_string = char != '"'
        elif char == '"':
            in_string = True
        elif char in _OPENERS:
            depth += 1
            deepest = max(deepest, depth)
        elif char in _CLOSERS:
            depth -= 1
            if depth <= 0:
                break
    return deepest
