"""``config set`` writes the value given and nothing more (#429).

Values the format cannot hold, values already set, cast strings, flat keys
and the paired symlink settings.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from assertpy import assert_that
from click.testing import CliRunner

from winnow.cli import main
from winnow.cli.errors import ExitCode
from winnow.config import CONFIG_FILE_NAME, set_config_value
from winnow.exceptions import ConfigError
from winnow.models.enums import SymlinkPolicy


def _set(tmp_path: Path, key: str, value: object) -> None:
    """Run ``set_config_value`` with discovery rooted at ``tmp_path``."""
    set_config_value(key, value, cwd=tmp_path, home_config_dir=tmp_path / "home")


def _main_file(tmp_path: Path) -> str:
    """Return the main config file's text."""
    return (tmp_path / CONFIG_FILE_NAME).read_text()


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {"x": [float("nan")]},
        ("a", float("nan")),
        {"x": (float("inf"),)},
    ],
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


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_unserializable_value_is_a_config_error(tmp_path: Path, suffix: str) -> None:
    """A value the format cannot hold fails as ``ConfigError``, file untouched."""
    config_path = tmp_path / f"c{suffix}"
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


@pytest.mark.parametrize(
    ("name", "text", "key", "value"),
    [
        ("c.json", '{"workers":2}', "workers", 2),
        ("c.yaml", "workers: 2", "workers", 2),
        ("c.yaml", "cache: {enabled: true}\n", "cache.enabled", True),
        ("c.yaml", 'workers: "@int 4"\n', "workers", "@int 4"),
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
    os.utime(config_path, ns=(1_000_000_000, 1_000_000_000))
    before = config_path.stat()

    set_config_value(key, value, config_path=config_path)

    after = config_path.stat()
    assert_that(config_path.read_text()).is_equal_to(text)
    assert_that(after.st_mtime_ns).is_equal_to(1_000_000_000)
    assert_that(after.st_ino).is_equal_to(before.st_ino)


def test_equal_value_of_another_type_is_written(tmp_path: Path) -> None:
    """``1`` is not the same value as ``true``, so the file changes."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("dry_run: 1\n")

    set_config_value("dry_run", True, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to("dry_run: true\n")


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


def test_whole_mapping_over_a_flat_child_key_says_why(tmp_path: Path) -> None:
    """Replacing a mapping a flat key also sets names that flat key."""
    original = "cache.directory: /x\n"
    (tmp_path / CONFIG_FILE_NAME).write_text(original)

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "cache", {"enabled": False})

    assert_that(str(error.value)).contains("cache.directory")
    assert_that(str(error.value)).contains("by hand")
    assert_that(_main_file(tmp_path)).is_equal_to(original)


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


def test_later_entry_that_reads_back_another_type_is_refused(tmp_path: Path) -> None:
    """A write that would read back as ``1`` where ``true`` was asked is refused."""
    config_path = tmp_path / "c.yaml"
    original = "cache.enabled: 1\ncache__enabled: 1\nCACHE.ENABLED: 0\n"
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("cache.enabled", True, config_path=config_path)

    assert_that(str(error.value)).contains("by hand")
    assert_that(config_path.read_text()).is_equal_to(original)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (
            "FOLLOW_SYMLINKS",
            True,
            "workers: 2\nFOLLOW_SYMLINKS: true\nsymlink_policy: follow\n",
        ),
        (
            "SYMLINK_POLICY",
            "follow",
            "workers: 2\nSYMLINK_POLICY: follow\nfollow_symlinks: true\n",
        ),
    ],
)
def test_uppercase_paired_key_sets_its_pair(
    tmp_path: Path,
    key: str,
    value: object,
    expected: str,
) -> None:
    """Paired symlink keys match whatever their letter case."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 2\n")

    config = set_config_value(key, value, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(expected)
    assert_that(config.symlink_policy).is_equal_to(SymlinkPolicy.FOLLOW)


@pytest.mark.parametrize(
    ("before", "value", "after"),
    [
        ("workers: 4\n", "@int 4", "workers: '@int 4'\n"),
        ('workers: "@int 4"\n', 4, "workers: 4\n"),
    ],
)
def test_value_in_another_written_form_is_written(
    tmp_path: Path,
    before: str,
    value: object,
    after: str,
) -> None:
    """A value that casts to the one set, but is written differently, is written."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(before)

    set_config_value("workers", value, config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(after)


def test_earlier_cast_string_that_joins_the_value_is_refused(tmp_path: Path) -> None:
    """The key must hold the value set when read with casts applied, too."""
    config_path = tmp_path / "c.yaml"
    original = (
        "paths:\n"
        "  include_patterns: '@json [\"*.jpg\"]'\n"
        'paths.include_patterns: ["*.png"]\n'
    )
    config_path.write_text(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("paths.include_patterns", ["*.gif"], config_path=config_path)

    assert_that(str(error.value)).contains("cannot change this key")
    assert_that(config_path.read_text()).is_equal_to(original)


def test_tuple_is_written_as_a_list(tmp_path: Path) -> None:
    """A tuple value is set as the list it holds."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 2\n")

    set_config_value("source_dirs", ("a", "b"), config_path=config_path)

    assert_that(config_path.read_text()).is_equal_to(
        "workers: 2\nsource_dirs:\n  - a\n  - b\n",
    )
