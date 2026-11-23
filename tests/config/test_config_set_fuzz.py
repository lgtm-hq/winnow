"""``config set`` over generated YAML layouts changes one line or refuses (#429).

Each generated file uses one mapping indent (2 or 4) and one sequence style
(dash at offset 0 or 2), with nested mappings, sequences, bare dashes, inline
comments, block scalars, flow collections and empty values around a
``workers: 2`` line. Setting ``workers`` to 3 must change that line only, or
raise :class:`ConfigError` and leave the file as it was.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from assertpy import assert_that
from ruamel.yaml import YAML

from winnow.config import set_config_value
from winnow.exceptions import ConfigError

_SEEDS = range(120)
"""Seeds the unit run covers; a larger run uses the same generator."""
_WORKERS_LINE = "workers: 2"


class _Picker:
    """Deterministic choices from a seed, the same on every platform."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._count = 0

    def _next(self) -> int:
        """Return the next 64-bit number drawn from the seed."""
        self._count += 1
        digest = hashlib.sha256(f"{self._seed}:{self._count}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    def randrange(self, stop: int) -> int:
        """Return a number from 0 up to, not including, ``stop``."""
        return self._next() % stop

    def randint(self, low: int, high: int) -> int:
        """Return a number from ``low`` to ``high``, both included."""
        return low + self.randrange(high - low + 1)

    def choice(self, items: list[str] | list[int]) -> str | int:
        """Return one of ``items``."""
        return items[self.randrange(len(items))]

    def sample(self, items: list[str], count: int) -> list[str]:
        """Return ``count`` different items in a drawn order."""
        pool = list(items)
        return [pool.pop(self.randrange(len(pool))) for _ in range(count)]


@dataclass(frozen=True, slots=True)
class _Style:
    """How one generated file indents its collections."""

    mapping: int
    offset: int

    @property
    def item(self) -> int:
        """Return the column of a sequence item's content under its key."""
        return self.offset + 2


def _comment(rng: _Picker) -> str:
    """Return an inline comment, or nothing."""
    return str(rng.choice(["", "", "  # note", " # x: y"]))


def _scalar(rng: _Picker) -> str:
    """Return a plain or quoted scalar."""
    return str(
        rng.choice(["a", "/photos", "'q: x'", '"- d"', "1", "true", "x # y"]),
    )


def _sequence(rng: _Picker, style: _Style, depth: int, key: str) -> list[str]:
    """Return a block sequence under ``key`` at ``depth``."""
    pad = " " * depth
    dash = " " * (depth + style.offset)
    lines = [f"{pad}{key}:{_comment(rng)}"]
    for _ in range(rng.randint(1, 3)):
        kind = rng.randrange(4)
        if kind == 0:
            lines.append(f"{dash}- {_scalar(rng)}")
        elif kind == 1:
            lines.append(f"{dash}-{_comment(rng)}")
            lines.append(f"{' ' * (depth + style.item)}{_scalar(rng)}")
        elif kind == 2:
            lines.append(f"{dash}- [a, b]")
        else:
            inner = " " * (depth + style.item)
            lines.append(f"{dash}- name: {_scalar(rng)}")
            lines.append(f"{inner}size: 3")
    return lines


def _block_scalar(rng: _Picker, depth: int, key: str) -> list[str]:
    """Return a block scalar whose text looks like YAML structure."""
    pad = " " * depth
    body = " " * (depth + 2)
    indicator = str(rng.choice(["|", ">", "|-", ">+"]))
    text = rng.sample(["- # just text", "key:", "- deep", "x: y", "#c"], 3)
    return [f"{pad}{key}: {indicator}", *(f"{body}{line}" for line in text)]


def _entry(rng: _Picker, style: _Style, depth: int, key: str) -> list[str]:
    """Return one mapping entry of a random kind at ``depth``."""
    pad = " " * depth
    kind = rng.randrange(8 if depth < 2 * style.mapping else 6)
    if kind == 0:
        return [f"{pad}{key}: {_scalar(rng)}{_comment(rng)}"]
    if kind == 1:
        return [f"{pad}{key}:{_comment(rng)}"]
    if kind == 2:
        flow = rng.choice(["[a, b]", "{a: 1}", "[]", "{}"])
        return [f"{pad}{key}: {flow}"]
    if kind == 3:
        return _block_scalar(rng, depth, key)
    if kind in {4, 5}:
        return _sequence(rng, style, depth, key)
    lines = [f"{pad}{key}:{_comment(rng)}"]
    for index in range(rng.randint(1, 3)):
        lines += _entry(rng, style, depth + style.mapping, f"{key}_{index}")
    return lines


def generate(seed: int) -> str:
    """Return a YAML config file with a ``workers: 2`` line somewhere in it."""
    rng = _Picker(seed)
    style = _Style(
        mapping=int(rng.choice([2, 4])),
        offset=int(rng.choice([0, 2])),
    )
    entries = [_entry(rng, style, 0, f"k{index}") for index in range(rng.randint(1, 5))]
    entries.insert(rng.randint(0, len(entries)), [_WORKERS_LINE + _comment(rng)])
    if rng.randrange(10) < 3:
        entries.insert(0, ["# header", ""])
    return "\n".join(line for entry in entries for line in entry) + "\n"


def check(seed: int, directory: Path) -> str:
    """Run ``config set workers 3`` on a generated file and check the result.

    Returns:
        ``"changed"`` when one line changed, ``"refused"`` on a ConfigError.
    """
    original = generate(seed)
    YAML(typ="safe", pure=True).load(original)
    config_path = directory / f"c{seed}.yaml"
    config_path.write_text(original)
    try:
        set_config_value("workers", 3, config_path=config_path)
    except ConfigError:
        assert_that(config_path.read_text()).is_equal_to(original)
        return "refused"
    after = config_path.read_text()
    YAML(typ="safe", pure=True).load(after)
    before_lines = original.splitlines()
    after_lines = after.splitlines()
    assert_that(after_lines).is_length(len(before_lines))
    changed = [
        (old, new)
        for old, new in zip(before_lines, after_lines, strict=True)
        if old != new
    ]
    assert_that(changed).is_length(1)
    assert_that(changed[0][0]).starts_with(_WORKERS_LINE)
    assert_that(changed[0][1]).starts_with("workers: 3")
    return "changed"


@pytest.mark.parametrize("seed", _SEEDS)
def test_generated_layout_changes_one_line_or_refuses(
    tmp_path: Path,
    seed: int,
) -> None:
    """Setting ``workers`` changes its line only, or refuses the file."""
    assert_that(check(seed, tmp_path)).is_in("changed", "refused")
