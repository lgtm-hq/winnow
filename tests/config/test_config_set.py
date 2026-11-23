"""``config set`` edits one key of the config file as the user wrote it (#429).

The first four tests started as characterizations of the bug on ``main``
(companion values copied into the file, merged lists growing, cast results
written back, YAML written into a ``.toml`` file); each passed there before
being turned into the behaviour #429 requires.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import CONFIG_FILE_NAME, load_config, set_config_value
from winnow.config._editor import set_in_document
from winnow.exceptions import ConfigError
from winnow.models.enums import SymlinkPolicy

_LOCAL_NAME = ".winnow-config.local.yaml"
_COMMENTED = """\
# Winnow settings
workers: 2  # threads
source_dirs: [/a, '/b']
routing.screenshots: Shots
cache:
    enabled: true
"""


def _set(tmp_path: Path, key: str, value: object) -> None:
    """Run ``set_config_value`` with discovery rooted at ``tmp_path``."""
    set_config_value(key, value, cwd=tmp_path, home_config_dir=tmp_path / "home")


def _main_file(tmp_path: Path) -> str:
    """Return the main config file's text."""
    return (tmp_path / CONFIG_FILE_NAME).read_text()


# --- the four cases from #429 ---------------------------------------------------


def test_companion_value_is_not_copied(tmp_path: Path) -> None:
    """A value only in the companion stays out of the main file."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\ndry_run: false\n")
    (tmp_path / _LOCAL_NAME).write_text("dry_run: true\nworkers: 5\n")

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to("workers: 3\ndry_run: false\n")
    assert_that((tmp_path / _LOCAL_NAME).read_text()).is_equal_to(
        "dry_run: true\nworkers: 5\n",
    )


def test_merged_list_does_not_grow(tmp_path: Path) -> None:
    """Repeated sets leave a list merged with the companion's as written."""
    (tmp_path / CONFIG_FILE_NAME).write_text("source_dirs:\n  - /a\nworkers: 1\n")
    (tmp_path / _LOCAL_NAME).write_text("source_dirs:\n  - /b\n")

    _set(tmp_path, "workers", 3)
    _set(tmp_path, "workers", 4)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "source_dirs:\n  - /a\nworkers: 4\n",
    )
    config = load_config(
        cwd=tmp_path, home_config_dir=tmp_path / "home", load_env=False
    )
    assert_that([str(path) for path in config.source_dirs]).is_equal_to(
        ["/a", "/b"],
    )


def test_cast_marker_is_kept(tmp_path: Path) -> None:
    """A ``@int`` marker on another key keeps the form the user wrote."""
    (tmp_path / CONFIG_FILE_NAME).write_text('workers: "@int 4"\n')

    _set(tmp_path, "dry_run", True)

    assert_that(_main_file(tmp_path)).is_equal_to(
        'workers: "@int 4"\ndry_run: true\n',
    )


def test_toml_file_is_refused_and_left_untouched(tmp_path: Path) -> None:
    """``config set --config c.toml`` exits with an error and keeps the file."""
    config_path = tmp_path / "c.toml"
    original = b"# keep me\nworkers = 2\n"
    config_path.write_bytes(original)

    result = CliRunner().invoke(
        main,
        ["config", "set", "dry_run", "true", "--config", str(config_path)],
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("cannot write TOML")
    assert_that(config_path.read_bytes()).is_equal_to(original)
    assert_that(load_config(config_path=config_path).workers).is_equal_to(2)


# --- acceptance criteria --------------------------------------------------------


def test_only_the_set_key_changes_with_a_companion(tmp_path: Path) -> None:
    """Comments, quoting, flow lists and flat keys survive; only one line changes."""
    (tmp_path / CONFIG_FILE_NAME).write_text(_COMMENTED)
    (tmp_path / _LOCAL_NAME).write_text("dry_run: true\nsource_dirs: [/c]\n")

    _set(tmp_path, "workers", 6)

    before = _COMMENTED.splitlines()
    after = _main_file(tmp_path).splitlines()
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert_that(changed).is_equal_to(
        [("workers: 2  # threads", "workers: 6  # threads")]
    )


def test_repeating_set_does_not_change_the_file(tmp_path: Path) -> None:
    """A second identical set leaves the file byte for byte as the first left it."""
    (tmp_path / CONFIG_FILE_NAME).write_text(_COMMENTED)
    (tmp_path / _LOCAL_NAME).write_text("source_dirs: [/c]\n")

    _set(tmp_path, "cache.enabled", False)
    first = _main_file(tmp_path)
    _set(tmp_path, "cache.enabled", False)

    assert_that(_main_file(tmp_path)).is_equal_to(first)
    assert_that(first).contains("    enabled: false\n")


def test_environment_values_do_not_reach_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WINNOW_*`` overrides are not written into the file."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")
    monkeypatch.setenv("WINNOW_DRY_RUN", "true")
    monkeypatch.setenv("WINNOW_WORKERS", "9")

    _set(tmp_path, "cache.enabled", False)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "workers: 2\ncache:\n  enabled: false\n",
    )


# --- where the key is written ---------------------------------------------------


def test_flat_key_is_updated_in_place(tmp_path: Path) -> None:
    """A setting written as a flat key is changed in that entry."""
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "routing.screenshots: Shots\nrouting__review: Rev\n",
    )

    _set(tmp_path, "routing.review", "Later")

    assert_that(_main_file(tmp_path)).is_equal_to(
        "routing.screenshots: Shots\nrouting__review: Later\n",
    )


def test_key_goes_to_the_last_entry_that_covers_it(tmp_path: Path) -> None:
    """A nested key lands in the mapping that takes effect, matched by case."""
    (tmp_path / CONFIG_FILE_NAME).write_text(
        "Routing:\n  Review: A\nrouting.screenshots: S\n",
    )

    _set(tmp_path, "routing.review", "B")

    assert_that(_main_file(tmp_path)).is_equal_to(
        "Routing:\n  Review: B\nrouting.screenshots: S\n",
    )


def test_null_parent_becomes_a_mapping(tmp_path: Path) -> None:
    """A ``null`` on the way to the key is replaced by a mapping, as before."""
    (tmp_path / CONFIG_FILE_NAME).write_text("cache:\n")

    _set(tmp_path, "cache.enabled", False)

    assert_that(_main_file(tmp_path)).is_equal_to("cache:\n  enabled: false\n")


def test_key_overridden_later_in_the_file_is_refused(tmp_path: Path) -> None:
    """When a later entry would undo the change, the file is left untouched."""
    original = "routing:\n  review: A\nrouting.review.extra: X\n"
    (tmp_path / CONFIG_FILE_NAME).write_text(original)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "routing", {"review": "B"})

    assert_that(error.value.context.operation).is_equal_to("set_config")
    assert_that(_main_file(tmp_path)).is_equal_to(original)


def test_invalid_result_leaves_the_file_untouched(tmp_path: Path) -> None:
    """A value the companion makes invalid is rejected before writing."""
    original = "workers: 2\n"
    (tmp_path / CONFIG_FILE_NAME).write_text(original)
    (tmp_path / _LOCAL_NAME).write_text("symlink_policy: follow\n")

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "follow_symlinks", False)

    assert_that(error.value.context.operation).is_equal_to("validate_config")
    assert_that(_main_file(tmp_path)).is_equal_to(original)


def test_symlink_pair_stays_consistent(tmp_path: Path) -> None:
    """Setting ``follow_symlinks`` also writes the policy it implies."""
    (tmp_path / CONFIG_FILE_NAME).write_text("symlink_policy: skip\n")

    _set(tmp_path, "follow_symlinks", True)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "symlink_policy: follow\nfollow_symlinks: true\n",
    )
    config = load_config(
        cwd=tmp_path, home_config_dir=tmp_path / "home", load_env=False
    )
    assert_that(config.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)


# --- other formats --------------------------------------------------------------


def test_json_file_is_written_as_json(tmp_path: Path) -> None:
    """A JSON config keeps its keys, values and indent, and stays JSON."""
    config_path = tmp_path / "c.json"
    config_path.write_text('{\n    "workers": "@int 4",\n    "dry_run": false\n}\n')

    set_config_value("cache.enabled", False, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(
        '{\n    "workers": "@int 4",\n    "dry_run": false,\n'
        '    "cache": {\n        "enabled": false\n    }\n}\n',
    )
    config = load_config(config_path=config_path, load_env=False)
    assert_that(config.workers).is_equal_to(4)
    assert_that(config.cache.enabled).is_false()


def test_single_line_json_stays_on_one_line(tmp_path: Path) -> None:
    """A JSON config written on one line is written back on one line."""
    config_path = tmp_path / "c.json"
    config_path.write_text('{"workers": 2}')

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to('{"workers": 3}')


def test_new_json_file_is_written_as_json(tmp_path: Path) -> None:
    """A JSON config that does not exist yet is created as JSON."""
    config_path = tmp_path / "c.json"

    set_config_value("workers", 3, config_path=config_path)

    assert_that(json.loads(config_path.read_text())).is_equal_to({"workers": 3})


def test_new_toml_file_is_refused_and_not_created(tmp_path: Path) -> None:
    """A TOML config that does not exist yet is not created with YAML in it."""
    config_path = tmp_path / "c.tml"

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(str(error.value)).contains("cannot write TOML")
    assert_that(config_path.exists()).is_false()


def test_ini_file_is_refused(tmp_path: Path) -> None:
    """A format winnow does not read is refused before anything is written."""
    config_path = tmp_path / "c.ini"

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(str(error.value)).contains("INI config files are not supported")
    assert_that(config_path.exists()).is_false()


def test_new_yaml_file_is_written_as_before(tmp_path: Path) -> None:
    """A YAML config that does not exist yet is created as it always was."""
    _set(tmp_path, "source_dirs", ["/a"])

    assert_that(_main_file(tmp_path)).is_equal_to("source_dirs:\n  - /a\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- a\n", "must be a mapping"),
        ("workers: [\n", "Unable to load"),
    ],
)
def test_unreadable_yaml_is_a_load_error(
    tmp_path: Path,
    text: str,
    message: str,
) -> None:
    """A target file that is not a valid mapping is reported, not overwritten."""
    (tmp_path / CONFIG_FILE_NAME).write_text(text)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "workers", 3)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that(str(error.value) + str(error.value.context.details)).contains(
        message,
    )
    assert_that(_main_file(tmp_path)).is_equal_to(text)


# --- errors and edge cases ------------------------------------------------------


def test_empty_yaml_file_gets_the_key(tmp_path: Path) -> None:
    """An existing empty YAML file is treated as an empty mapping."""
    (tmp_path / CONFIG_FILE_NAME).write_text("")

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to("workers: 3\n")


def test_dunder_key_that_does_not_read_back_is_refused(tmp_path: Path) -> None:
    """A key the reader would split differently is refused, file untouched."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "routing__review", "X")

    assert_that(error.value.context.operation).is_equal_to("set_config")
    assert_that(_main_file(tmp_path)).is_equal_to("workers: 2\n")


def test_invalid_companion_is_a_load_error(tmp_path: Path) -> None:
    """A companion that does not parse fails the set before writing."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")
    (tmp_path / _LOCAL_NAME).write_text("workers: [\n")

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "workers", 3)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that(_main_file(tmp_path)).is_equal_to("workers: 2\n")


def test_undecodable_file_is_a_load_error(tmp_path: Path) -> None:
    """A target file that is not UTF-8 is reported, not overwritten."""
    (tmp_path / CONFIG_FILE_NAME).write_bytes(b"workers: \xff\n")

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "workers", 3)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that((tmp_path / CONFIG_FILE_NAME).read_bytes()).is_equal_to(
        b"workers: \xff\n",
    )


def test_write_failure_is_a_write_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write leaves the file and no temporary file behind."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")

    def _fail(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("winnow.config._writer.os.fsync", _fail)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "workers", 3)

    assert_that(error.value.context.operation).is_equal_to("write_config")
    assert_that(_main_file(tmp_path)).is_equal_to("workers: 2\n")
    assert_that(sorted(path.name for path in tmp_path.iterdir())).is_equal_to(
        [CONFIG_FILE_NAME],
    )


def test_nested_key_below_an_existing_mapping() -> None:
    """A deep key reuses existing mappings, matched by case, and adds the rest."""
    document: dict[str, object] = {"a": {"B": {"c": 1}}}

    set_in_document(document, ["a", "b", "d", "e"], 2)

    assert_that(document).is_equal_to({"a": {"B": {"c": 1, "d": {"e": 2}}}})


def test_unindented_json_lines_fall_back_to_one_line(tmp_path: Path) -> None:
    """JSON with line breaks but no indent is written back without one."""
    config_path = tmp_path / "c.json"
    config_path.write_text('{\n"workers": 2\n}\n')

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to('{"workers": 3}\n')


# --- review findings: one key or a ConfigError, never a crash -------------------


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), {"x": [float("nan")]}],
)
def test_non_finite_number_is_refused(
    tmp_path: Path,
    suffix: str,
    value: object,
) -> None:
    """NaN and infinities fail with a clear error and leave the file as it was."""
    config_path = tmp_path / f"c{suffix}"
    original = '{"workers": 2}\n'
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("min_similarity", value, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("set_config")
    assert_that(str(error.value)).contains("not a finite number")
    assert_that(config_path.read_text()).is_equal_to(original)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_number_on_the_command_line_is_an_error(
    tmp_path: Path,
    raw: str,
) -> None:
    """The CLI reports a non-finite number as an error, not a traceback."""
    config_path = tmp_path / "c.json"
    config_path.write_text('{"workers": 2}\n')

    result = CliRunner().invoke(
        main,
        ["config", "set", "--config", str(config_path), "--", "min_similarity", raw],
    )

    assert_that(result.exit_code).is_equal_to(ExitCode.FAILURE)
    assert_that(result.stderr).contains("finite")
    assert_that(config_path.read_text()).is_equal_to('{"workers": 2}\n')


def test_unserializable_value_is_a_config_error(tmp_path: Path) -> None:
    """A value the format cannot hold fails as ``ConfigError``, file untouched."""
    config_path = tmp_path / "c.json"
    config_path.write_text('{"workers": 2}\n')

    with pytest.raises(ConfigError):
        set_config_value("workers", object(), config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to('{"workers": 2}\n')


@pytest.mark.parametrize("policy", ["skip", "error"])
def test_policy_matching_the_default_adds_no_paired_key(
    tmp_path: Path,
    policy: str,
) -> None:
    """``follow_symlinks`` is written only when the file would contradict itself."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")

    _set(tmp_path, "symlink_policy", policy)

    assert_that(_main_file(tmp_path)).is_equal_to(
        f"workers: 2\nsymlink_policy: {policy}\n",
    )


def test_follow_policy_still_writes_the_paired_key(tmp_path: Path) -> None:
    """A policy the default ``follow_symlinks`` contradicts still sets it."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")

    _set(tmp_path, "symlink_policy", "follow")

    assert_that(_main_file(tmp_path)).is_equal_to(
        "workers: 2\nsymlink_policy: follow\nfollow_symlinks: true\n",
    )


def test_symlinked_config_file_keeps_its_link(tmp_path: Path) -> None:
    """Setting through a symlink writes the target and keeps the link."""
    target = tmp_path / "real" / "settings.yaml"
    target.parent.mkdir()
    target.write_text("workers: 2\n")
    link = tmp_path / "c.yaml"
    link.symlink_to(target)

    set_config_value("workers", 3, config_path=link)

    assert_that(link.is_symlink()).is_true()
    assert_that(target.read_text()).is_equal_to("workers: 3\n")
    assert_that(sorted(path.name for path in target.parent.iterdir())).is_equal_to(
        ["settings.yaml"],
    )


def test_comment_only_yaml_keeps_its_comments(tmp_path: Path) -> None:
    """A YAML file holding only comments keeps them when a key is added."""
    (tmp_path / CONFIG_FILE_NAME).write_text("# Winnow settings\n\n# more\n")

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "# Winnow settings\n\n# more\nworkers: 3\n",
    )


_ANCHORED = "base: &b\n  review: A\nrouting: *b\nworkers: 2\n"


@pytest.mark.parametrize("key", ["routing.review", "base.review", "base.x.y"])
def test_key_shared_through_an_anchor_is_refused(tmp_path: Path, key: str) -> None:
    """A key inside an anchored mapping that an alias shares is not edited."""
    (tmp_path / CONFIG_FILE_NAME).write_text(_ANCHORED)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, key, "X")

    assert_that(str(error.value)).contains("anchor")
    assert_that(str(error.value)).contains("by hand")
    assert_that(_main_file(tmp_path)).is_equal_to(_ANCHORED)


def test_unshared_key_in_a_file_with_anchors_can_be_set(tmp_path: Path) -> None:
    """Keys outside the shared mapping stay settable."""
    (tmp_path / CONFIG_FILE_NAME).write_text(_ANCHORED)

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to(
        "base: &b\n  review: A\nrouting: *b\nworkers: 3\n",
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write read-only files")
def test_read_only_file_is_refused(tmp_path: Path) -> None:
    """A file the user cannot write is refused, not replaced."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\n")
    config_path.chmod(0o444)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "workers", 3)

    assert_that(str(error.value)).contains("not writable")
    assert_that(_main_file(tmp_path)).is_equal_to("workers: 2\n")
    assert_that(stat.S_IMODE(config_path.stat().st_mode)).is_equal_to(0o444)


def test_existing_permission_bits_are_kept(tmp_path: Path) -> None:
    """The replacement file keeps the permission bits of the file it replaces."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\n")
    config_path.chmod(0o640)

    _set(tmp_path, "workers", 3)

    assert_that(stat.S_IMODE(config_path.stat().st_mode)).is_equal_to(0o640)


def test_new_file_mode_is_unchanged(tmp_path: Path) -> None:
    """A new file gets the private mode ``config set`` always gave it."""
    _set(tmp_path, "workers", 3)

    mode = stat.S_IMODE((tmp_path / CONFIG_FILE_NAME).stat().st_mode)
    assert_that(mode).is_equal_to(0o600)


@pytest.mark.parametrize(
    ("name", "text", "key", "value"),
    [
        ("c.json", '{"workers":2}', "workers", 2),
        ("c.yaml", "workers: 2", "workers", 2),
        ("c.yaml", "cache: {enabled: true}\n", "cache.enabled", True),
        ("c.yaml", 'workers: "@int 4"\n', "workers", 4),
    ],
)
def test_value_already_set_writes_nothing(
    tmp_path: Path,
    name: str,
    text: str,
    key: str,
    value: object,
) -> None:
    """Setting the value a key already has leaves the file byte for byte."""
    config_path = tmp_path / name
    config_path.write_text(text)
    before = config_path.stat().st_mtime_ns

    set_config_value(key, value, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(text)
    assert_that(config_path.stat().st_mtime_ns).is_equal_to(before)


def test_equal_value_of_another_type_is_written(tmp_path: Path) -> None:
    """``1`` is not the same value as ``true``, so the file changes."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("dry_run: 1\n")

    set_config_value("dry_run", True, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to("dry_run: true\n")


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        (
            "c.yaml",
            b"workers: 2\r\ndry_run: false\r\n",
            b"workers: 3\r\ndry_run: false\r\n",
        ),
        ("c.yaml", b"workers: 2", b"workers: 3"),
        ("c.yaml", b"workers: 2\r\ndry_run: false", b"workers: 3\r\ndry_run: false"),
        (
            "c.json",
            b'{\r\n  "workers": 2\r\n}\r\n',
            b'{\r\n  "workers": 3\r\n}\r\n',
        ),
        ("c.json", b'{\r\n  "workers": 2\r\n}', b'{\r\n  "workers": 3\r\n}'),
    ],
)
def test_line_endings_and_final_newline_are_kept(
    tmp_path: Path,
    name: str,
    before: bytes,
    after: bytes,
) -> None:
    """CRLF files stay CRLF and a missing final newline stays missing."""
    config_path = tmp_path / name
    config_path.write_bytes(before)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_bytes()).is_equal_to(after)


def test_duplicate_json_key_is_refused(tmp_path: Path) -> None:
    """A JSON file with a duplicate key is refused, as YAML ones are."""
    config_path = tmp_path / "c.json"
    original = '{"workers": 2, "workers": 3}\n'
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("dry_run", True, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that(str(error.value.context.details)).contains("duplicate key")
    assert_that(config_path.read_text()).is_equal_to(original)


def test_child_of_a_cast_string_is_refused_with_a_reason(tmp_path: Path) -> None:
    """A key under a ``@json`` string names the cast and the ways out."""
    original = 'routing: "@json {\\"review\\": \\"A\\"}"\n'
    (tmp_path / CONFIG_FILE_NAME).write_text(original)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "routing.review", "Later")

    message = str(error.value)
    assert_that(message).contains("cast string")
    assert_that(message).contains("set the whole routing")
    assert_that(message).contains("by hand")
    assert_that(_main_file(tmp_path)).is_equal_to(original)


def test_empty_json_file_is_a_load_error(tmp_path: Path) -> None:
    """An empty JSON file is invalid JSON, as the reader has always said."""
    config_path = tmp_path / "c.json"
    config_path.write_text("")

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that(config_path.read_text()).is_equal_to("")


def test_yaml_parse_error_names_the_file(tmp_path: Path) -> None:
    """A parse error from the editing round trip names the config file."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\nworkers: 3\n")

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "dry_run", True)

    assert_that(str(error.value.context.details)).contains(str(config_path))


def test_parent_line_with_an_inline_comment_keeps_its_indent(tmp_path: Path) -> None:
    """A mapping whose key line ends in a comment is not re-indented."""
    original = "cache:  # note\n    enabled: true\n"
    (tmp_path / CONFIG_FILE_NAME).write_text(original)

    _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to(f"{original}workers: 3\n")


def test_whole_mapping_over_a_flat_child_key_says_why(tmp_path: Path) -> None:
    """Replacing a mapping a flat key also sets names that flat key."""
    original = "cache.directory: /x\n"
    (tmp_path / CONFIG_FILE_NAME).write_text(original)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "cache", {"enabled": False})

    assert_that(str(error.value)).contains("cache.directory")
    assert_that(str(error.value)).contains("by hand")
    assert_that(_main_file(tmp_path)).is_equal_to(original)


def test_interrupted_write_leaves_no_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any exception during the write removes the temporary file."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")

    def _interrupt(_fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("winnow.config._writer.os.fsync", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        _set(tmp_path, "workers", 3)

    assert_that(_main_file(tmp_path)).is_equal_to("workers: 2\n")
    assert_that(sorted(path.name for path in tmp_path.iterdir())).is_equal_to(
        [CONFIG_FILE_NAME],
    )


def test_unsupported_marker_value_is_refused(tmp_path: Path) -> None:
    """A value with a marker winnow does not apply fails before writing."""
    (tmp_path / CONFIG_FILE_NAME).write_text("workers: 2\n")

    with pytest.raises(ConfigError):
        _set(tmp_path, "workers", "@format {this.x}")

    assert_that(_main_file(tmp_path)).is_equal_to("workers: 2\n")


def test_invalid_paired_value_is_replaced(tmp_path: Path) -> None:
    """A paired value the model cannot read counts as contradicting the change."""
    (tmp_path / CONFIG_FILE_NAME).write_text("follow_symlinks: maybe\n")

    _set(tmp_path, "symlink_policy", "follow")

    assert_that(_main_file(tmp_path)).is_equal_to(
        "follow_symlinks: true\nsymlink_policy: follow\n",
    )


def test_directory_that_cannot_be_made_is_a_write_error(tmp_path: Path) -> None:
    """A new file under a path that is not a directory fails as a write error."""
    (tmp_path / "f").write_text("")
    config_path = tmp_path / "f" / "c.yaml"

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("write_config")
    assert_that((tmp_path / "f").read_text()).is_equal_to("")
