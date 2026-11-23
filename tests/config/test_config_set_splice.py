"""``config set`` replaces only the edited value's text in a YAML file (#429).

The fifth review round's blockers, each as a test, and the splice's own
checks: where a value's text ends, where a new key's lines go, how a new
value is written, and the refusals that remain.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that
from ruamel.yaml.error import StreamMark
from ruamel.yaml.tokens import CommentToken

from winnow.config import set_config_value
from winnow.config._yaml_render import YamlStyle, file_style, render_inline
from winnow.config._yaml_splice import Splice, plan_edit
from winnow.config._yaml_tree import _comment_lines, parse_yaml
from winnow.exceptions import ConfigError

_ANCHOR_ERROR = "shared through a YAML anchor"
_COMMENT_ERROR = "a comment inside its old value"


def _edit(tmp_path: Path, original: str, key: str, value: object) -> str:
    """Set a key in a file holding ``original`` and return the file's text."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")
    set_config_value(key, value, config_path=config_path)
    return config_path.read_text(encoding="utf-8")


def _refusal(tmp_path: Path, original: str, key: str, value: object) -> str:
    """Return the error a refused set gives, checking the file is untouched."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        set_config_value(key, value, config_path=config_path)
    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(original)
    return str(error.value)


def test_anchor_on_a_replaced_key_used_elsewhere_is_refused(tmp_path: Path) -> None:
    """Blocker 1: an anchor on a key inside the value, aliased outside it."""
    message = _refusal(
        tmp_path,
        "paths:\n  &k output_dir: /out\nrouting: {review: *k}\n",
        "paths",
        {"output_dir": "/new"},
    )

    assert_that(message).contains(_ANCHOR_ERROR)


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        (
            "routing:\n  review: R  # note\n  graphics: G  # note\n"
            "  screenshots: S\nworkers: 2\n",
            "routing",
            {"review": "R2", "screenshots": "S"},
        ),
        ("source_dirs:\n  - /a  # c\n  - /b\nworkers: 2\n", "source_dirs", ["/x # c"]),
    ],
)
def test_inline_comment_in_a_replaced_value_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """Blocker 2: comments the new value would drop, even with matching text."""
    assert_that(_refusal(tmp_path, original, key, value)).contains(_COMMENT_ERROR)


@pytest.mark.parametrize(
    "value",
    ["/srv/out", "Review #1", "Photos # old"],
)
@pytest.mark.parametrize(
    "old",
    ['"/tmp/dir #1"', "'Review #1'", '"Photos # old"'],
)
def test_quoted_hash_is_not_a_comment(tmp_path: Path, old: str, value: str) -> None:
    """Blocker 2: `` #`` inside a quoted string does not block the edit."""
    original = f"paths:\n  output_dir: {old}\nworkers: 2\n"

    after = _edit(tmp_path, original, "paths.output_dir", value)

    assert_that(after).starts_with("paths:\n  output_dir: ")
    assert_that(after).ends_with("\nworkers: 2\n")
    assert_that(after.count("\n")).is_equal_to(3)


@pytest.mark.parametrize(
    ("original", "key", "value"),
    [
        (
            "source_dirs:\n  - /a\n  - /b  # only for /b\nworkers: 2\n",
            "source_dirs",
            ["/x"],
        ),
        (
            "source_dirs:\n  - /a\n  - /b  # only for /b\nworkers: 2\n",
            "source_dirs",
            [],
        ),
        (
            "cache:\n  enabled: true\n  directory: /c  # only here\nworkers: 2\n",
            "cache",
            {"enabled": False},
        ),
        (
            "cache:\n  enabled: true\n  directory: /c  # only here\nworkers: 2\n",
            "cache",
            {},
        ),
    ],
)
def test_comment_on_the_last_line_is_not_moved(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
) -> None:
    """Blocker 3: a comment after the last item would land on another one."""
    assert_that(_refusal(tmp_path, original, key, value)).contains(_COMMENT_ERROR)


def test_new_key_goes_below_an_indented_comment(tmp_path: Path) -> None:
    """Blocker 4: a comment indented into a block stays in it, above the new key."""
    after = _edit(tmp_path, "cache:\n  enabled: true\n  # cache end\n", "workers", 3)

    assert_that(after).is_equal_to(
        "cache:\n  enabled: true\n  # cache end\nworkers: 3\n"
    )


def test_shallow_comment_stays_below_the_new_key(tmp_path: Path) -> None:
    """A comment at the mapping's own indent is not skipped."""
    after = _edit(
        tmp_path,
        "cache:\n  enabled: true\n  # about cache\nworkers: 2\n",
        "cache.directory",
        "/c",
    )

    assert_that(after).is_equal_to(
        "cache:\n  enabled: true\n  directory: /c\n  # about cache\nworkers: 2\n",
    )


def test_block_list_without_comments_is_replaced(tmp_path: Path) -> None:
    """Blocker 5: a list that keeps one item in place is still replaced."""
    after = _edit(
        tmp_path,
        "source_dirs:\n  - /a\n  - /b\n  - /c\nworkers: 2\n",
        "source_dirs",
        ["/x", "/b", "/z"],
    )

    assert_that(after).is_equal_to("source_dirs:\n  - /x\n  - /b\n  - /z\nworkers: 2\n")


def test_items_with_similar_comments_are_refused(tmp_path: Path) -> None:
    """Blocker 6: comments whose text contains another's are both kept or refused."""
    message = _refusal(
        tmp_path,
        "source_dirs:\n  - /a  # source\n  - /b  # source backup\nworkers: 2\n",
        "source_dirs",
        ["/c"],
    )

    assert_that(message).contains(_COMMENT_ERROR)


def test_anchored_capitalised_boolean_is_left_alone(tmp_path: Path) -> None:
    """Blocker 7: an anchored ``True`` on another line is not touched."""
    after = _edit(tmp_path, "dry_run: &b True\nworkers: 2\n", "workers", 3)

    assert_that(after).is_equal_to("dry_run: &b True\nworkers: 3\n")


@pytest.mark.parametrize(
    ("original", "key", "value", "after"),
    [
        (
            "cache:\n    enabled: true\nworkers: 2\n",
            "cache",
            {"enabled": False, "directory": "/d"},
            "cache:\n    enabled: false\n    directory: /d\nworkers: 2\n",
        ),
        (
            "source_dirs: [/a]\nworkers: 2\n",
            "source_dirs",
            ["/a", "/b"],
            "source_dirs:\n  - /a\n  - /b\nworkers: 2\n",
        ),
        (
            "source_dirs:\n- /a\nworkers: 2\n",
            "routing",
            {"review": "R"},
            "source_dirs:\n- /a\nworkers: 2\nrouting:\n  review: R\n",
        ),
        (
            "routing:\n  review:\n    Review\nworkers: 2\n",
            "routing.review",
            "Later",
            "routing:\n  review: Later\nworkers: 2\n",
        ),
        ("routing: {}\n", "routing.review", "R", "routing: {review: R}\n"),
        ("workers: 2", "dry_run", True, "workers: 2\ndry_run: true"),
        (
            "cache:\n  enabled: true\n  # end",
            "workers",
            3,
            "cache:\n  enabled: true\n  # end\nworkers: 3",
        ),
        (
            "note: |+\n  text\n\n",
            "workers",
            3,
            "note: |+\n  text\n\nworkers: 3\n",
        ),
    ],
)
def test_value_is_written_in_the_file_style(
    tmp_path: Path,
    original: str,
    key: str,
    value: object,
    after: str,
) -> None:
    """Blocks go below the key in the file's indents; scalars on the key's line."""
    assert_that(_edit(tmp_path, original, key, value)).is_equal_to(after)


@pytest.mark.parametrize(
    ("original", "key", "value", "after"),
    [
        (
            "routing:\n  review: R\nsource_dirs:\n- /a\n",
            "source_dirs",
            "/b",
            "routing:\n  review: R\nsource_dirs: /b\n",
        ),
        (
            "cache:\n  enabled: true\nworkers: 2\n",
            "cache",
            ["/a"],
            "cache:\n  - /a\nworkers: 2\n",
        ),
        (
            "routing: {review: R}\n",
            "routing.review",
            "two\nlines",
            'routing: {review: "two\\nlines"}\n',
        ),
        (
            "paths: {output_dir: }\n",
            "paths.output_dir",
            ["/a"],
            "paths: {output_dir: [/a] }\n",
        ),
    ],
)
def test_splice_changes_the_shape_of_a_value(
    original: str,
    key: str,
    value: object,
    after: str,
) -> None:
    """A list over a scalar, a scalar over a list, and values in flow maps."""
    splice = plan_edit(original, key.split("."), value)

    assert_that(splice.apply(original)).is_equal_to(after)


def test_value_alias_is_refused(tmp_path: Path) -> None:
    """A value that is an alias is not replaced."""
    message = _refusal(tmp_path, "a: &x 1\nworkers: *x\n", "workers", 3)

    assert_that(message).contains(_ANCHOR_ERROR)


def test_key_below_an_alias_is_refused(tmp_path: Path) -> None:
    """A key inside a mapping reached through an alias is not set."""
    message = _refusal(
        tmp_path,
        "base: &b {review: R}\nrouting: *b\n",
        "routing.review",
        "X",
    )

    assert_that(message).contains(_ANCHOR_ERROR)


def test_alias_inside_the_replaced_value_does_not_block(tmp_path: Path) -> None:
    """An alias inside the replaced text refers out of it; dropping it is fine."""
    after = _edit(
        tmp_path,
        "base: &b /a\nsource_dirs:\n  - *b\n  - /c\n",
        "source_dirs",
        ["/x"],
    )

    assert_that(after).is_equal_to("base: &b /a\nsource_dirs:\n  - /x\n")


def test_two_documents_are_refused_by_the_planner() -> None:
    """The planner refuses a second document even if the reader let it by."""
    with pytest.raises(ConfigError) as error:
        plan_edit("workers: 2\n---\nworkers: 3\n", ["workers"], 4)

    assert_that(str(error.value)).contains("more than one YAML document")


def test_top_level_that_is_not_a_mapping_is_a_value_error() -> None:
    """A list at the top level cannot take a key."""
    with pytest.raises(ValueError, match="top level"):
        plan_edit("- a\n", ["workers"], 4)


def test_splice_that_does_not_fit_its_range_is_refused() -> None:
    """A splice whose range runs backwards is refused rather than applied."""
    with pytest.raises(ConfigError) as error:
        Splice(3, 1, "x").apply("abcdef")

    assert_that(str(error.value)).contains("could not limit its edit")


def test_splice_puts_its_text_between_the_kept_parts() -> None:
    """A splice keeps the text before and after its range."""
    assert_that(Splice(1, 3, "XY").apply("abcd")).is_equal_to("aXYd")


def test_comment_positions_come_from_the_scanner() -> None:
    """Comments are found by the scanner, never inside quoted text."""
    text = (
        '# top\na: "x #1"  # one\nb: | # header\n  t # text\n'
        "c:\n  - 1\n\n  # deep\n# shallow\n"
    )

    tree = parse_yaml(text)

    assert_that(
        [text[position : position + 4] for position in tree.comments]
    ).is_equal_to(
        ["# to", "# on", "# he", "# de", "# sh"],
    )


def test_file_style_reads_indents_from_nodes() -> None:
    """The mapping indent, sequence offset and item indent come from the file."""
    tree = parse_yaml("a:\n    b: 1\nc:\n- x\nd:\n  -\n    y\n")

    assert_that(file_style(tree)).is_equal_to(
        YamlStyle(mapping=4, sequence=2, offset=0)
    )


def test_file_style_defaults_without_examples() -> None:
    """A flat file gives ruamel's defaults."""
    assert_that(file_style(parse_yaml("a: 1\n"))).is_equal_to(YamlStyle())


def test_inline_value_that_needs_lines_is_a_value_error() -> None:
    """A value that cannot be written on one line is not written."""
    with pytest.raises(ValueError, match="one line"):
        render_inline(b"binary data", YamlStyle(), flow=False)


def test_null_by_tag_is_replaced_by_a_mapping() -> None:
    """A value tagged ``!!null`` is a null the key's mapping replaces."""
    original = "cache: !!null\nworkers: 2\n"

    after = plan_edit(original, ["cache", "enabled"], False).apply(original)

    assert_that(after).is_equal_to("cache:\n  enabled: false\nworkers: 2\n")


def test_flow_key_without_a_colon_takes_a_sibling() -> None:
    """A flow mapping entry with no ``:`` keeps its text when a key follows it."""
    original = "routing: {review}\n"

    after = plan_edit(original, ["routing", "graphics"], "G").apply(original)

    assert_that(after).is_equal_to("routing: {review, graphics: G}\n")


def test_comment_the_scanner_misplaces_is_a_value_error() -> None:
    """A comment token whose text is not in the file fails closed."""
    token = CommentToken("# missing\n", StreamMark("c.yaml", 0, 0, 0))

    with pytest.raises(ValueError, match="cannot place the comment"):
        list(_comment_lines("a: 1\n", token))


def test_blank_comment_token_holds_no_comment() -> None:
    """A comment token of blank lines places nothing."""
    token = CommentToken("\n\n", StreamMark("c.yaml", 0, 0, 0))

    assert_that(list(_comment_lines("a: 1\n\n", token))).is_empty()


def test_deep_key_follows_nested_mappings() -> None:
    """A key three levels down is found through each mapping, matched by case."""
    original = "a:\n  B:\n    c: 1  # keep\n"

    after = plan_edit(original, ["a", "b", "c"], 2).apply(original)

    assert_that(after).is_equal_to("a:\n  B:\n    c: 2  # keep\n")
