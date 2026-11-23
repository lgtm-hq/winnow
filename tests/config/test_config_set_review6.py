"""``config set`` sets one key or refuses, and never crashes (#429, #451).

The sixth review round's findings, each as a test: deep and recursive
files and values, anchors and tags given to the wrong node, comments in
flow mappings, trailing commas, explicit ``?`` keys, whitespace-only block
scalars, a splice range check that can fail, the message for an edit that
does not parse, and how often a call parses the file.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from assertpy import assert_that
from click.testing import CliRunner
from ruamel.yaml import YAML
from ruamel.yaml.events import Event

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import _files, _writer, _yaml_splice, load_config, set_config_value
from winnow.config._shape import MAX_DEPTH, nesting_depth
from winnow.config._yaml_splice import Splice, plan_edit
from winnow.config._yaml_tree import YamlTree, _Builder, parse_yaml
from winnow.exceptions import ConfigError

_DEEP = "levels deep"
_CYCLE = "contains itself"
_IRREGULAR = "explicit '?' key or a key without ':'"


def _edit(tmp_path: Path, original: str, key: str, value: object) -> str:
    """Set a key in a file holding ``original`` and return the file's text."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")
    set_config_value(key, value, config_path=config_path)
    return config_path.read_text(encoding="utf-8")


def _refusal(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
    *,
    name: str = "c.yaml",
) -> str:
    """Return the message a refused set gives, checking the file is untouched."""
    config_path = tmp_path / name
    config_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        set_config_value(key, value, config_path=config_path)
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(original)
    return str(error.value)


_DEEP_FILES = [
    pytest.param("{" * 1000 + "a: 1" + "}" * 1000 + "\n", "c.yaml", id="flow"),
    pytest.param(
        "".join(f"{' ' * 2 * level}k{level}:\n" for level in range(500))
        + " " * 1000
        + "x: 1\n",
        "c.yaml",
        id="block",
    ),
    pytest.param('{"a": ' * 1000 + "1" + "}" * 1000, "c.json", id="json"),
    pytest.param('{"a": ' * 100_000 + "1" + "}" * 100_000, "c.json", id="json-c"),
]


@pytest.mark.parametrize(("original", "name"), _DEEP_FILES)
def test_deeply_nested_file_is_a_config_error(
    tmp_path: Path,
    original: str,
    name: str,
) -> None:
    """Blocker 1: a file nested too deeply is refused, naming the file."""
    message = _refusal(tmp_path, original, "workers", 2, name=name)

    assert_that(message).contains(_DEEP).contains(str(tmp_path / name))
    with pytest.raises(ConfigError, match=_DEEP):
        load_config(tmp_path / name, load_env=False)


@pytest.mark.parametrize(
    "original",
    [
        pytest.param("x: &r {self: *r}\n", id="mapping"),
        pytest.param("x: &r [*r]\n", id="list"),
        pytest.param("x: &r\n  a:\n    - *r\n", id="block"),
    ],
)
def test_recursive_anchor_is_a_config_error(tmp_path: Path, original: str) -> None:
    """Blocker 1 and #451: a self-referencing anchor is a ``ConfigError``."""
    message = _refusal(tmp_path, original, "workers", 2)

    assert_that(message).contains(_CYCLE).contains(str(tmp_path / "c.yaml"))
    with pytest.raises(ConfigError, match=_CYCLE):
        load_config(tmp_path / "c.yaml", load_env=False)


def test_config_show_on_a_recursive_anchor_gives_the_error_panel(
    tmp_path: Path,
) -> None:
    """#451: ``winnow config show`` exits with the normal error, not a crash."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("x: &r {self: *r}\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["config", "show", "--config", str(config_path)])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.exception).is_instance_of(SystemExit)
    assert_that(result.stderr).contains(_CYCLE).contains("operation: load_config")
    assert_that(result.stdout).is_empty()


def test_file_at_the_depth_limit_loads(tmp_path: Path) -> None:
    """A file exactly ``MAX_DEPTH`` collections deep is read."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(
        "x: " + "[" * (MAX_DEPTH - 1) + "]" * (MAX_DEPTH - 1) + "\n",
        encoding="utf-8",
    )

    set_config_value("workers", 2, config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).starts_with("x: [[")


def _cyclic_list() -> list[object]:
    """Return a list that contains itself."""
    value: list[object] = []
    value.append(value)
    return value


def _cyclic_mapping() -> dict[str, object]:
    """Return a mapping that contains itself."""
    value: dict[str, object] = {}
    value["self"] = value
    return value


def _deep_value(depth: int) -> object:
    """Return ``depth`` nested lists."""
    value: object = []
    for _ in range(depth - 1):
        value = [value]
    return value


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        pytest.param(_cyclic_list(), _CYCLE, id="cyclic-list"),
        pytest.param(_cyclic_mapping(), _CYCLE, id="cyclic-mapping"),
        pytest.param(_deep_value(2000), _DEEP, id="deep"),
        pytest.param(_deep_value(MAX_DEPTH - 1), _DEEP, id="deep-with-key"),
    ],
)
def test_cyclic_or_deep_value_is_a_config_error(
    tmp_path: Path,
    value: object,
    reason: str,
) -> None:
    """Blocker 1: a value that contains itself or nests too deeply is refused."""
    message = _refusal(tmp_path, "workers: 1\n", "paths.include_patterns", value)

    assert_that(message).contains(reason)


def test_nesting_depth_counts_a_shared_value_once() -> None:
    """A value reached twice is not a cycle, and scalars are 0 deep."""
    shared = [[1]]

    assert_that(nesting_depth({"a": shared, "b": (shared,)})).is_equal_to(4)
    assert_that(nesting_depth("text")).is_equal_to(0)
    assert_that(nesting_depth({frozenset({1})})).is_equal_to(2)


@pytest.mark.parametrize(
    ("original", "key", "value", "expected"),
    [
        pytest.param(
            "routing: &r\n  review: Old\n",
            "routing.enabled",
            True,
            "routing: &r\n  review: Old\n  enabled: true\n",
            id="anchored-mapping",
        ),
        pytest.param(
            "routing: !!map\n  review: Old\n",
            "routing.enabled",
            True,
            "routing: !!map\n  review: Old\n  enabled: true\n",
            id="tagged-mapping",
        ),
        pytest.param(
            "defaults: &base\n  enabled: true\ncache: *base\nworkers: 4\n",
            "organize.max_depth",
            5,
            "defaults: &base\n  enabled: true\ncache: *base\nworkers: 4\n"
            "organize:\n  max_depth: 5\n",
            id="file-indent",
        ),
        pytest.param(
            "paths: &p\n  include_patterns: null\n",
            "paths.include_patterns",
            ["*.jpg"],
            "paths: &p\n  include_patterns:\n    - '*.jpg'\n",
            id="anchored-parent",
        ),
        pytest.param(
            "paths:\n  output_dir: !!null\n  include_patterns: null\n",
            "paths.include_patterns",
            ["*.jpg"],
            "paths:\n  output_dir: !!null\n  include_patterns:\n    - '*.jpg'\n",
            id="after-tagged-null",
        ),
    ],
)
def test_anchor_or_tag_belongs_to_its_own_node(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
    expected: str,
) -> None:
    """Blocker 2: an anchor or tag never moves the start of a later node."""
    assert_that(_edit(tmp_path, original, key, value)).is_equal_to(expected)


def test_a_property_token_starts_one_node_only() -> None:
    """Blocker 2: a node is moved back to an anchor once, never twice."""
    text = "k: &a v\n"
    builder = _Builder(text, list(YAML().scan(io.StringIO(text))))
    event = cast(
        "Event",
        SimpleNamespace(
            start_mark=SimpleNamespace(index=6),
            end_mark=SimpleNamespace(index=7),
        ),
    )

    first = builder._start(event, has_properties=True)
    second = builder._start(event, has_properties=True)

    assert_that([first, second]).is_equal_to([3, 6])


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        ("routing:\n  review: Old\n", "routing.enabled", True),
        ("paths:\n  include_patterns: null\n", "paths.include_patterns", ["*.jpg"]),
        ("source_dirs:\n  - /a\n", "source_dirs", ["/b", "/c"]),
    ],
)
def test_lines_off_the_parser_column_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original: str,
    key: str,
    value: object,
) -> None:
    """Blocker 2: new lines that would not start at the file's column refuse."""
    real = YamlTree.column_of
    monkeypatch.setattr(
        YamlTree,
        "column_of",
        lambda tree, position: real(tree, position) + 1,
    )

    assert_that(_refusal(tmp_path, original, key, value)).contains(
        "at the file's indentation",
    )


def test_key_added_to_a_flow_mapping_with_a_comment_is_refused(
    tmp_path: Path,
) -> None:
    """Blocker 3: a comment in a flow mapping would follow the new key."""
    message = _refusal(
        tmp_path,
        "paths: {output_dir: /o # output\n  }\n",
        "paths.quarantine_dir",
        "/q",
    )

    assert_that(message).contains("holds a comment")


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("routing: {review:,}\n", "routing: {review:, enabled: true}\n"),
        ("routing: {review: Old,}\n", "routing: {review: Old, enabled: true}\n"),
        ("routing: {review: ,}\n", "routing: {review: , enabled: true}\n"),
        ("routing: {review: Old , }\n", "routing: {review: Old , enabled: true }\n"),
        ("routing: {review: Old}\n", "routing: {review: Old, enabled: true}\n"),
    ],
)
def test_entry_goes_after_a_trailing_comma(original: str, expected: str) -> None:
    """Finding 4: a new flow entry reuses a trailing comma, never doubles it."""
    after = plan_edit(original, ["routing", "enabled"], True).apply(original)

    assert_that(after).is_equal_to(expected)


def test_trailing_comma_edit_is_written(tmp_path: Path) -> None:
    """Finding 4: the edit after a trailing comma reads back and is written."""
    after = _edit(tmp_path, "routing: {review: Old,}\n", "routing.enabled", True)

    assert_that(after).is_equal_to("routing: {review: Old, enabled: true}\n")


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        ("? workers\n: 4\n", "dry_run", True),
        ("? workers\n: 4\n", "workers", 5),
        ("routing:\n  ? review\n  : Old\n", "routing.enabled", True),
        ("routing:\n  ? review\n", "routing.review", "New"),
        ("routing:\n  review: Old\n  ? graphics\n  : Art\n", "routing", {"a": "b"}),
    ],
)
def test_explicit_key_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """Finding 5: a mapping with a ``?`` key or no ``:`` is not edited."""
    assert_that(_refusal(tmp_path, original, key, value)).contains(_IRREGULAR)


def test_whitespace_only_block_scalar_is_replaced_whole(tmp_path: Path) -> None:
    """Finding 6: a block scalar holding only spaces leaves no orphaned line."""
    after = _edit(
        tmp_path,
        "paths:\n  output_dir: |2\n     \nworkers: 1\n",
        "paths.output_dir",
        "/x",
    )

    assert_that(after).is_equal_to("paths:\n  output_dir: /x\nworkers: 1\n")


def test_comment_after_a_key_above_its_block_list_is_kept(tmp_path: Path) -> None:
    """Fuzz: a comment the parser moves onto the list's event is still seen."""
    message = _refusal(
        tmp_path,
        "workers: 2\nsource_dirs:  # note\n  - /photos\n",
        "source_dirs",
        [],
    )

    assert_that(message).contains("a comment inside its old value")


@pytest.mark.parametrize(("start", "end"), [(3, 1), (-1, 0), (0, 99)])
def test_splice_outside_the_text_is_refused(start: int, end: int) -> None:
    """Finding 8: the splice's own check can fail."""
    with pytest.raises(ConfigError, match="could not limit its edit"):
        Splice(start, end, "x").apply("abcd")


def test_edit_that_does_not_parse_is_reported_as_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 9: new text that does not parse is the edit's fault."""
    monkeypatch.setattr(
        _writer,
        "plan_edit",
        lambda *_args: Splice(0, 0, "[\n"),
    )

    message = _refusal(tmp_path, "workers: 1\n", "workers", 2)

    assert_that(message).contains("could not make this edit safely")
    assert_that(message).does_not_contain("Unable to load")


def test_second_update_that_does_not_parse_is_reported_as_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 9: a paired key's splice on broken text is the edit's fault."""
    real = plan_edit

    def broken(text: str, parts: list[str], value: object) -> Splice:
        if parts == ["follow_symlinks"]:
            return Splice(0, 0, "[\n")
        return real(text, parts, value)

    monkeypatch.setattr(_writer, "plan_edit", broken)

    message = _refusal(
        tmp_path,
        "symlink_policy: skip\nfollow_symlinks: false\n",
        "follow_symlinks",
        True,
    )

    assert_that(message).contains("could not make this edit safely")


def test_each_text_is_parsed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 10: one call reads the old and the new text once each."""
    calls: list[str] = []
    real_parse = _files.parse_document
    real_tree = parse_yaml

    def counting_parse(text: str, path: Path) -> object:
        calls.append("document")
        return real_parse(text, path)

    def counting_tree(text: str) -> YamlTree:
        calls.append("tree")
        return real_tree(text)

    monkeypatch.setattr(_writer, "parse_document", counting_parse)
    monkeypatch.setattr(_yaml_splice, "parse_yaml", counting_tree)

    _edit(tmp_path, "workers: 1\nrouting:\n  review: R\n", "workers", 2)

    assert_that(sorted(calls)).is_equal_to(["document", "document", "tree"])
