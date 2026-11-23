"""``config set`` over generated YAML files replaces one value's text (#429).

Each seed generates a YAML file and makes one edit of one shape. The files
mix every layout the old whole-file dump could not keep: comments inline,
on their own lines, indented at the end of a block, with repeated text and
with text holding other comments; blank lines; quoted strings holding
`` #``; anchors and aliases on keys and values; merge keys; flow
collections with and without inner spaces; block scalars with every
chomping indicator; bare ``-`` items; aligned values; trailing spaces;
``True``, ``~`` and ``null`` spellings; wrapped scalars; document
markers; anchored and tagged mappings and empty values; explicit ``?``
keys; flow mappings with trailing commas, spanning lines and holding
comments; block scalars holding only spaces; CRLF line ends, a byte order
mark and U+0085.

The oracle shares no code with the editor. It finds what changed by
comparing the texts, reads values with ``ruamel.yaml``'s safe loader and
comments with its scanner. An accepted edit must change only text inside
the key's old value, or insert whole lines for a new key; keep every
comment token, in order and in number; leave every other key equal; give
the key the value set; start every line it inserts or replaces at the
file's own indent; keep the file's line ends and byte order mark; and be a
no-op when made again. A refused edit must raise :class:`ConfigError` for a
listed reason and leave the file as it was.

``WINNOW_FUZZ_SEEDS`` sets how many seeds the run covers.
"""

from __future__ import annotations

import hashlib
import io
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import cast

from assertpy import assert_that
from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, Node, SequenceNode
from ruamel.yaml.tokens import CommentToken

from winnow.config import set_config_value
from winnow.exceptions import ConfigError

_SEEDS = range(int(os.environ.get("WINNOW_FUZZ_SEEDS", "540")))
"""Seeds the run covers: 60 per shape in the unit run."""
_MIN_ACCEPTED = 0.8
"""The share of each shape's edits that must be accepted."""
_EXPECTED_REFUSALS = (
    "a comment inside its old value",
    "shared through a YAML anchor",
    "holds a comment, which would then follow the new key",
    "explicit '?' key or a key without ':'",
    "U+0085, U+2028 or U+2029",
)
"""The only reasons a generated edit may be refused."""
_BOM = "\ufeff"


class Shape(StrEnum):
    """The kind of edit a seed makes."""

    TOP_SCALAR = auto()
    NESTED_SCALAR = auto()
    REPLACE_LIST = auto()
    REPLACE_MAPPING = auto()
    NEW_TOP_KEY = auto()
    NEW_NESTED_KEY = auto()
    SET_NULL = auto()
    NULL_TO_LIST = auto()
    NULL_TO_MAPPING = auto()
    LIST_TO_SCALAR = auto()
    MAPPING_TO_SCALAR = auto()
    EMPTY_LIST = auto()
    FLOW_INSERT = auto()
    EXPLICIT_KEY = auto()
    PROPERTY_INSERT = auto()
    BLOCK_SCALAR = auto()


_SHAPES = list(Shape)
_INSERTS = frozenset(
    {Shape.NEW_TOP_KEY, Shape.NEW_NESTED_KEY, Shape.EXPLICIT_KEY}
    | {Shape.PROPERTY_INSERT},
)
_ADDS_GRAPHICS = _INSERTS - {Shape.NEW_TOP_KEY} | {Shape.FLOW_INSERT}
_MIN_ACCEPTED_BY_SHAPE = {Shape.FLOW_INSERT: 0.5, Shape.EXPLICIT_KEY: 0.3}
"""Shapes whose files hold what refuses an edit by design, more often."""
_EDITS: dict[Shape, tuple[str, object]] = {
    Shape.TOP_SCALAR: ("workers", 3),
    Shape.NESTED_SCALAR: ("cache.enabled", False),
    Shape.REPLACE_LIST: ("source_dirs", ["/x", "/videos"]),
    Shape.REPLACE_MAPPING: ("cache", {"enabled": False, "directory": "/d"}),
    Shape.NEW_TOP_KEY: ("dry_run", True),
    Shape.NEW_NESTED_KEY: ("routing.graphics", "Drawings"),
    Shape.SET_NULL: ("paths.output_dir", None),
    Shape.NULL_TO_LIST: ("source_dirs", ["/a", "/b"]),
    Shape.NULL_TO_MAPPING: ("cache.enabled", True),
    Shape.LIST_TO_SCALAR: ("paths.output_dir", "/new"),
    Shape.MAPPING_TO_SCALAR: ("paths.output_dir", "/new"),
    Shape.EMPTY_LIST: ("source_dirs", []),
    Shape.FLOW_INSERT: ("routing.graphics", "Drawings"),
    Shape.EXPLICIT_KEY: ("routing.graphics", "Drawings"),
    Shape.PROPERTY_INSERT: ("routing.graphics", "Drawings"),
    Shape.BLOCK_SCALAR: ("paths.output_dir", "/new"),
}
"""The key and value each shape sets."""


class _Picker:
    """Deterministic choices from a seed, the same on every platform."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._count = 0

    def below(self, stop: int) -> int:
        """Return a number from 0 up to, not including, ``stop``."""
        self._count += 1
        digest = hashlib.sha256(f"{self._seed}:{self._count}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % stop

    def chance(self, one_in: int) -> bool:
        """Return true once in ``one_in`` draws."""
        return self.below(one_in) == 0

    def pick(self, items: list[str]) -> str:
        """Return one of ``items``."""
        return items[self.below(len(items))]


@dataclass(frozen=True, slots=True)
class _Style:
    """How one generated file lays out its collections."""

    mapping: int
    offset: int
    null: str
    true: str
    gap: str
    """What follows a key's colon before an inline value."""

    @property
    def item(self) -> int:
        """Return the column of an item's content under its key."""
        return self.offset + 2


@dataclass(frozen=True, slots=True)
class Case:
    """One generated file and the edit made on it."""

    text: str
    shape: Shape
    key: str
    value: object
    crlf: bool = False
    bom: bool = False

    def raw(self, text: str) -> str:
        """Return ``\\n``-ended text laid out as this case's file is."""
        text = text.replace("\n", "\r\n") if self.crlf else text
        return _BOM + text if self.bom else text


_COMMENTS = ["  # note", " # note", "  # note # note", " # x: y", "  #"]
_SCALARS = [
    "a",
    "/photos",
    "'q: x'",
    '"- d"',
    "1",
    "x # y",
    '"Review #1"',
    "'Photos # old'",
    '"[~]"',
    "True",
    "~",
    "null",
    "Null",
    "2024-01-02",
    "1.5",
]
_FLOWS = ["[a, b]", "{a: 1}", "[]", "{}", "[ a, b ]", "{ a: 1 }", "[~, True]"]


class _Generator:
    """Writes one file's lines from a seed."""

    def __init__(self, rng: _Picker, style: _Style, shape: Shape) -> None:
        self.rng = rng
        self.style = style
        self.shape = shape
        self.anchors: list[str] = []
        self.base = False

    def comment(self, one_in: int = 5) -> str:
        """Return an inline comment once in ``one_in`` calls, else nothing."""
        return self.rng.pick(_COMMENTS) if self.rng.chance(one_in) else ""

    def tail(self) -> str:
        """Return trailing spaces now and then."""
        return "   " if self.rng.chance(15) else ""

    def scalar(self) -> str:
        """Return a filler scalar, sometimes anchored or an alias."""
        if self.anchors and self.rng.chance(12):
            return f"*{self.rng.pick(self.anchors)}"
        value = self.rng.pick(_SCALARS)
        if self.rng.chance(10):
            name = f"a{len(self.anchors)}"
            self.anchors.append(name)
            return f"&{name} {value}"
        return value

    def after_block(self, depth: int) -> list[str]:
        """Return what may follow a block collection."""
        deeper = " " * (depth + self.style.mapping)
        return [
            [],
            [],
            ["# after"],
            [""],
            [f"{deeper}# block end"],
            [f"{deeper}# block end", ""],
        ][self.rng.below(6)]

    def block_scalar(self, depth: int, key: str) -> list[str]:
        """Return a block scalar with any chomping and perhaps a header comment."""
        pad = " " * depth
        body = " " * (depth + self.style.mapping)
        header = self.rng.pick(["|", ">", "|-", ">-", "|+", ">+"])
        if self.rng.chance(6):
            return [f"{pad}{key}: {header}{self.style.mapping}", f"{body}   "]
        if self.rng.chance(4):
            header += str(self.style.mapping)
        if self.rng.chance(4):
            header += " # header"
        text = [body + self.rng.pick(["- # text", "key:", "x: y", "#c", "a b"])]
        text += [""] if "+" in header else []
        return [f"{pad}{key}: {header}", *text]

    def sequence(self, depth: int, key: str, comments: int = 5) -> list[str]:
        """Return a block sequence under ``key`` at ``depth``."""
        dash = " " * (depth + self.style.offset)
        lines = [f"{' ' * depth}{key}:{self.comment(comments)}"]
        for _ in range(1 + self.rng.below(3)):
            kind = self.rng.below(10)
            if kind < 7:
                lines.append(f"{dash}- {self.scalar()}{self.comment(comments)}")
            elif kind == 7:
                lines += [f"{dash}-", f"{' ' * (depth + self.style.item)}bare"]
            elif kind == 8:
                lines.append(f"{dash}- {self.rng.pick(_FLOWS)}")
            else:
                inner = " " * (depth + self.style.item)
                lines += [f"{dash}- name: {self.scalar()}", f"{inner}size: 3"]
        return lines

    def entry(self, depth: int, key: str) -> list[str]:
        """Return one filler mapping entry of any kind at ``depth``."""
        kind = self.rng.below(12 if depth < 2 * self.style.mapping else 8)
        name = key
        if self.rng.chance(12):
            self.anchors.append(f"k{len(self.anchors)}")
            key = f"&{self.anchors[-1]} {key}"
        if kind < len(self._LEAVES):
            return self._LEAVES[kind](self, " " * depth, key)
        if kind == 5:
            return self.block_scalar(depth, key)
        if kind in {6, 7}:
            body = " " * (depth + self.style.mapping)
            first, second = (
                ("wrapped words", "go on") if kind == 6 else ('"quoted', 'wrapped"')
            )
            return [f"{' ' * depth}{key}: {first}", f"{body}{second}"]
        if kind in {8, 9}:
            return self.sequence(depth, key) + self.after_block(depth)
        if kind == 10:
            merge = "{<<: *base, b: 2}" if self.base else "&base {a: 1}"
            self.base = True
            return [f"{' ' * depth}{key}: {merge}"]
        props = self.rng.pick(["", "", "", " !!map", f" &m_{name}"])
        lines = [f"{' ' * depth}{key}:{props}{self.comment()}"]
        for index in range(1 + self.rng.below(3)):
            lines += self.entry(depth + self.style.mapping, f"{name}_{index}")
        if self.rng.chance(6):
            inner = " " * (depth + self.style.mapping)
            lines += [f"{inner}? {name}_q", f"{inner}: v"]
        return lines + self.after_block(depth)

    def _scalar_entry(self, pad: str, key: str) -> list[str]:
        """Return an entry holding a scalar on the key's line."""
        return [f"{pad}{key}:{self.style.gap}{self.scalar()}{self.comment()}"]

    def _null_entry(self, pad: str, key: str) -> list[str]:
        """Return an entry holding a null in the file's spelling."""
        null = " !!null" if self.rng.chance(6) else self.style.null
        return [f"{pad}{key}:{null}{self.comment()}{self.tail()}"]

    def _flow_entry(self, pad: str, key: str) -> list[str]:
        """Return an entry holding a flow collection."""
        return [f"{pad}{key}: {self.rng.pick(_FLOWS)}{self.comment()}"]

    _LEAVES = (_scalar_entry, _scalar_entry, _scalar_entry, _null_entry, _flow_entry)
    """The one-line entries, by kind."""

    def mapping(
        self,
        key: str,
        entries: Sequence[str | list[str]],
        comments: int = 8,
        props: str = "",
    ) -> list[str]:
        """Return an edited top-level block mapping of ``entries``.

        An entry given as lines is written as they are, under the pad.
        """
        pad = " " * self.style.mapping
        lines = [f"{key}:{props}{self.comment(comments)}"]
        for entry in entries:
            if isinstance(entry, list):
                lines += [f"{pad}{line}" for line in entry]
                continue
            if self.rng.chance(10):
                lines.append(f"{pad}# about {entry.partition(':')[0]}")
            lines.append(f"{pad}{entry}{self.comment(comments)}{self.tail()}")
        return lines + self.after_block(0)

    def section(self, name: str) -> list[str]:
        """Return the section an edit touches, laid out for the shape."""
        return {
            "workers": self._workers,
            "source_dirs": self._sources,
            "cache": self._cache,
            "routing": self._routing,
            "paths": self._paths,
        }[name]()

    def _workers(self) -> list[str]:
        """Return the ``workers`` entry, perhaps anchored."""
        if self.rng.chance(8):
            self.anchors.append("w")
            return [f"workers: &w 2{self.comment()}"]
        return [f"workers:{self.style.gap}2{self.comment()}{self.tail()}"]

    def _sources(self) -> list[str]:
        """Return ``source_dirs`` as a null, a flow list or a block list."""
        if self.shape is Shape.NULL_TO_LIST:
            return [f"source_dirs:{self.style.null}{self.comment(14)}"]
        if self.shape is not Shape.REPLACE_LIST and self.rng.chance(4):
            return [f"source_dirs: {self.rng.pick(['[/a, /b]', '[ /a ]'])}"]
        dash = " " * self.style.offset
        lines = [f"source_dirs:{self.comment(14)}"]
        for _ in range(1 + self.rng.below(3)):
            item = self.rng.pick(["/photos", "'/a b'", '"/c #1"', "/d"])
            if self.rng.chance(8):
                lines += [f"{dash}-", f"{dash}  {item}"]
            else:
                lines.append(f"{dash}- {item}{self.comment(14)}")
        return lines + self.after_block(0)

    def _cache(self) -> list[str]:
        """Return ``cache`` as a null or a block mapping."""
        if self.shape is Shape.NULL_TO_MAPPING:
            return [f"cache:{self.style.null}{self.comment(14)}"]
        entries = [f"enabled:{self.style.gap}{self.style.true}"]
        entries += ["directory: /data/cache"] if self.rng.chance(2) else []
        return self.mapping("cache", entries, comments=14)

    def _routing(self) -> list[str]:
        """Return ``routing``, without ``graphics`` when the edit adds it."""
        routing: list[str | list[str]] = ["review: Review"]
        if self.shape not in _ADDS_GRAPHICS and self.rng.chance(2):
            routing.append("graphics: Art")
        if self.shape is Shape.FLOW_INSERT:
            return self._flow_routing([str(entry) for entry in routing])
        if self.shape is Shape.EXPLICIT_KEY and self.rng.chance(2):
            routing.insert(self.rng.below(2), ["? screenshots", ": Shots"])
        props = ""
        if self.shape is Shape.PROPERTY_INSERT or self.rng.chance(6):
            props = self.rng.pick([" &r", " !!map", " &r !!map"])
        return self.mapping("routing", routing, props=props)

    def _flow_routing(self, entries: list[str]) -> list[str]:
        """Return ``routing`` as a flow mapping, perhaps over several lines."""
        trailing = self.rng.pick(["", "", ",", " ,", ", "])
        if self.rng.chance(2):
            body = self.rng.pick([", ", ","]).join(entries) + trailing
            return [f"routing: {{{body}}}{self.comment()}"]
        note = "  # note" if self.rng.chance(3) else ""
        first, *rest = entries
        return [
            f"routing: {{{first},{note}",
            *(f"  {entry}," for entry in rest),
            "  }",
        ]

    def _paths(self) -> list[str]:
        """Return ``paths``, its output perhaps quoted, anchored, null or a block."""
        output: str | list[str] = self.rng.pick(
            ["/out", '"/tmp/dir #1"', "&o /out", "~", "!!null"],
        )
        item = " " * self.style.offset
        nested = " " * self.style.mapping
        if self.shape is Shape.LIST_TO_SCALAR:
            output = self.rng.pick(["[/a, /b]", "[]"])
            if self.rng.chance(2):
                output = ["output_dir:", f"{item}- /a", f"{item}- /b"]
        elif self.shape is Shape.MAPPING_TO_SCALAR:
            output = ["output_dir:", f"{nested}dir: /a", f"{nested}name: x"]
        elif self.shape is Shape.BLOCK_SCALAR:
            header = self.rng.pick(["|", ">", "|-", "|+"]) + str(self.style.mapping)
            output = [f"output_dir: {header}", f"{nested}{' ' * self.rng.below(4)}"]
        if isinstance(output, str) and output.startswith("&"):
            self.anchors.append("o")
        entry = f"output_dir: {output}" if isinstance(output, str) else output
        return self.mapping("paths", [entry, "quarantine_dir: /q"])


_SECTIONS = ["workers", "source_dirs", "cache", "routing", "paths"]


def generate(seed: int) -> Case:
    """Return a generated file and the edit its seed makes."""
    return generate_with_style(seed)[0]


def generate_with_style(seed: int) -> tuple[Case, _Style]:
    """Return a generated file, the edit its seed makes and the file's layout."""
    shape = _SHAPES[seed % len(_SHAPES)]
    rng = _Picker(seed)
    style = _Style(
        mapping=int(rng.pick(["2", "4"])),
        offset=int(rng.pick(["0", "2"])),
        null=rng.pick(["", " null", " ~", " Null"]),
        true=rng.pick(["true", "True", "TRUE"]),
        gap=rng.pick([" ", " ", "    "]),
    )
    generator = _Generator(rng, style, shape)
    order = [f"f{index}" for index in range(rng.below(4))]
    for name in _SECTIONS:
        order.insert(rng.below(len(order) + 1), name)
    lines = [["# header", ""], ["%YAML 1.2", "---"], ["--- # start"], [], []][
        rng.below(5)
    ]
    for index, name in enumerate(order):
        if index and rng.chance(3):
            lines.append(rng.pick(["", "# section", "# section # section"]))
        if name in _SECTIONS:
            lines += generator.section(name)
        else:
            lines += generator.entry(0, name)
    if generator.anchors and rng.chance(2):
        lines.append(f"ref: *{rng.pick(generator.anchors)}")
    lines += [[], [], [""], ["# end"], ["..."]][rng.below(5)]
    if rng.chance(40):
        lines.insert(0, "# next line\x85")
    key, value = _EDITS[shape]
    case = Case(
        text="\n".join(lines) + "\n",
        shape=shape,
        key=key,
        value=value,
        crlf=rng.chance(8),
        bom=rng.chance(8),
    )
    return case, style


def _load(text: str) -> dict[str, object]:
    """Return YAML content read with the safe loader."""
    loaded = YAML(typ="safe", pure=True).load(text)
    return loaded if isinstance(loaded, dict) else {}


def _comments(text: str) -> list[str]:
    """Return the text of every comment line the scanner finds, in order."""
    found: list[str] = []

    def collect(attached: object) -> None:
        if isinstance(attached, CommentToken):
            found.extend(
                line.strip() for line in attached.value.split("\n") if line.strip()
            )
        elif isinstance(attached, list):
            for item in attached:
                collect(item)

    for token in YAML().scan(io.StringIO(text)):
        collect(getattr(token, "_comment", None))
    return found


def _without(settings: dict[str, object], key: str) -> dict[str, object]:
    """Return settings with a dotted key and the parents it empties removed."""
    copied = dict(settings)
    head, _, rest = key.partition(".")
    child = copied.get(head)
    if rest and isinstance(child, dict):
        reduced = _without(child, rest)
        if reduced:
            copied[head] = reduced
            return copied
    copied.pop(head, None)
    return copied


def _value_region(text: str, key: str) -> tuple[int, int, int]:
    """Return where a key's old value may change, and the key's column.

    The range runs from the key's end to its value's last leaf; a key below
    a ``null`` counts from the key that holds the ``null``.
    """
    node: Node = YAML().compose(io.StringIO(text))
    key_node: Node | None = None
    for part in key.split("."):
        if not isinstance(node, MappingNode):
            break
        key_node, node = next(
            (name, value) for name, value in node.value if name.value == part
        )
    while (
        isinstance(node, MappingNode | SequenceNode)
        and node.value
        and not node.flow_style
    ):
        last = node.value[-1]
        node = last[1] if isinstance(node, MappingNode) else last
    assert_that(key_node).is_not_none()
    found = cast("Node", key_node)
    return found.end_mark.index, node.end_mark.index, found.start_mark.column


def _sequence_offsets(text: str, style: _Style) -> set[int]:
    """Return the dash offsets a new block sequence in the file may use.

    The file's own offset when it has a block sequence under a key whose
    first item is on its dash's line, else that or the default of 2.
    """
    pending: list[Node] = [YAML().compose(io.StringIO(text))]
    while pending:
        node = pending.pop()
        if isinstance(node, SequenceNode):
            pending.extend(node.value)
        if not isinstance(node, MappingNode):
            continue
        for _name, value in node.value:
            pending.append(value)
            if (
                isinstance(value, SequenceNode)
                and not value.flow_style
                and value.value
                and value.value[0].start_mark.line == value.start_mark.line
            ):
                return {style.offset}
    return {style.offset, 2}


def _indent(line: str) -> int:
    """Return how many spaces start a line."""
    return len(line) - len(line.lstrip(" "))


def _changed(before: str, after: str) -> tuple[int, int, str]:
    """Return the range of ``before`` that changed and the text replacing it."""
    start = 0
    limit = min(len(before), len(after))
    while start < limit and before[start] == after[start]:
        start += 1
    end = 0
    while end < limit - start and before[-1 - end] == after[-1 - end]:
        end += 1
    return start, len(before) - end, after[start : len(after) - end]


def _check_inserted(case: Case, before: str, after: str) -> None:
    """Check an inserted entry: whole lines, at its siblings' column."""
    start, end, _ = _changed(before, after)
    assert_that(end).is_equal_to(start)
    line_start = before.rfind("\n", 0, start) + 1
    lines = after[line_start : line_start + len(after) - len(before)]
    assert_that(after).is_equal_to(before[:line_start] + lines + before[line_start:])
    assert_that(lines).ends_with("\n")
    root = YAML().compose(io.StringIO(before))
    parent = root
    for part in case.key.split(".")[:-1]:
        parent = next(value for name, value in parent.value if name.value == part)
    sibling = parent.value[0][0].start_mark.line
    column = _indent(before.splitlines()[sibling])
    for line in lines.splitlines():
        assert_that(_indent(line)).described_as(line).is_equal_to(column)


def _check_flow_insert(case: Case, before: str, after: str) -> None:
    """Check an entry added to a flow mapping: on its line, inside the braces."""
    start, end, text = _changed(before, after)
    assert_that(end).is_equal_to(start)
    assert_that(text).does_not_contain("\n")
    root = YAML().compose(io.StringIO(before))
    routing = next(value for name, value in root.value if name.value == "routing")
    assert_that(start).is_greater_than(routing.start_mark.index)
    assert_that(start).is_less_than(routing.end_mark.index)
    assert_that(after).does_not_contain(",,").does_not_contain(", ,")


def _check_replaced(case: Case, before: str, after: str, style: _Style) -> None:
    """Check a replaced value: inside the old one, its new lines at the indent."""
    start, end, text = _changed(before, after)
    region_start, region_end, column = _value_region(before, case.key)
    assert_that(start).is_greater_than_or_equal_to(region_start)
    assert_that(end).is_less_than_or_equal_to(region_end)
    offsets = _sequence_offsets(before, style)
    line_start = after.rfind("\n", 0, start) + 1
    new_lines = after[line_start : start + len(text)].split("\n")[1:]
    for line in new_lines:
        if not line.strip():
            continue
        allowed = (
            {column + offset for offset in offsets}
            if line.lstrip().startswith("-")
            else {column + style.mapping}
        )
        assert_that(allowed).described_as(line).contains(_indent(line))


def _check_accepted(case: Case, after: str, style: _Style) -> None:
    """Check an accepted edit with the oracle."""
    before = case.text
    old_settings, new_settings = _load(before), _load(after)
    found: object = new_settings
    for part in case.key.split("."):
        assert_that(found).is_instance_of(dict)
        found = cast("dict[str, object]", found)[part]
    assert_that(found).is_equal_to(case.value)
    assert_that(type(found)).is_equal_to(type(case.value))
    assert_that(_without(new_settings, case.key)).is_equal_to(
        _without(old_settings, case.key),
    )
    assert_that(_comments(after)).is_equal_to(_comments(before))
    if after == before:
        return
    if case.shape is Shape.FLOW_INSERT:
        _check_flow_insert(case, before, after)
    elif case.shape in _INSERTS:
        _check_inserted(case, before, after)
    else:
        _check_replaced(case, before, after, style)


def _read_raw(path: Path) -> str:
    """Return a file's text with its line ends and byte order mark."""
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def check(case: Case, directory: Path, style: _Style) -> str | None:
    """Make a case's edit on its file and check the result.

    Returns:
        ``None`` when the edit was made, or the refusal's message.
    """
    _load(case.text)
    config_path = directory / "c.yaml"
    raw = case.raw(case.text)
    with config_path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(raw)
    try:
        set_config_value(case.key, case.value, config_path=config_path)
    except ConfigError as error:
        assert_that(_read_raw(config_path)).is_equal_to(raw)
        return error.message
    after_raw = _read_raw(config_path)
    after = after_raw.removeprefix(_BOM).replace("\r\n", "\n")
    assert_that(after_raw).is_equal_to(case.raw(after))
    _check_accepted(case, after, style)
    set_config_value(case.key, case.value, config_path=config_path)
    assert_that(_read_raw(config_path)).is_equal_to(after_raw)
    return None


def run(seeds: range, directory: Path) -> dict[Shape, Counter[str]]:
    """Check every seed and count the outcomes per shape.

    Returns:
        For each shape, ``"accepted"`` and each refusal message counted.
    """
    outcomes: dict[Shape, Counter[str]] = {shape: Counter() for shape in Shape}
    for seed in seeds:
        case, style = generate_with_style(seed)
        seed_directory = directory / str(seed)
        seed_directory.mkdir()
        try:
            refusal = check(case, seed_directory, style)
        except AssertionError as error:
            message = f"seed {seed} ({case.shape}): {case.text!r}\n{error}"
            raise AssertionError(message) from error
        outcomes[case.shape][refusal or "accepted"] += 1
    return outcomes


def test_generated_files_change_only_the_value_or_refuse(tmp_path: Path) -> None:
    """Every shape edits its key's text alone, or refuses for a listed reason."""
    outcomes = run(_SEEDS, tmp_path)

    for shape, counts in outcomes.items():
        total = sum(counts.values())
        minimum = _MIN_ACCEPTED_BY_SHAPE.get(shape, _MIN_ACCEPTED)
        assert_that(counts["accepted"]).described_as(
            f"{shape}: {dict(counts)}",
        ).is_greater_than_or_equal_to(int(total * minimum))
        refusals = [reason for reason in counts if reason != "accepted"]
        for refusal in refusals:
            assert_that(
                any(reason in refusal for reason in _EXPECTED_REFUSALS),
            ).described_as(refusal).is_true()


def test_every_shape_is_generated() -> None:
    """The unit run makes every edit shape."""
    shapes = {generate(seed).shape for seed in _SEEDS}

    assert_that(shapes).is_equal_to(set(Shape))
