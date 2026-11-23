"""How a YAML config file writes its values, so a dump writes them alike.

``config set`` dumps an edited YAML file through ``ruamel.yaml``, which has
its own indents and its own spellings of null and booleans. These helpers
read the file's own and set the dump up to match, so the lines the edit does
not touch come back as they were (#429).
"""

from __future__ import annotations

import re
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from ruamel.yaml.representer import RoundTripRepresenter

_DEFAULT_MAPPING_INDENT: Final = 2
_DEFAULT_SEQUENCE_INDENT: Final = 4
_DEFAULT_SEQUENCE_OFFSET: Final = 2
_YAML_WIDTH: Final = 4096
"""Wide enough that the dump never folds a long value onto a new line."""
_INLINE_COMMENT: Final = re.compile(r"\s+#.*$")
"""A YAML comment after content on the same line."""
_BLOCK_SCALAR: Final = re.compile(r"(?:^|\s)[|>][-+1-9]{0,2}$")
"""A literal or folded block scalar header at the end of a line."""
_NODE_PROPERTIES: Final = re.compile(r"(?:\s+[&!]\S*)+$")
"""Anchors and tags after a key, before the value on the next lines."""
_SEQUENCE_LOOKAHEAD: Final = 64
"""How many lines past a key the sequence indent guess reads."""
_NULL_TAG: Final = "tag:yaml.org,2002:null"
_BOOL_TAG: Final = "tag:yaml.org,2002:bool"
_BOOL_SPELLINGS: Final = (
    ("false", "true"),
    ("False", "True"),
    ("FALSE", "TRUE"),
)
"""The ways YAML 1.2 spells ``false`` and ``true``, by letter case."""
_BLANK: Final = " \t\n"
"""The characters a blank YAML line holds."""


def round_trip_yaml(text: str) -> YAML:
    """Return a round-trip YAML instance indented like ``text``."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = _YAML_WIDTH
    yaml.default_flow_style = False
    lines = _structure_lines(text)
    null_spelling, bool_spelling = _guess_spellings(text)
    yaml.Representer = _SpelledRepresenter
    yaml.representer.null_spelling = null_spelling
    yaml.representer.bool_spelling = bool_spelling
    sequence_indent, sequence_offset = _guess_sequence_indent(lines)
    yaml.indent(
        mapping=_guess_mapping_indent(lines),
        sequence=sequence_indent,
        offset=sequence_offset,
    )
    return yaml


class _SpelledRepresenter(RoundTripRepresenter):
    """A round-trip representer that writes nulls and booleans as the file does."""

    null_spelling: str = ""
    """How the file writes a null; empty keeps ``ruamel.yaml``'s own form."""
    bool_spelling: tuple[str, str] = _BOOL_SPELLINGS[0]
    """How the file writes ``false`` and ``true``."""

    def represent_none(self, data: None) -> ScalarNode:
        """Represent ``None`` with the file's spelling of null."""
        if not self.null_spelling:
            return super().represent_none(data)
        return self.represent_scalar(_NULL_TAG, self.null_spelling)

    def represent_bool(
        self,
        data: object,
        anchor: object | None = None,
    ) -> ScalarNode:
        """Represent a boolean in the file's letter case."""
        value = self.bool_spelling[bool(data)]
        return self.represent_scalar(_BOOL_TAG, value, anchor=anchor)


_SpelledRepresenter.add_representer(type(None), _SpelledRepresenter.represent_none)
_SpelledRepresenter.add_representer(bool, _SpelledRepresenter.represent_bool)


def _guess_spellings(text: str) -> tuple[str, tuple[str, str]]:
    """Return how YAML content spells its nulls and booleans.

    Only values the parser reads as null or boolean count, so a quoted
    ``"null"`` or ``"[~]"`` does not.

    Returns:
        The one spelling of null (``null``, ``Null``, ``NULL`` or ``~``) the
        content writes out, or empty, ``ruamel.yaml``'s own form, when it
        writes none or more than one; and the letter case of its booleans,
        lowercase when it has none or mixes cases. A file that mixes
        spellings comes back changed, and the round-trip check refuses it.
    """
    nulls: set[str] = set()
    bools: set[str] = set()
    root = YAML().compose(text)
    seen: set[int] = set()
    pending: list[Node] = [] if root is None else [root]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, MappingNode):
            pending.extend(part for pair in node.value for part in pair)
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
        elif node.tag == _NULL_TAG and node.value:
            nulls.add(node.value)
        elif node.tag == _BOOL_TAG:
            bools.add(node.value)
    cases = [spelling for spelling in _BOOL_SPELLINGS if bools <= set(spelling)]
    return (
        nulls.pop() if len(nulls) == 1 else "",
        cases[0] if cases else _BOOL_SPELLINGS[0],
    )


def _structure_lines(text: str) -> list[tuple[int, str]]:
    """Return the lines of ``text`` that show its block structure.

    Blank lines, comment lines and the content of block scalars are left
    out, and inline comments are cut off. Lines end where the YAML parser
    ends them.

    Returns:
        Each line's indent and its content without the indent.
    """
    found: list[tuple[int, str]] = []
    block_depth: int | None = None
    for line in text.splitlines():
        content = line.lstrip(" ")
        depth = len(line) - len(content)
        blank = not content.strip(_BLANK)
        if block_depth is not None and (blank or depth > block_depth):
            continue
        block_depth = None
        if blank or content.startswith("#"):
            continue
        content = _INLINE_COMMENT.sub("", content).rstrip(_BLANK)
        found.append((depth, content))
        if _BLOCK_SCALAR.search(content):
            block_depth = depth
    return found


def _key_column(depth: int, content: str) -> tuple[int, str]:
    """Return where the node on a line starts, past any sequence dashes."""
    rest = _dash_rest(content)
    while rest is not None:
        depth += len(content) - len(rest)
        content = rest
        rest = _dash_rest(content)
    return depth, content


def _opens_block(content: str) -> bool:
    """Return whether a line ends in a key whose value starts on the next line."""
    return _NODE_PROPERTIES.sub("", content).endswith(":")


def _guess_mapping_indent(lines: list[tuple[int, str]]) -> int:
    """Return how far the lines indent a nested mapping under its key."""
    for (depth, content), (nested_depth, nested) in zip(
        lines,
        lines[1:],
        strict=False,
    ):
        column, key = _key_column(depth, content)
        if _opens_block(key) and nested_depth > column and not nested.startswith("-"):
            return nested_depth - column
    return _DEFAULT_MAPPING_INDENT


def _guess_sequence_indent(lines: list[tuple[int, str]]) -> tuple[int, int]:
    """Return how the lines indent a block sequence under its key.

    The first sequence decides: its first dash gives the offset, and its
    first item with content on the dash line gives the indent, looking at
    most :data:`_SEQUENCE_LOOKAHEAD` lines past the key.

    Returns:
        The column of an item's content and of its dash, both counted from
        the key's column; ``ruamel.yaml``'s defaults when no item tells. The
        content always lies at least two columns past the dash.
    """
    for index, (depth, content) in enumerate(lines[:-1]):
        column, key = _key_column(depth, content)
        dash_depth, item = lines[index + 1]
        offset = dash_depth - column
        if not _opens_block(key) or _dash_rest(item) is None or offset < 0:
            continue
        for later_depth, later in lines[index + 1 : index + 1 + _SEQUENCE_LOOKAHEAD]:
            rest = _dash_rest(later)
            if later_depth < dash_depth or rest is None and later_depth == dash_depth:
                break
            if later_depth == dash_depth and rest:
                return offset + len(later) - len(rest), offset
    return _DEFAULT_SEQUENCE_INDENT, _DEFAULT_SEQUENCE_OFFSET


def _dash_rest(content: str) -> str | None:
    """Return what follows a sequence item's dash, or ``None`` for no dash."""
    if content != "-" and not content.startswith("- "):
        return None
    return content[1:].lstrip(" ")
