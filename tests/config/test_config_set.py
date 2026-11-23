"""``config set`` edits one key of the config file as the user wrote it (#429).

The first four tests started as characterizations of the bug on ``main``
(companion values copied into the file, merged lists growing, cast results
written back, YAML written into a ``.toml`` file); each passed there before
being turned into the behaviour #429 requires.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import CONFIG_FILE_NAME, load_config, set_config_value
from winnow.config._editor import load_json, set_in_document
from winnow.config._writer import _parse_new_text, _refuse_other_changes
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


def test_json_null_file_takes_the_key(tmp_path: Path) -> None:
    """A JSON file holding ``null`` reads as empty and gets the key."""
    config_path = tmp_path / "c.json"
    config_path.write_text("null\n")

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to('{"workers": 3}\n')


@pytest.mark.parametrize(
    ("original", "key", "message"),
    [
        ('{"routing": "@json {}"}', "routing.review", "cast string"),
        ('{"workers": 2}', "workers.x", "non-object"),
    ],
)
def test_json_key_below_a_non_mapping_is_refused(
    tmp_path: Path,
    original: str,
    key: str,
    message: str,
) -> None:
    """A JSON key below a cast string or a scalar is refused, file untouched."""
    config_path = tmp_path / "c.json"
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value(key, "X", config_path=config_path)

    assert_that(str(error.value)).contains(message)
    assert_that(config_path.read_text()).is_equal_to(original)


def test_json_top_level_must_be_an_object() -> None:
    """A JSON array cannot take a key."""
    with pytest.raises(ValueError, match="top level"):
        load_json("[1]")


def test_change_to_another_setting_is_refused(tmp_path: Path) -> None:
    """The structural check refuses content that changes any other key."""
    config_path = tmp_path / "c.yaml"

    with pytest.raises(ConfigError) as error:
        _refuse_other_changes(
            {"workers": 2, "dry_run": True},
            {"workers": 3, "dry_run": False},
            config_path,
            {"workers": 3},
        )

    assert_that(str(error.value)).contains("other settings")


def test_unreadable_new_content_is_an_unsafe_edit(tmp_path: Path) -> None:
    """New content that does not parse is the edit's failure, not the file's."""
    with pytest.raises(ConfigError) as error:
        _parse_new_text("workers: [\n", target_path=tmp_path / "c.yaml")

    assert_that(error.value.message).contains("could not make this edit safely")
    assert_that(error.value.message).does_not_contain("Unable to load")
