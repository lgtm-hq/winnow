"""Writing one config value as YAML text in the style of the file it goes in.

``config set`` replaces only the text of the value it sets (#429); this
module writes that text. The file's mapping indent and sequence indent and
offset are read from its nodes. Nulls are written ``null`` and booleans
``true`` and ``false``, strings that hold a line break are double-quoted so
they stay on one line, and nothing is folded.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import RoundTripRepresenter

from winnow.config._yaml_tree import LINE_BREAKS, NodeKind, YamlNode, YamlTree

_DEFAULT_MAPPING_INDENT: Final = 2
_DEFAULT_SEQUENCE_INDENT: Final = 4
_DEFAULT_SEQUENCE_OFFSET: Final = 2
_MIN_ITEM_GAP: Final = 2
"""An item's content lies at least this far past its dash."""
_NULL_TAG: Final = "tag:yaml.org,2002:null"
_STR_TAG: Final = "tag:yaml.org,2002:str"
_PLACEHOLDER: Final = "k"


@dataclass(frozen=True, slots=True)
class YamlStyle:
    """How a file indents block collections under their key."""

    mapping: int = _DEFAULT_MAPPING_INDENT
    """Columns from a key to the keys of the mapping it holds."""
    sequence: int = _DEFAULT_SEQUENCE_INDENT
    """Columns from a key to the content of the items of its sequence."""
    offset: int = _DEFAULT_SEQUENCE_OFFSET
    """Columns from a key to the dashes of its sequence."""


def file_style(tree: YamlTree) -> YamlStyle:
    """Return how a file indents block collections, or the defaults.

    The first block mapping held by a block mapping's key gives the mapping
    indent; the first block sequence held by a key whose first item starts
    on its dash's line gives the sequence offset and indent.
    """
    mapping: int | None = None
    sequence: tuple[int, int] | None = None
    for node in tree.nodes():
        if node.flow:
            continue
        for pair in node.pairs:
            value = pair.value
            if value.flow or value.kind is NodeKind.SCALAR:
                continue
            key_column = pair.key.column
            if value.pairs and mapping is None:
                indent = value.pairs[0].key.column - key_column
                mapping = indent if indent > 0 else None
            elif value.items and sequence is None:
                sequence = _sequence_indent(tree, value, key_column)
    offset, indent = sequence or (_DEFAULT_SEQUENCE_OFFSET, _DEFAULT_SEQUENCE_INDENT)
    return YamlStyle(
        mapping=mapping or _DEFAULT_MAPPING_INDENT,
        sequence=indent,
        offset=offset,
    )


def _sequence_indent(
    tree: YamlTree,
    sequence: YamlNode,
    key_column: int,
) -> tuple[int, int] | None:
    """Return a block sequence's dash offset and item indent under its key."""
    dash = sequence.start
    offset = tree.column_of(dash) - key_column
    item = sequence.items[0]
    if offset < 0 or item.is_empty or tree.line_of(item.start) != tree.line_of(dash):
        return None
    indent = tree.column_of(item.start) - key_column
    return (offset, indent) if indent >= offset + _MIN_ITEM_GAP else None


class _Representer(RoundTripRepresenter):
    """Writes nulls as ``null`` and keeps every string on one line."""

    def ignore_aliases(self, data: object) -> bool:
        """Never write an anchor, even for a value given twice."""
        return True

    def represent_none(self, data: None) -> ScalarNode:
        """Write a null as ``null``."""
        return self.represent_scalar(_NULL_TAG, "null")

    def represent_str(self, data: str) -> ScalarNode:
        """Double-quote a string that holds a line break."""
        if any(char in LINE_BREAKS or char == "\r" for char in data):
            return self.represent_scalar(_STR_TAG, data, style='"')
        node: ScalarNode = super().represent_str(data)
        return node


_Representer.add_representer(type(None), _Representer.represent_none)
_Representer.add_representer(str, _Representer.represent_str)


def _dump(value: object, style: YamlStyle, *, flow: bool) -> str:
    """Dump a value as a YAML document."""
    yaml = YAML()
    yaml.Representer = _Representer
    yaml.width = sys.maxsize
    yaml.default_flow_style = flow
    yaml.indent(mapping=style.mapping, sequence=style.sequence, offset=style.offset)
    stream = io.StringIO()
    yaml.dump(value, stream)
    return stream.getvalue()


def is_block(value: object) -> bool:
    """Return whether a value is written as a block below its key."""
    return isinstance(value, dict | list) and bool(value)


def render_inline(value: object, style: YamlStyle, *, flow: bool) -> str:
    """Return a value written on one line, as it follows ``key: ``.

    Args:
        value: The value; a collection is written in flow style.
        style: The file's indents.
        flow: Whether the value goes inside a flow collection.

    Raises:
        ValueError: If the value cannot be written on one line.
    """
    text = _dump({_PLACEHOLDER: value}, style, flow=flow or is_block(value))
    prefix = f"{{{_PLACEHOLDER}: " if flow or is_block(value) else f"{_PLACEHOLDER}: "
    suffix = "}\n" if flow or is_block(value) else "\n"
    rendered = text.removeprefix(prefix).removesuffix(suffix)
    if len(rendered) != len(text) - len(prefix) - len(suffix) or any(
        char in LINE_BREAKS for char in rendered
    ):
        message = f"cannot write {value!r} on one line"
        raise ValueError(message)
    return rendered


def render_entry(key: str, value: object, style: YamlStyle, *, column: int) -> str:
    """Return a mapping entry as whole lines, indented to ``column``.

    Args:
        key: The entry's key.
        value: Its value; a non-empty collection goes below the key.
        style: The file's indents.
        column: The column of the entry's key.

    Returns:
        The entry's lines, each ending in ``\\n``.
    """
    lines = _dump({key: value}, style, flow=False).splitlines(keepends=True)
    return "".join(" " * column + line for line in lines)


def render_block(
    value: object,
    style: YamlStyle,
    *,
    key_column: int,
    column: int | None = None,
) -> list[str]:
    """Return a non-empty collection's lines, as they go below its key.

    Args:
        value: The collection.
        style: The file's indents.
        key_column: The column of the key that holds the value.
        column: The column the first line must start at, when the value
            takes the place of a collection that started there; by default
            the file's indents under the key decide.

    Returns:
        The lines without line breaks; the others keep their indent
        relative to the first.
    """
    lines = _dump({_PLACEHOLDER: value}, style, flow=False).splitlines()[1:]
    if column is None:
        return [" " * key_column + line for line in lines]
    base = len(lines[0]) - len(lines[0].lstrip(" "))
    return [" " * column + line[base:] for line in lines]
