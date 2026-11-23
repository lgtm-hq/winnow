"""``config set`` over generated YAML layouts edits one key or refuses (#429).

Each generated file uses one mapping indent (2 or 4), one sequence style
(dash at offset 0 or 2), one spelling of null and one letter case of
booleans. It holds the sections an edit touches (``workers``,
``source_dirs``, ``cache``, ``routing``, ``paths``) among filler entries with
nested mappings, sequences, inline comments, block scalars, flow collections
and empty values, with comments and blank lines between sections, after
block collections and at the end of the file.

Every seed makes one edit of one shape. The edit must either raise
:class:`ConfigError` and leave the file byte for byte as it was, or give a
file that parses, holds the new value, keeps every comment and blank line,
and differs from the original in one run of lines that removes no blank or
comment line and keeps every inline comment it removes.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import cast

from assertpy import assert_that
from ruamel.yaml import YAML

from winnow.config import set_config_value
from winnow.exceptions import ConfigError

_SEEDS = range(270)
"""Seeds the unit run covers; a larger run uses the same generator."""
_MIN_ACCEPTED = 0.6
"""The share of each shape's edits that must be accepted."""
_INLINE_COMMENT = re.compile(r"\s+#.*$")


class Shape(StrEnum):
    """The kind of edit a seed makes."""

    TOP_SCALAR = auto()
    REPLACE_LIST = auto()
    REPLACE_MAPPING = auto()
    NESTED_SCALAR = auto()
    NEW_TOP_KEY = auto()
    NEW_NESTED_KEY = auto()
    SET_NULL = auto()
    NULL_TO_LIST = auto()
    NULL_TO_MAPPING = auto()


_SHAPES = list(Shape)


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
    """How one generated file lays out its collections and scalars."""

    mapping: int
    offset: int
    null: str
    true: str

    @property
    def item(self) -> int:
        """Return the column of a sequence item's content under its key."""
        return self.offset + 2

    def empty(self) -> str:
        """Return a null value written after ``key:``, its space included."""
        return f" {self.null}" if self.null else ""


@dataclass(frozen=True, slots=True)
class Case:
    """One generated file and the edit made on it."""

    text: str
    shape: Shape
    key: str
    value: object


def _comment(rng: _Picker) -> str:
    """Return an inline comment, or nothing."""
    return str(rng.choice(["", "", "", "  # note", " # x: y"]))


def _scalar(rng: _Picker) -> str:
    """Return a plain or quoted scalar."""
    return str(
        rng.choice(["a", "/photos", "'q: x'", '"- d"', "1", "x # y", '"[~]"']),
    )


def _after_block(rng: _Picker) -> list[str]:
    """Return what follows a block collection: nothing, a comment or a blank."""
    return {0: [], 1: [], 2: ["# after"], 3: [""]}[rng.randrange(4)]


def _sequence(rng: _Picker, style: _Style, depth: int, key: str) -> list[str]:
    """Return a block sequence under ``key`` at ``depth``."""
    pad = " " * depth
    dash = " " * (depth + style.offset)
    lines = [f"{pad}{key}:{_comment(rng)}"]
    for _ in range(rng.randint(1, 3)):
        kind = rng.randrange(20)
        if kind < 16:
            lines.append(f"{dash}- {_scalar(rng)}{_comment(rng)}")
        elif kind == 16:
            lines.append(f"{dash}-{_comment(rng)}")
            lines.append(f"{' ' * (depth + style.item)}{_scalar(rng)}")
        elif kind == 17:
            lines.append(f"{dash}- [a, b]")
        else:
            inner = " " * (depth + style.item)
            lines.append(f"{dash}- name: {_scalar(rng)}")
            lines.append(f"{inner}size: 3")
    return lines


def _block_scalar(rng: _Picker, style: _Style, depth: int, key: str) -> list[str]:
    """Return a block scalar whose text looks like YAML structure."""
    pad = " " * depth
    body = " " * (depth + style.mapping)
    indicator = str(rng.choice(["|", ">", "|-", ">-"]))
    text = rng.sample(["- # just text", "key:", "- deep", "x: y", "#c"], 3)
    return [f"{pad}{key}: {indicator}", *(f"{body}{line}" for line in text)]


def _entry(rng: _Picker, style: _Style, depth: int, key: str) -> list[str]:
    """Return one filler mapping entry of a random kind at ``depth``."""
    pad = " " * depth
    kind = rng.randrange(9 if depth < 2 * style.mapping else 7)
    if kind in {0, 1}:
        return [f"{pad}{key}: {_scalar(rng)}{_comment(rng)}"]
    if kind == 2:
        return [f"{pad}{key}:{style.empty()}{_comment(rng)}"]
    if kind == 3:
        flow = rng.choice(["[a, b]", "{a: 1}", "[]", "{}", f"[{style.true}]"])
        return [f"{pad}{key}: {flow}"]
    if kind == 4:
        return _block_scalar(rng, style, depth, key)
    if kind in {5, 6}:
        return _sequence(rng, style, depth, key) + _after_block(rng)
    lines = [f"{pad}{key}:{_comment(rng)}"]
    for index in range(rng.randint(1, 3)):
        lines += _entry(rng, style, depth + style.mapping, f"{key}_{index}")
    return lines + _after_block(rng)


def _mapping(
    rng: _Picker,
    style: _Style,
    key: str,
    entries: list[str],
) -> list[str]:
    """Return a top-level block mapping of ``entries``, perhaps with a comment."""
    pad = " " * style.mapping
    lines = [f"{key}:{_comment(rng)}"]
    for entry in entries:
        if rng.randrange(12) == 0:
            lines.append(f"{pad}# about {entry.partition(':')[0]}")
        lines.append(f"{pad}{entry}{_comment(rng)}")
    return lines + _after_block(rng)


def _sections(rng: _Picker, style: _Style, shape: Shape) -> list[list[str]]:
    """Return the sections the edits touch, laid out for ``shape``."""
    dash = " " * style.offset
    if shape is Shape.NULL_TO_LIST:
        sources = [f"source_dirs:{style.empty()}{_comment(rng)}"]
    elif rng.randrange(5) == 0 and shape is not Shape.REPLACE_LIST:
        sources = [f"source_dirs: [/photos, /videos]{_comment(rng)}"]
    else:
        items = ["/photos", "/videos", "'/a b'"][: rng.randint(1, 3)]
        sources = ["source_dirs:" + _comment(rng)]
        sources += [f"{dash}- {item}{_comment(rng)}" for item in items]
        sources += _after_block(rng)
    if shape is Shape.NULL_TO_MAPPING:
        cache = [f"cache:{style.empty()}{_comment(rng)}"]
    else:
        cache_entries = [f"enabled: {style.true}"]
        if rng.randrange(2):
            cache_entries.append("directory: /data/cache")
        cache = _mapping(rng, style, "cache", cache_entries)
    routing = ["review: Review"]
    if shape is not Shape.NEW_NESTED_KEY and rng.randrange(2):
        routing.append("graphics: Art")
    return [
        [f"workers: 2{_comment(rng)}"],
        sources,
        cache,
        _mapping(rng, style, "routing", routing),
        _mapping(rng, style, "paths", ["output_dir: /out", "quarantine_dir: /q"]),
    ]


_EDITS: dict[Shape, tuple[str, object]] = {
    Shape.TOP_SCALAR: ("workers", 3),
    Shape.REPLACE_LIST: ("source_dirs", ["/x", "/y"]),
    Shape.REPLACE_MAPPING: ("cache", {"enabled": False, "directory": "/d"}),
    Shape.NESTED_SCALAR: ("cache.enabled", False),
    Shape.NEW_TOP_KEY: ("dry_run", True),
    Shape.NEW_NESTED_KEY: ("routing.graphics", "Drawings"),
    Shape.SET_NULL: ("paths.output_dir", None),
    Shape.NULL_TO_LIST: ("source_dirs", ["/a"]),
    Shape.NULL_TO_MAPPING: ("cache.enabled", True),
}
"""The key and value each shape sets."""
_APPENDED = ["/photos", "/videos", "/x"]
"""A list that keeps the generated items it can and adds one."""


def generate(seed: int) -> Case:
    """Return a generated file and the edit its seed makes."""
    shape = _SHAPES[seed % len(_SHAPES)]
    rng = _Picker(seed)
    style = _Style(
        mapping=int(rng.choice([2, 4])),
        offset=int(rng.choice([0, 2])),
        null=str(rng.choice(["", "null", "~"])),
        true=str(rng.choice(["true", "true", "True"])),
    )
    entries = [_entry(rng, style, 0, f"k{index}") for index in range(rng.randint(0, 3))]
    for section in _sections(rng, style, shape):
        entries.insert(rng.randint(0, len(entries)), section)
    lines: list[str] = []
    if rng.randrange(10) < 3:
        lines += ["# header", ""]
    for index, entry in enumerate(entries):
        if index and not (lines[-1] == "" or lines[-1].startswith("#")):
            lines += {0: [], 1: [], 2: [""], 3: ["# section"], 4: ["", "# section"]}[
                rng.randrange(5)
            ]
        lines += entry
    footer = {0: [], 1: [], 2: [""], 3: ["# end"]}[rng.randrange(4)]
    if footer and lines[-1] == "":
        footer = []
    key, value = _EDITS[shape]
    if shape is Shape.REPLACE_LIST and rng.randrange(2):
        value = _APPENDED
    return Case(
        text="\n".join(lines + footer) + "\n",
        shape=shape,
        key=key,
        value=value,
    )


def _read_key(text: str, key: str) -> object:
    """Return the value at a dotted key of YAML content."""
    current: object = YAML(typ="safe", pure=True).load(text)
    for part in key.split("."):
        assert_that(current).is_instance_of(dict)
        current = cast("dict[str, object]", current)[part]
    return current


def _layout_lines(text: str) -> Counter[str]:
    """Count the blank and full-line comment lines of content."""
    return Counter(
        line.strip()
        for line in text.splitlines()
        if not line.strip() or line.lstrip().startswith("#")
    )


def _assert_one_run(before: str, after: str) -> None:
    """Assert the content changed in one run that removed only key lines."""
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    opcodes = [
        opcode
        for opcode in difflib.SequenceMatcher(
            None,
            old,
            new,
            autojunk=False,
        ).get_opcodes()
        if opcode[0] != "equal"
    ]
    assert_that(opcodes).is_not_empty()
    for previous, current in zip(opcodes, opcodes[1:], strict=False):
        assert_that((current[1], current[3])).is_equal_to(
            (previous[2], previous[4]),
        )
    removed = [line for _, i1, i2, _, _ in opcodes for line in old[i1:i2]]
    added = "".join(line for _, _, _, j1, j2 in opcodes for line in new[j1:j2])
    for line in removed:
        assert_that(line.strip()).is_not_empty()
        assert_that(line.lstrip().startswith("#")).is_false()
        comment = _INLINE_COMMENT.search(line.rstrip("\n"))
        if comment is not None:
            assert_that(added).contains(comment.group().strip())


def check(case: Case, directory: Path) -> str | None:
    """Make a case's edit on its file and check the result.

    Returns:
        ``None`` when the edit was made, or the refusal's message.
    """
    YAML(typ="safe", pure=True).load(case.text)
    config_path = directory / "c.yaml"
    config_path.write_text(case.text, encoding="utf-8")
    try:
        set_config_value(case.key, case.value, config_path=config_path)
    except ConfigError as error:
        assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(case.text)
        return error.message
    after = config_path.read_text(encoding="utf-8")
    assert_that(_read_key(after, case.key)).is_equal_to(case.value)
    assert_that(_layout_lines(after)).is_equal_to(_layout_lines(case.text))
    _assert_one_run(case.text, after)
    return None


def run(seeds: range, directory: Path) -> dict[Shape, Counter[str]]:
    """Check every seed and count the outcomes per shape.

    Returns:
        For each shape, ``"accepted"`` and each refusal message counted.
    """
    outcomes: dict[Shape, Counter[str]] = {shape: Counter() for shape in Shape}
    for seed in seeds:
        case = generate(seed)
        seed_directory = directory / str(seed)
        seed_directory.mkdir()
        try:
            refusal = check(case, seed_directory)
        except AssertionError as error:
            message = f"seed {seed} ({case.shape}): {case.text!r}\n{error}"
            raise AssertionError(message) from error
        outcomes[case.shape][refusal or "accepted"] += 1
    return outcomes


def test_generated_layouts_edit_one_key_or_refuse(tmp_path: Path) -> None:
    """Every shape edits its key in place, or refuses and leaves the file."""
    outcomes = run(_SEEDS, tmp_path)

    for shape, counts in outcomes.items():
        total = sum(counts.values())
        assert_that(counts["accepted"]).described_as(
            f"{shape}: {dict(counts)}",
        ).is_greater_than_or_equal_to(int(total * _MIN_ACCEPTED))


def test_every_shape_is_generated() -> None:
    """The unit run makes every edit shape."""
    shapes = {generate(seed).shape for seed in _SEEDS}

    assert_that(shapes).is_equal_to(set(Shape))
