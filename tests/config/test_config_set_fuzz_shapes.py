"""``config set`` over generated files of the seventh review's shapes (#429, #451).

Each seed makes one file of one shape, with filler entries and comments
from :mod:`tests.config.test_config_set_fuzz`, and one edit: block scalars
whose header holds a comment and no content; one-entry block lists and
mappings, with and without an inline comment, replaced by one-entry
values; flow mappings whose last value is empty, on one line and across
lines; anchors and tags on a line of their own above a block sequence or
mapping; ``@json`` cast strings of any depth; and flat keys of every depth
up to the limit and past it.

The oracle knows from the shape whether the edit is accepted, and why it
is refused otherwise. An accepted edit must give the key its value, leave
every other setting and every comment as it was, each comment belonging
to the same key path (:mod:`tests.config.fuzz_oracle`), and be a no-op
when made again; a refused edit must leave the file as it was.
``WINNOW_FUZZ_SEEDS`` sets how many seeds the run covers.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import cast

from assertpy import assert_that

from tests.config.fuzz_oracle import comment_owners
from tests.config.test_config_set_fuzz import (
    Shape,
    _comments,
    _Generator,
    _load,
    _Picker,
    _Style,
    _without,
)
from winnow.config import set_config_value
from winnow.config._shape import MAX_DEPTH
from winnow.exceptions import ConfigError

_SEEDS = range(int(os.environ.get("WINNOW_FUZZ_SEEDS", "140")))
_COMMENT_REFUSAL = "a comment inside its old value"
_INDENT_REFUSAL = "at the file's indentation"
_LOAD_REFUSAL = "Unable to load Winnow configuration"


class Kind(StrEnum):
    """The shape a seed's file takes."""

    HEADER_ONLY = auto()
    ONE_LIST = auto()
    ONE_MAPPING = auto()
    FLOW_EMPTY_LAST = auto()
    PROPERTY_LINE = auto()
    CAST = auto()
    FLAT_KEY = auto()


_KINDS = list(Kind)


@dataclass(frozen=True, slots=True)
class Case:
    """One generated file, its edit and the outcome the shape expects."""

    kind: Kind
    text: str
    key: str
    value: object
    refusal: str | None
    """A text the refusal must hold, or ``None`` when the edit is accepted."""


Section = tuple[list[str], str, object, str | None]
"""An edited section's lines, the key, the value and the refusal expected."""


def _block_section(kind: Kind, rng: _Picker) -> Section:
    """Return a header-only block scalar or a one-entry block collection."""
    comment = rng.pick(["", "  # note", " # x: y"])
    refusal = _COMMENT_REFUSAL if comment else None
    pad = rng.pick(["  ", "    "])
    if kind is Kind.HEADER_ONLY:
        header = rng.pick(["|", ">", "|-", ">+", "|2"])
        lines = [
            "paths:",
            f"{pad}output_dir: {header}{comment}",
            f"{pad}quarantine_dir: /q",
        ]
        return lines, "paths.output_dir", "/new", refusal
    if kind is Kind.ONE_LIST:
        dash = rng.pick(["", "  "])
        return ["source_dirs:", f"{dash}- /a{comment}"], "source_dirs", ["/b"], refusal
    lines = ["cache:", f"{pad}enabled: true{comment}"]
    return lines, "cache", {"directory": "/d"}, refusal


def _flow_empty_last(rng: _Picker) -> Section:
    """Return a flow mapping whose last value is empty, on one or more lines."""
    body = rng.pick(
        [
            "{output_dir: }",
            "{output_dir:   }",
            "{include_patterns: [], output_dir: }",
            "{output_dir:\n  }",
            "{output_dir:   \n  }",
            "{\n  output_dir: ,\n}",
            "{output_dir: ,}",
        ],
    )
    return [f"paths: {body}"], "paths.quarantine_dir", "/q", None


def _cast(rng: _Picker) -> Section:
    """Return a ``@json`` cast string of any depth, up to past the limit."""
    depth = 1 + rng.below(MAX_DEPTH + 20)
    inner = (
        "[" * depth + "]" * depth
        if rng.chance(2)
        else '{"a": ' * depth + "1" + "}" * depth
    )
    refusal = _LOAD_REFUSAL if depth + 1 > MAX_DEPTH else None
    return [f"cast_value: '@json {inner}'"], "workers", 3, refusal


def _flat_key(rng: _Picker) -> Section:
    """Return a flat key of any depth, up to past the limit."""
    parts = 1 + rng.below(MAX_DEPTH + 5)
    flat = ".".join(["flat", *(["a"] * (parts - 1))])
    refusal = _LOAD_REFUSAL if parts > MAX_DEPTH else None
    return [f"{flat}: 1"], "workers", 3, refusal


def _section(kind: Kind, rng: _Picker) -> Section:
    """Return the edited section of a shape."""
    if kind in {Kind.HEADER_ONLY, Kind.ONE_LIST, Kind.ONE_MAPPING}:
        return _block_section(kind, rng)
    makers = {
        Kind.FLOW_EMPTY_LAST: _flow_empty_last,
        Kind.PROPERTY_LINE: _property_line,
        Kind.CAST: _cast,
        Kind.FLAT_KEY: _flat_key,
    }
    return makers[kind](rng)


def _property_line(rng: _Picker) -> Section:
    """Return a block collection under an anchor or tag on a line of its own."""
    sequence = rng.chance(2)
    column = 2 * rng.below(2) if sequence else 2
    prop_column = max(column, 2) + 2 * rng.below(2)
    pad, prop_pad = " " * column, " " * prop_column
    if sequence:
        prop = rng.pick(["&s", "!!seq", "&s !!seq"])
        lines = ["source_dirs:", f"{prop_pad}{prop}", f"{pad}- /a", f"{pad}- /b"]
        value: object = ["/x", "/y"]
        key = "source_dirs"
    else:
        prop = rng.pick(["&c", "!!map", "&c !!map"])
        lines = ["cache:", f"{prop_pad}{prop}", f"{pad}enabled: true"]
        value = {"enabled": False, "directory": "/d"}
        key = "cache"
    refusal = None if prop_column == column else _INDENT_REFUSAL
    return lines, key, value, refusal


def generate(seed: int) -> Case:
    """Return a generated file of the seed's shape and its edit."""
    kind = _KINDS[seed % len(_KINDS)]
    rng = _Picker(seed + 1_000_000)
    style = _Style(mapping=2, offset=2, null="", true="true", gap=" ")
    filler = _Generator(rng, style, Shape.TOP_SCALAR)
    section, key, value, refusal = _section(kind, rng)
    lines: list[str] = []
    for index in range(1 + rng.below(3)):
        lines += filler.entry(0, f"f{index}")
    lines = [*section, *lines] if rng.chance(2) else [*lines, *section]
    if key == "workers":
        lines.append(f"workers: 2{filler.comment()}")
    lines.append(rng.pick(["# end", "", "next_key: 1"]))
    text = "\n".join(lines) + "\n"
    return Case(kind=kind, text=text, key=key, value=value, refusal=refusal)


def _value_at(settings: dict[str, object], key: str) -> object:
    """Return the value at a dotted key of loaded settings."""
    found: object = settings
    for part in key.split("."):
        assert_that(found).is_instance_of(dict)
        found = cast("dict[str, object]", found)[part]
    return found


def check(case: Case, directory: Path) -> str | None:
    """Make a case's edit and check it with the oracle.

    Returns:
        ``None`` when the edit was made, or the refusal's message.
    """
    config_path = directory / "c.yaml"
    config_path.write_text(case.text, encoding="utf-8")
    try:
        set_config_value(case.key, case.value, config_path=config_path)
    except ConfigError as error:
        assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(case.text)
        assert_that(case.refusal).described_as(str(error)).is_not_none()
        assert_that(str(error)).contains(str(case.refusal))
        return error.message
    assert_that(case.refusal).described_as("accepted").is_none()
    after = config_path.read_text(encoding="utf-8")
    old_settings, new_settings = _load(case.text), _load(after)
    assert_that(_value_at(new_settings, case.key)).is_equal_to(case.value)
    assert_that(_without(new_settings, case.key)).is_equal_to(
        _without(old_settings, case.key),
    )
    assert_that(_comments(after)).is_equal_to(_comments(case.text))
    assert_that(comment_owners(after)).is_equal_to(comment_owners(case.text))
    set_config_value(case.key, case.value, config_path=config_path)
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(after)
    return None


def run(seeds: range, directory: Path) -> dict[Kind, Counter[str]]:
    """Check every seed and count the outcomes per shape."""
    outcomes: dict[Kind, Counter[str]] = {kind: Counter() for kind in Kind}
    for seed in seeds:
        case = generate(seed)
        seed_directory = directory / str(seed)
        seed_directory.mkdir()
        try:
            refusal = check(case, seed_directory)
        except AssertionError as error:
            message = f"seed {seed} ({case.kind}): {case.text!r}\n{error}"
            raise AssertionError(message) from error
        outcomes[case.kind][refusal or "accepted"] += 1
    return outcomes


def test_review_shapes_set_one_key_or_refuse_as_expected(tmp_path: Path) -> None:
    """Every shape is accepted or refused exactly as its rule says."""
    outcomes = run(_SEEDS, tmp_path)

    for kind, counts in outcomes.items():
        assert_that(counts["accepted"]).described_as(
            f"{kind}: {dict(counts)}",
        ).is_greater_than(0)
