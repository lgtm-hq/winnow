"""``config set`` changes one key or refuses, and never crashes (#429, #451).

The seventh review round's findings, each as a test: the comment rule for
every kind of old and new value, the depth and cycle guard on what winnow
builds after parsing, a new key after an empty last value in a flow
mapping, a block sequence under an anchor on a line of its own, trailing
spaces after a replaced block, and where ``YamlNode.end`` is documented.
"""

from __future__ import annotations

import ast
import collections
import inspect
import io
import json
from pathlib import Path
from typing import cast

import pytest
from assertpy import assert_that
from click.testing import CliRunner
from ruamel.yaml import YAML

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import _writer, _yaml_tree, load_config, set_config_value
from winnow.config._editor import check_finite
from winnow.config._markers import cast_marker
from winnow.config._shape import MAX_DEPTH, children, nesting_depth, text_depth
from winnow.config._yaml_splice import Splice, plan_edit
from winnow.exceptions import ConfigError

_COMMENT = "  # note"
_COMMENT_ERROR = "a comment inside its old value"
_INDENT_ERROR = "at the file's indentation"
_UNSAFE = "could not make this edit safely"
_DEEP = "levels deep"


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


# Blocker 1: the comment rule, decided from the kinds of the old and new value.

_OLD_VALUES = {
    "plain": " a{c}",
    "quoted": ' "a"{c}',
    "flow": " [a, b]{c}",
    "block-scalar": " |{c}\n  text",
    "block-scalar-empty": " |{c}",
    "sequence-one": "\n  - a{c}",
    "sequence-several": "\n  - a\n  - b{c}",
    "mapping-one": "\n  x: 1{c}",
    "mapping-several": "\n  x: 1\n  y: 2{c}",
    "empty": "{c}",
}
"""Each kind of old value after ``key:``; ``{c}`` is where a comment goes."""
_INLINE_OLD = frozenset({"plain", "quoted", "flow", "empty"})
_NEW_VALUES: dict[str, object] = {
    "scalar": "/new",
    "empty-list": [],
    "empty-mapping": {},
    "list-one": ["/b"],
    "list-several": ["/b", "/c"],
    "mapping-one": {"d": "/d"},
    "mapping-several": {"d": "/d", "e": "/e"},
}
_INLINE_NEW = frozenset({"scalar", "empty-list", "empty-mapping"})


def _table() -> list[object]:
    """Return every old kind against every new kind, with and without a comment."""
    return [
        pytest.param(old, new, comment, id=f"{old}-to-{new}-{comment}")
        for old in _OLD_VALUES
        for new in _NEW_VALUES
        for comment in ("comment", "bare")
    ]


@pytest.mark.parametrize(("old", "new", "comment"), _table())
def test_comment_rule_for_every_kind_of_value(old: str, new: str, comment: str) -> None:
    """Blocker 1: a comment is kept on the key's line, or the edit is refused."""
    value_text = _OLD_VALUES[old].format(c=_COMMENT if comment == "comment" else "")
    text = f"key:{value_text}\nnext: 1\n"
    value = _NEW_VALUES[new]
    kept = comment == "bare" or (old in _INLINE_OLD and new in _INLINE_NEW)

    if not kept:
        with pytest.raises(ConfigError, match=_COMMENT_ERROR):
            plan_edit(text, ["key"], value)
        return
    after = plan_edit(text, ["key"], value).apply(text)

    loaded = YAML(typ="safe", pure=True).load(after)
    assert_that(loaded).is_equal_to({"key": value, "next": 1})
    if comment == "comment":
        assert_that(after.count("# note")).is_equal_to(1)
        key_line = next(line for line in after.splitlines() if line.startswith("key:"))
        assert_that(key_line).ends_with(_COMMENT)


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        pytest.param(
            "paths:\n  output_dir: | # header\nworkers: 1\n",
            "paths.output_dir",
            "/new",
            id="block-scalar-header",
        ),
        pytest.param(
            "source_dirs:\n  - /a  # about /a\n",
            "source_dirs",
            ["/b"],
            id="one-entry-list",
        ),
        pytest.param(
            "cache:\n  enabled: true  # about enabled\n",
            "cache",
            {"directory": "/d"},
            id="one-entry-mapping",
        ),
    ],
)
def test_comment_on_a_one_line_block_value_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """Blocker 1: the comment would be dropped or follow another item."""
    assert_that(_refusal(tmp_path, original, key, value)).contains(_COMMENT_ERROR)


def test_comment_after_an_inline_value_in_a_flow_mapping_is_kept() -> None:
    """A flow value on the key's line keeps the comment after it."""
    text = "routing: {review: Old,  # note\n  graphics: Art}\n"

    after = plan_edit(text, ["routing", "review"], "New").apply(text)

    assert_that(after).is_equal_to("routing: {review: New,  # note\n  graphics: Art}\n")


# Blocker 2: the depth and cycle guard covers what winnow builds after parsing.

_CAST_DEEP = 'x: "@json ' + "[" * 1500 + "]" * 1500 + '"\n'
_FLAT_KEY = "a." * 1500 + "a"
_DEEP_SOURCES = [
    pytest.param(_CAST_DEEP, "c.yaml", id="cast-string"),
    pytest.param('x: "@json ' + "[" * 100_000 + '"\n', "c.yaml", id="cast-json-c"),
    pytest.param(
        "x: '@json x " + '{"a": ' * 1500 + "1" + "}" * 1500 + "'\n",
        "c.yaml",
        id="cast-object",
    ),
    pytest.param('{"' + _FLAT_KEY + '": 1}', "c.json", id="json-flat-key"),
    pytest.param(
        f"? {_FLAT_KEY}\n: 1\n? {_FLAT_KEY}x\n: 2\n",
        "c.yaml",
        id="yaml-two-flat-keys",
    ),
    pytest.param(f'"{_FLAT_KEY}" = 1\n', "c.toml", id="toml-flat-key"),
]


@pytest.mark.parametrize(("original", "name"), _DEEP_SOURCES)
def test_structure_built_after_parsing_is_measured(
    tmp_path: Path,
    original: str,
    name: str,
) -> None:
    """Blocker 2: cast strings and flat keys are measured once expanded."""
    config_path = tmp_path / name
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigError) as loaded:
        load_config(config_path, load_env=False)
    result = CliRunner().invoke(main, ["config", "show", "--config", str(config_path)])

    assert_that(str(loaded.value)).contains("Unable to load Winnow configuration")
    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.exception).is_instance_of(SystemExit)
    if name != "c.toml":
        _refusal(tmp_path, original, "workers", 2, name=name)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("1+" * 100_000 + "1", id="binary-chain"),
        pytest.param("-" * 100_000 + "1", id="unary-chain"),
        pytest.param("not " * 10_000 + "1", id="not-chain"),
        pytest.param("[" * 100_000, id="unclosed"),
    ],
)
def test_cast_that_would_overflow_a_parser_is_a_value_error(text: str) -> None:
    """Blocker 2: ``@json`` text never reaches a parser that runs out of stack."""
    with pytest.raises(ValueError, match="cannot cast"):
        cast_marker(f"@json {text}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-1+2j", -1 + 2j),
        ("[1, -2, (3, 4)]", [1, -2, (3, 4)]),
        ("{'a': True, 'b': None}", {"a": True, "b": None}),
        ("set()", set()),
        ("[" * 150 + "]" * 150, None),
    ],
)
def test_python_literals_are_still_read(text: str, expected: object) -> None:
    """The guard keeps every Python literal ``@json`` read before."""
    matched, value = cast_marker(f"@json {text}")

    assert_that(matched).is_true()
    if expected is not None:
        assert_that(value).is_equal_to(expected)
    else:
        assert_that(nesting_depth(value)).is_equal_to(150)


@pytest.mark.parametrize("parts", [MAX_DEPTH, MAX_DEPTH + 1])
def test_flat_key_at_the_limit_loads_and_one_past_is_refused(
    tmp_path: Path,
    parts: int,
) -> None:
    """Blocker 2: a flat key nests one mapping per part, the file's counted."""
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({".".join(["a"] * parts): 1}))

    if parts <= MAX_DEPTH:
        load_config(config_path, load_env=False)
        return
    with pytest.raises(ConfigError, match=_DEEP):
        load_config(config_path, load_env=False)


def test_cast_string_value_too_deep_is_refused(tmp_path: Path) -> None:
    """Blocker 2: a value set as a deep cast string is refused, not a crash."""
    value = "@json " + "[" * 1500 + "]" * 1500

    message = _refusal(tmp_path, "workers: 1\n", "paths.include_patterns", value)

    assert_that(message).contains("Unable to load Winnow configuration")


def test_custom_sequence_that_contains_itself_is_refused(tmp_path: Path) -> None:
    """Blocker 2: a ``UserList`` holding itself is a collection that cycles."""
    value: collections.UserList[object] = collections.UserList()
    value.append(value)

    message = _refusal(tmp_path, "workers: 1\n", "source_dirs", value)

    assert_that(message).contains("contains itself")


def test_every_walker_agrees_on_what_a_collection_is() -> None:
    """Blocker 2: ``check_finite`` walks what ``children`` calls a collection."""
    nan_inside = collections.UserList([{"k": (float("nan"),)}])
    cyclic: list[object] = [1.0]
    cyclic.append(cyclic)

    assert_that(children(nan_inside)).is_not_none()
    assert_that(children("text")).is_none()
    assert_that(children(b"bytes")).is_none()
    with pytest.raises(ConfigError, match="not a finite number"):
        check_finite(nan_inside, key="k")
    check_finite(cyclic, key="k")


@pytest.mark.parametrize(
    ("text", "start", "expected"),
    [
        ('[["[[[[", {"a": [1]}]]', 0, 4),
        ('"\\"[[[" [[1]]', 0, 2),
        ("x {[[ ]]} [[[[[[", 2, 3),
        ("]", 0, 0),
    ],
)
def test_text_depth_counts_brackets_outside_strings(
    text: str,
    start: int,
    expected: int,
) -> None:
    """Strings, escapes and the end of the first value bound the count."""
    assert_that(text_depth(text, start)).is_equal_to(expected)


def test_recursion_error_is_handled_only_for_tomli() -> None:
    """Blocker 2: only tomli's own refusal, in one place, is handled."""
    package = Path(_yaml_tree.__file__).parent
    handlers = [
        path.name
        for path in sorted(package.glob("*.py"))
        if "RecursionError" in path.read_text(encoding="utf-8")
    ]

    assert_that(handlers).is_equal_to(["_shape.py"])


@pytest.mark.parametrize(
    ("variable", "raw"),
    [
        pytest.param("WINNOW_SOURCE_DIRS", "[" * 500 + "]" * 500, id="past-tomli"),
        pytest.param("WINNOW_SOURCE_DIRS", "[" * 150 + "]" * 150, id="toml-deep"),
        pytest.param("WINNOW_SOURCE_DIRS", "@json " + "[" * 100_000, id="json-cast"),
        pytest.param("WINNOW_" + "A__" * 150 + "B", "1", id="flat-name"),
    ],
)
def test_deep_environment_value_is_a_config_error_naming_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    raw: str,
) -> None:
    """#451: an environment value nested too deeply is refused by name."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 1\n", encoding="utf-8")
    monkeypatch.setenv(variable, raw)

    with pytest.raises(ConfigError) as loaded:
        load_config(config_path)
    result = CliRunner().invoke(main, ["config", "show", "--config", str(config_path)])

    assert_that(str(loaded.value)).contains(variable)
    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.exception).is_instance_of(SystemExit)


def test_deep_companion_file_is_a_config_error(tmp_path: Path) -> None:
    """#451: the ``.local`` companion is measured as the file is."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 1\n", encoding="utf-8")
    (tmp_path / "c.local.yaml").write_text(_CAST_DEEP, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_path, load_env=False)
    _refusal(tmp_path, "workers: 1\n", "workers", 2)


def test_deep_command_line_value_is_a_config_error(tmp_path: Path) -> None:
    """#451: ``config set`` measures a value before ``json`` parses it."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 1\n", encoding="utf-8")
    command = ["config", "set", "source_dirs", "[" * 100_000, "--config"]

    result = CliRunner().invoke(main, [*command, str(config_path)])

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.exception).is_instance_of(SystemExit)
    assert_that(result.stderr).contains("nests more than")
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to("workers: 1\n")


# Blocker 3: a new key after an empty last value in a flow mapping.


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "paths: {output_dir: }\n",
            "paths: {output_dir: , quarantine_dir: /q}\n",
        ),
        (
            "paths: {output_dir: ,}\n",
            "paths: {output_dir: , quarantine_dir: /q}\n",
        ),
        (
            "paths: {output_dir:\n  }\n",
            "paths: {output_dir: , quarantine_dir: /q\n  }\n",
        ),
        (
            "paths: {output_dir:   \n  }\n",
            "paths: {output_dir: , quarantine_dir: /q\n  }\n",
        ),
    ],
)
def test_new_key_after_an_empty_flow_value_keeps_the_last_key(
    tmp_path: Path,
    original: str,
    expected: str,
) -> None:
    """Blocker 3: ``output_dir`` stays ``output_dir``, not ``output_dir:``."""
    after = _edit(tmp_path, original, "paths.quarantine_dir", "/q")

    assert_that(after).is_equal_to(expected)


def test_new_key_after_a_key_holding_its_colon_keeps_it() -> None:
    """Blocker 3: in ``{review:}`` the key is ``review:``, and stays so."""
    original = "routing: {review:}\n"

    after = plan_edit(original, ["routing", "graphics"], "Art").apply(original)

    assert_that(after).is_equal_to("routing: {review:, graphics: Art}\n")
    loaded = YAML(typ="safe", pure=True).load(after)
    assert_that(loaded).is_equal_to({"routing": {"review:": None, "graphics": "Art"}})


def test_edit_that_changes_another_key_is_not_an_invalid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocker 3: a wrong edit is reported as one, never as an invalid file."""
    original = "paths:\n  output_dir: /o\nworkers: 1\n"
    broken = "paths:\n  output_dir: /o\n  bogus: 1\nworkers: 2\n"

    def wrong_plan(text: str, key_parts: list[str], value: object) -> Splice:
        return Splice(0, len(text), broken)

    monkeypatch.setattr(_writer, "plan_edit", wrong_plan)

    message = _refusal(tmp_path, original, "workers", 2)

    assert_that(message).contains(_UNSAFE).does_not_contain("Invalid")


def test_invalid_value_is_still_reported_as_invalid(tmp_path: Path) -> None:
    """An edit that changes only the key reports the value as invalid."""
    message = _refusal(tmp_path, "workers: 1\n", "workers", "many")

    assert_that(message).contains("Invalid Winnow configuration")


# Blocker 4: an anchor or tag on a line of its own above a block collection.


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        pytest.param(
            "source_dirs:\n    &s\n  - /a\n",
            "source_dirs",
            ["/b", "/c"],
            id="anchored-sequence",
        ),
        pytest.param(
            "source_dirs:\n    !!seq\n  - /a\n",
            "source_dirs",
            ["/b", "/c"],
            id="tagged-sequence",
        ),
        pytest.param(
            "cache:\n    &c\n  enabled: true\n",
            "cache",
            {"directory": "/d"},
            id="anchored-mapping",
        ),
    ],
)
def test_block_under_a_property_line_at_another_column_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """Blocker 4: the dashes' column, not the anchor's, decides."""
    assert_that(_refusal(tmp_path, original, key, value)).contains(_INDENT_ERROR)


def test_block_under_a_property_line_at_its_column_is_replaced(
    tmp_path: Path,
) -> None:
    """Blocker 4: an anchor at the dashes' column is replaced with the list."""
    after = _edit(tmp_path, "source_dirs:\n  &s\n  - /a\n", "source_dirs", ["/b"])

    assert_that(after).is_equal_to("source_dirs:\n  - /b\n")


# Finding 5: trailing spaces after a replaced block.


@pytest.mark.parametrize(
    "original",
    [
        "cache:\n  enabled:   \nworkers: 1\n",
        "cache:\n  enabled: true   \nworkers: 1\n",
    ],
)
def test_trailing_spaces_after_a_replaced_block_go_with_it(
    tmp_path: Path,
    original: str,
) -> None:
    """Finding 5: the new value does not end in the old value's spaces."""
    after = _edit(tmp_path, original, "cache", {"directory": "/d"})

    assert_that(after).is_equal_to("cache:\n  directory: /d\nworkers: 1\n")


def test_trailing_spaces_after_an_inline_value_stay(tmp_path: Path) -> None:
    """A value on the key's line keeps the spaces after it, as before."""
    assert_that(_edit(tmp_path, "workers: 1   \n", "workers", 2)).is_equal_to(
        "workers: 2   \n",
    )


# Finding 6: each attribute docstring sits under its own attribute.


def test_yaml_node_attribute_docstrings_follow_their_attributes() -> None:
    """Finding 6: ``end`` is documented under ``end``, not under ``column``."""
    source = inspect.getsource(_yaml_tree.YamlNode)
    body = ast.parse(io.StringIO(source).read()).body[0]
    assert_that(body).is_instance_of(ast.ClassDef)
    body = cast("ast.ClassDef", body)
    documented: dict[str, str] = {}
    for previous, current in zip(body.body, body.body[1:], strict=False):
        if (
            isinstance(previous, ast.AnnAssign)
            and isinstance(previous.target, ast.Name)
            and isinstance(current, ast.Expr)
            and isinstance(current.value, ast.Constant)
        ):
            documented[previous.target.id] = str(current.value.value)

    assert_that(documented["end"]).starts_with("Where the parser ends the node")
    assert_that(documented["column"]).starts_with("The column the parser gives")
