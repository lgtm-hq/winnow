"""A YAML file's nodes with the place each one takes in the file's text.

``config set`` edits a YAML file by replacing the text of one value and
leaving every other character as it was (#429). This module reads the text
with ``ruamel.yaml``'s parser and scanner and gives each node the range of
characters it spans, with the positions of the file's comments, anchors and
aliases, so the editor can find the text to replace and check what that text
holds.
"""

from __future__ import annotations

import bisect
import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.events import (
    AliasEvent,
    CollectionStartEvent,
    DocumentEndEvent,
    DocumentStartEvent,
    Event,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
)
from ruamel.yaml.tokens import (
    AnchorToken,
    CommentToken,
    ScalarToken,
    TagToken,
    Token,
    ValueToken,
)

LINE_BREAKS: Final = "\n\x85  "
"""The characters that end a line for the YAML parser, once CRLF is gone."""
_NULL_SPELLINGS: Final = frozenset({"", "~", "null", "Null", "NULL"})
_NULL_TAG: Final = "tag:yaml.org,2002:null"
_BLOCK_STYLES: Final = frozenset({"|", ">"})
_BLOCK_INDICATORS: Final = "+-0123456789"
_SPACE: Final = " \t"


class NodeKind(StrEnum):
    """What a YAML node is."""

    SCALAR = auto()
    MAPPING = auto()
    SEQUENCE = auto()
    ALIAS = auto()


@dataclass(eq=False, slots=True)
class YamlNode:
    """One node of a YAML document and the characters it spans."""

    kind: NodeKind
    start: int
    """Where the node starts, its anchor and tag included."""
    end: int
    """Where the parser ends the node; a block collection ends after the
    comments that follow it, so the editor uses :meth:`YamlTree.leaf_end`."""
    anchor: str | None = None
    tag: str | None = None
    value: str = ""
    """A scalar's value, or the anchor name an alias refers to."""
    style: str | None = None
    """A scalar's style: ``None`` for plain, else its quote or block indicator."""
    flow: bool = False
    """Whether a collection is written in flow style."""
    keep: bool = False
    """Whether a block scalar keeps its final line breaks (``+`` chomping)."""
    target: YamlNode | None = None
    """The node an alias refers to."""
    pairs: list[YamlPair] = field(default_factory=list)
    items: list[YamlNode] = field(default_factory=list)

    @property
    def is_null(self) -> bool:
        """Return whether the node is a null the reader reads as ``None``."""
        if self.kind is not NodeKind.SCALAR:
            return False
        if self.tag == _NULL_TAG:
            return True
        return self.tag is None and self.style is None and self.value in _NULL_SPELLINGS

    @property
    def is_empty(self) -> bool:
        """Return whether the node is an empty plain scalar with no anchor or tag."""
        return (
            self.kind is NodeKind.SCALAR
            and self.start == self.end
            and self.style is None
            and not self.value
        )

    def resolved(self) -> YamlNode:
        """Return the node an alias refers to, or the node itself."""
        return self.target if self.target is not None else self


@dataclass(eq=False, slots=True)
class YamlPair:
    """A mapping entry: its key, its value and where its ``:`` ends."""

    key: YamlNode
    value: YamlNode
    colon_end: int
    """The position right after the entry's ``:``; the key's end when the
    entry has no ``:``."""

    @property
    def name(self) -> str | None:
        """Return the key's text, through an alias; ``None`` if not a scalar."""
        key = self.key.resolved()
        return key.value if key.kind is NodeKind.SCALAR else None


@dataclass(slots=True)
class YamlTree:
    """A parsed YAML file and the positions of what it holds."""

    text: str
    root: YamlNode | None
    documents: int
    comments: list[int]
    """Where each comment's ``#`` is, in order."""
    aliases: list[tuple[str, int]]
    """Each alias's anchor name and position."""
    anchored: list[YamlNode]
    """Every node that carries an anchor, keys included."""
    document_end: int | None
    """Where an explicit ``...`` document end starts, if there is one."""
    line_starts: list[int]

    def line_of(self, position: int) -> int:
        """Return the zero-based line a position lies on."""
        return bisect.bisect_right(self.line_starts, position) - 1

    def column_of(self, position: int) -> int:
        """Return a position's column on its line."""
        return position - self.line_starts[self.line_of(position)]

    def line_end(self, position: int) -> int:
        """Return where the line holding ``position`` ends, before its break."""
        index = self.line_of(position)
        if index + 1 < len(self.line_starts):
            return self.line_starts[index + 1] - 1
        return len(self.text)

    def next_line(self, position: int) -> int:
        """Return where the line after the one holding ``position`` starts."""
        index = self.line_of(position)
        if index + 1 < len(self.line_starts):
            return self.line_starts[index + 1]
        return len(self.text)

    def comments_in(self, start: int, end: int) -> list[int]:
        """Return the comments starting in ``[start, end)``."""
        low = bisect.bisect_left(self.comments, start)
        high = bisect.bisect_left(self.comments, end)
        return self.comments[low:high]

    def comment_line(self, position: int) -> bool:
        """Return whether the line starting at ``position`` holds only a comment."""
        end = self.line_end(position)
        content = self.text[position:end].lstrip(_SPACE)
        start = end - len(content)
        return bool(content) and bool(self.comments_in(start, start + 1))

    def leaf_end(self, node: YamlNode, *, kept_breaks: bool = False) -> int:
        """Return where a value's text ends: the end of its last scalar.

        A block collection ends where its last value ends, not after the
        comments that follow it. A scalar ends at its last character that
        is not a space or line break, at the end of that line; an empty
        value ends where it starts.

        Args:
            node: The value.
            kept_breaks: Whether a block scalar with ``+`` chomping ends
                after the line breaks it keeps, which belong to its value.
        """
        last = node
        while not last.flow and (last.pairs or last.items):
            last = last.pairs[-1].value if last.pairs else last.items[-1]
        if last.kind is not NodeKind.SCALAR or last.start == last.end:
            return last.end
        if kept_breaks and last.keep:
            return last.end
        text = self.text[last.start : last.end]
        content = len(text.rstrip(_SPACE + LINE_BREAKS))
        return min(last.end, self.line_end(last.start + max(content - 1, 0)))

    def nodes(self) -> Iterator[YamlNode]:
        """Yield every node of the document once, keys included."""
        pending = [] if self.root is None else [self.root]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(
                item for pair in node.pairs for item in (pair.key, pair.value)
            )
            pending.extend(node.items)


def parse_yaml(text: str) -> YamlTree:
    """Parse YAML text into nodes that know their place in it.

    Args:
        text: The content, ``\\n`` line ends and no byte order mark.

    Returns:
        The first document's root node, the number of documents, and the
        positions of comments, anchors and aliases.

    Raises:
        ruamel.yaml.YAMLError: If the text is not valid YAML.
    """
    yaml = YAML()
    tokens = list(yaml.scan(io.StringIO(text)))
    events = list(yaml.parse(io.StringIO(text)))
    line_starts = [0] + [
        index + 1 for index, char in enumerate(text) if char in LINE_BREAKS
    ]
    builder = _Builder(text, tokens)
    for event in events:
        builder.add(event)
    return YamlTree(
        text=text,
        root=builder.root,
        documents=builder.documents,
        comments=_comment_positions(text, tokens),
        aliases=builder.aliases,
        anchored=builder.anchored,
        document_end=builder.document_end,
        line_starts=line_starts,
    )


class _Builder:
    """Builds nodes from parser events, with positions from the scanner."""

    def __init__(self, text: str, tokens: list[Token]) -> None:
        self._text = text
        self._stack: list[tuple[YamlNode, list[YamlNode]]] = []
        self._anchors: dict[str, YamlNode] = {}
        self._properties: dict[int, int] = {
            token.end_mark.index: token.start_mark.index
            for token in tokens
            if isinstance(token, AnchorToken | TagToken)
        }
        self._values: list[tuple[int, int]] = sorted(
            (token.start_mark.index, token.end_mark.index)
            for token in tokens
            if isinstance(token, ValueToken)
        )
        self._ends: list[int] = sorted(
            token.end_mark.index
            for token in tokens
            if token.end_mark.index > token.start_mark.index
        )
        self._block_scalars = {
            token.end_mark.index: token.start_mark.index
            for token in tokens
            if isinstance(token, ScalarToken) and token.style in _BLOCK_STYLES
        }
        self.root: YamlNode | None = None
        self.documents = 0
        self.document_end: int | None = None
        self.aliases: list[tuple[str, int]] = []
        self.anchored: list[YamlNode] = []

    def add(self, event: Event) -> None:
        """Add one parser event to the tree."""
        if isinstance(event, DocumentStartEvent):
            self.documents += 1
        elif isinstance(event, DocumentEndEvent):
            if event.explicit and self.document_end is None:
                self.document_end = event.start_mark.index
        elif isinstance(event, MappingEndEvent | SequenceEndEvent):
            node, _ = self._stack.pop()
            node.end = event.end_mark.index
        elif isinstance(event, ScalarEvent | AliasEvent | CollectionStartEvent):
            node = self._node(event)
            if self.documents == 1:
                self._attach(node)
            if isinstance(event, CollectionStartEvent):
                self._stack.append((node, []))

    def _node(self, event: Event) -> YamlNode:
        """Return the node an event opens or holds."""
        start = self._start(event.start_mark.index)
        end = event.end_mark.index
        anchor = getattr(event, "anchor", None)
        if isinstance(event, AliasEvent):
            self.aliases.append((str(anchor), start))
            return YamlNode(
                NodeKind.ALIAS,
                start,
                end,
                value=str(anchor),
                target=self._anchors.get(str(anchor)),
            )
        tag = getattr(event, "tag", None)
        node = YamlNode(NodeKind.SCALAR, start, end, anchor=anchor, tag=tag)
        if isinstance(event, ScalarEvent):
            node.value = event.value
            node.style = event.style
            header = self._block_scalars.get(end)
            if header is not None:
                node.keep = "+" in self._header(header)
        elif isinstance(event, CollectionStartEvent):
            node.kind = (
                NodeKind.MAPPING
                if isinstance(event, MappingStartEvent)
                else NodeKind.SEQUENCE
            )
            node.flow = bool(event.flow_style)
        if anchor is not None:
            self._anchors[anchor] = node
            self.anchored.append(node)
        return node

    def _start(self, position: int) -> int:
        """Return where a node starts, counting the anchor and tag before it."""
        while True:
            before = len(self._text[:position].rstrip(_SPACE + LINE_BREAKS))
            earlier = self._properties.get(before)
            if earlier is None or earlier >= position:
                return position
            position = earlier

    def _header(self, start: int) -> str:
        """Return a block scalar header's indicators."""
        end = start + 1
        while end < len(self._text) and self._text[end] in _BLOCK_INDICATORS:
            end += 1
        return self._text[start + 1 : end]

    def _attach(self, node: YamlNode) -> None:
        """Add a node to the collection being built, or make it the root."""
        if not self._stack:
            if node.is_empty:
                node.start = node.end = self._last_end(node.start)
            self.root = node
            return
        parent, pending = self._stack[-1]
        if parent.kind is NodeKind.SEQUENCE:
            if node.is_empty:
                node.start = node.end = self._last_end(node.start)
            parent.items.append(node)
        elif not pending:
            pending.append(node)
        else:
            key = pending.pop()
            colon_end = self._colon_end(key.end, node.start)
            if node.is_empty:
                node.start = node.end = colon_end
            parent.pairs.append(YamlPair(key, node, colon_end))

    def _colon_end(self, key_end: int, value_start: int) -> int:
        """Return where the ``:`` between a key and its value ends."""
        index = bisect.bisect_left(self._values, (key_end, key_end))
        if index < len(self._values):
            start, end = self._values[index]
            if start < value_start:
                return end
        return key_end

    def _last_end(self, position: int) -> int:
        """Return where the last token before ``position`` ends."""
        index = bisect.bisect_right(self._ends, position)
        return self._ends[index - 1] if index else 0


def _comment_positions(text: str, tokens: list[Token]) -> list[int]:
    """Return where each comment of the text starts, from the scanner's tokens.

    ``ruamel.yaml``'s scanner keeps comments as comment tokens attached to
    other tokens, several lines to a token; a comment after a block
    scalar's header is read the way the scanner reads that header.
    """
    found: set[int] = set()
    for token in tokens:
        for comment in _comment_tokens(getattr(token, "_comment", None)):
            found.update(_comment_lines(text, comment))
        if isinstance(token, ScalarToken) and token.style in _BLOCK_STYLES:
            position = token.start_mark.index + 1
            while position < len(text) and text[position] in _BLOCK_INDICATORS:
                position += 1
            while position < len(text) and text[position] in _SPACE:
                position += 1
            if position < len(text) and text[position] == "#":
                found.add(position)
    return sorted(found)


def _comment_tokens(attached: object) -> Iterator[CommentToken]:
    """Yield the comment tokens a token carries, however they are nested."""
    if isinstance(attached, CommentToken):
        yield attached
    elif isinstance(attached, list):
        for item in attached:
            yield from _comment_tokens(item)


def _comment_lines(text: str, comment: CommentToken) -> Iterator[int]:
    """Yield where each comment line of a comment token starts.

    The token's value is the file's text from its first comment on, with
    the blank lines and comment lines after it, and perhaps the line breaks
    before it. The token's mark lies on the first comment's line or on a
    blank line above it, so the value's text is found from there on.

    Raises:
        ValueError: If the value's text is not found, so the comment cannot
            be placed.
    """
    core = comment.value.lstrip("\n").rstrip("\n")
    indent = len(core) - len(core.lstrip(_SPACE))
    found = text.find(core, max(comment.start_mark.index - indent, 0))
    if not core.strip() or found < 0:
        if core.strip():
            message = f"cannot place the comment {core.strip()!r}"
            raise ValueError(message)
        return
    offset = found
    for piece in core.split("\n"):
        content = piece.lstrip(_SPACE)
        if content.startswith("#"):
            yield offset + len(piece) - len(content)
        offset += len(piece) + 1
