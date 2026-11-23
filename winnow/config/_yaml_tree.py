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
from ruamel.yaml.scanner import RoundTripScanner
from ruamel.yaml.tokens import (
    AnchorToken,
    BlockEntryToken,
    CommentToken,
    KeyToken,
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
    column: int
    """The column the parser gives the node's start, kept apart from
    ``start`` so a misplaced start cannot also misplace the column it is
    checked against."""
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
    irregular: bool = False
    """Whether a mapping has an explicit ``?`` key or a key with no ``:``."""
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

    @property
    def is_block_scalar(self) -> bool:
        """Return whether the node is a ``|`` or ``>`` block scalar."""
        return self.kind is NodeKind.SCALAR and self.style in _BLOCK_STYLES

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
    entries: list[tuple[int, int]]
    """Where each block sequence entry's ``-`` is, and its column, in order."""

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
        comments that follow it. A block scalar ends where the parser ends
        it, before the empty lines at its end, so a line holding only spaces
        inside it goes with it. Another scalar ends at its last character
        that is not a space or line break, at the end of that line; an empty
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
        if last.style in _BLOCK_STYLES:
            return last.start + len(text.rstrip(LINE_BREAKS))
        content = len(text.rstrip(_SPACE + LINE_BREAKS))
        return min(last.end, self.line_end(last.start + max(content - 1, 0)))

    def first_entry_column(self, sequence: YamlNode) -> int:
        """Return the column of a block sequence's first ``-``.

        The sequence's own start may lie at an anchor or tag on a line
        above its first entry; its first ``-`` is the first after that.
        """
        index = bisect.bisect_left(self.entries, (sequence.start, -1))
        if index < len(self.entries):
            return self.entries[index][1]
        return sequence.column

    def nodes(self, top: YamlNode | None = None) -> Iterator[YamlNode]:
        """Yield every node of the document, or below ``top``, keys included."""
        start = self.root if top is None else top
        pending = [] if start is None else [start]
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
    yaml.Scanner = _RecordingScanner
    events = list(yaml.parse(io.StringIO(text)))
    tokens: list[Token] = [token for token in yaml.scanner.taken if token]
    line_starts = [0] + [
        index + 1 for index, char in enumerate(text) if char in LINE_BREAKS
    ]
    builder = _Builder(text, tokens)
    for event in events:
        builder.add(event)
    entries: list[tuple[int, int]] = [
        (token.start_mark.index, token.start_mark.column)
        for token in tokens
        if isinstance(token, BlockEntryToken)
    ]
    return YamlTree(
        text=text,
        root=builder.root,
        documents=builder.documents,
        comments=_comment_positions(
            text,
            tokens,
            events,
            list(yaml.scanner.carried),
        ),
        aliases=builder.aliases,
        anchored=builder.anchored,
        document_end=builder.document_end,
        line_starts=line_starts,
        entries=entries,
    )


class _RecordingScanner(RoundTripScanner):
    """A scanner that keeps every token the parser takes from it.

    The tree needs the parser's events and the scanner's tokens; keeping
    the tokens as the parser reads them reads the text once, not twice.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.taken: list[Token | None] = []
        self.carried: list[object] = []
        super().__init__(*args, **kwargs)

    def get_token(self) -> Token | None:
        """Return the next token, keeping it and the comments it carries.

        The comments are kept as the token is taken: the parser can detach
        a comment from a token afterwards and then lose it, as it does for
        a comment after a key whose value on the next line starts with a tag.
        """
        token: Token | None = super().get_token()
        self.taken.append(token)
        self.carried.append(getattr(token, "_comment", None))
        return token


class _Builder:
    """Builds nodes from parser events, with positions from the scanner."""

    def __init__(self, text: str, tokens: list[Token]) -> None:
        self._text = text
        self._stack: list[tuple[YamlNode, list[YamlNode]]] = []
        self._anchors: dict[str, YamlNode] = {}
        properties: list[tuple[int, int]] = [
            (token.start_mark.index, token.end_mark.index)
            for token in tokens
            if isinstance(token, AnchorToken | TagToken)
        ]
        self._properties: dict[int, int] = {end: start for start, end in properties}
        self._property_starts: list[int] = sorted(start for start, _ in properties)
        self._claimed: set[int] = set()
        """The property tokens a node already starts at, so none has two."""
        self._explicit_keys: list[int] = sorted(
            token.start_mark.index
            for token in tokens
            if isinstance(token, KeyToken)
            and token.end_mark.index > token.start_mark.index
        )
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
        end: int = event.end_mark.index
        column: int = event.start_mark.column
        anchor = getattr(event, "anchor", None)
        if isinstance(event, AliasEvent):
            start: int = event.start_mark.index
            self.aliases.append((str(anchor), start))
            return YamlNode(
                NodeKind.ALIAS,
                start,
                end,
                column,
                value=str(anchor),
                target=self._anchors.get(str(anchor)),
            )
        tag = getattr(event, "tag", None)
        start = self._start(event, has_properties=anchor is not None or tag is not None)
        node = YamlNode(NodeKind.SCALAR, start, end, column, anchor=anchor, tag=tag)
        if isinstance(event, ScalarEvent):
            node.value = event.value
            node.style = event.style
            header = self._block_scalars.get(end)
            if header is not None:
                node.keep = "+" in self._header(header)
        else:
            node.kind = (
                NodeKind.MAPPING
                if isinstance(event, MappingStartEvent)
                else NodeKind.SEQUENCE
            )
            node.flow = bool(getattr(event, "flow_style", False))
        if anchor is not None:
            self._anchors[anchor] = node
            self.anchored.append(node)
        return node

    def _start(self, event: Event, *, has_properties: bool) -> int:
        """Return where a node starts, counting the anchor and tag before it.

        Only a node that has an anchor or a tag looks for them, and each
        anchor or tag token is given to one node: the first key inside an
        anchored mapping, or a key after a tagged empty value, never starts
        at the earlier node's anchor or tag.
        """
        position: int = event.start_mark.index
        if not has_properties:
            return position
        end: int = event.end_mark.index
        low = bisect.bisect_left(self._property_starts, position)
        high = bisect.bisect_left(self._property_starts, max(end, position + 1))
        self._claimed.update(self._property_starts[low:high])
        while True:
            before = position
            while before > 0 and self._text[before - 1] in _SPACE + LINE_BREAKS:
                before -= 1
            earlier = self._properties.get(before)
            if earlier is None or earlier >= position or earlier in self._claimed:
                return position
            self._claimed.add(earlier)
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
            if self._irregular_key(parent, key, colon_end):
                parent.irregular = True
            if node.is_empty:
                node.start = node.end = colon_end
            parent.pairs.append(YamlPair(key, node, colon_end))

    def _irregular_key(self, mapping: YamlNode, key: YamlNode, colon_end: int) -> bool:
        """Return whether a key is written ``? key`` or has no ``:`` after it.

        A plain key in a flow mapping whose text ends in ``:``, as
        ``{review:,}``, holds its ``:``: the parser reads the key as
        ``review:``, and the file reads it the same way after an edit.
        """
        previous = mapping.pairs[-1].value.end if mapping.pairs else mapping.start
        index = bisect.bisect_left(self._explicit_keys, previous)
        if index < len(self._explicit_keys) and self._explicit_keys[index] < key.start:
            return True
        if colon_end != key.end:
            return False
        return not (mapping.flow and key.style is None and key.value.endswith(":"))

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


def _comment_positions(
    text: str,
    tokens: list[Token],
    events: list[Event],
    carried: list[object],
) -> list[int]:
    """Return where each comment of the text starts, from tokens and events.

    ``ruamel.yaml``'s scanner keeps comments as comment tokens attached to
    other tokens, several lines to a token, and its parser moves some of
    them onto events, such as a comment after a key whose block sequence
    starts on the next line; a comment after a block scalar's header is
    read the way the scanner reads that header. ``carried`` holds what each
    token carried when the parser took it, since the parser drops some
    comments after that.
    """
    found: set[int] = set()
    attached = [getattr(token, "_comment", None) for token in tokens]
    attached += [getattr(event, "comment", None) for event in events]
    attached += carried
    for item in attached:
        for comment in _comment_tokens(item):
            found.update(_comment_lines(text, comment))
    for token in tokens:
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
