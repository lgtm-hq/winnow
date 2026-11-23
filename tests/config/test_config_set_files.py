"""``config set`` writes the config file it found, and only it (#429).

Symlinks, permission bits, line endings, byte order marks, JSON parse
errors and failed writes: the file is changed in place as it was, or left
untouched with a :class:`ConfigError`.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import CONFIG_FILE_NAME, set_config_value
from winnow.exceptions import ConfigError

_WINDOWS = sys.platform.startswith("win")
_POSIX_MODES = pytest.mark.skipif(_WINDOWS, reason="POSIX mode bits do not apply")


def _set(tmp_path: Path, key: str, value: object) -> None:
    """Run ``set_config_value`` with discovery rooted at ``tmp_path``."""
    set_config_value(key, value, cwd=tmp_path, home_config_dir=tmp_path / "home")


def _main_file(tmp_path: Path) -> str:
    """Return the main config file's text."""
    return (tmp_path / CONFIG_FILE_NAME).read_text()


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


@_POSIX_MODES
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can write read-only files",
)
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


@_POSIX_MODES
def test_existing_permission_bits_are_kept(tmp_path: Path) -> None:
    """The replacement file keeps the permission bits of the file it replaces."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\n")
    config_path.chmod(0o640)

    _set(tmp_path, "workers", 3)

    assert_that(stat.S_IMODE(config_path.stat().st_mode)).is_equal_to(0o640)


@_POSIX_MODES
def test_new_file_mode_is_unchanged(tmp_path: Path) -> None:
    """A new file gets the private mode ``config set`` always gave it."""
    _set(tmp_path, "workers", 3)

    mode = stat.S_IMODE((tmp_path / CONFIG_FILE_NAME).stat().st_mode)
    assert_that(mode).is_equal_to(0o600)


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


def test_empty_json_file_is_a_load_error(tmp_path: Path) -> None:
    """An empty JSON file is invalid JSON, as the reader has always said."""
    config_path = tmp_path / "c.json"
    config_path.write_text("")

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that(config_path.read_text()).is_equal_to("")


def test_yaml_parse_error_names_the_file(tmp_path: Path) -> None:
    """A YAML parse error names the config file."""
    config_path = tmp_path / CONFIG_FILE_NAME
    config_path.write_text("workers: 2\nworkers: 3\n")

    with pytest.raises(ConfigError) as error:
        _set(tmp_path, "dry_run", True)

    assert_that(str(error.value.context.details)).contains(str(config_path))


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


def test_directory_that_cannot_be_made_is_a_write_error(tmp_path: Path) -> None:
    """A new file under a path that is not a directory fails as a write error."""
    (tmp_path / "f").write_text("")
    config_path = tmp_path / "f" / "c.yaml"

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("write_config")
    assert_that((tmp_path / "f").read_text()).is_equal_to("")


@pytest.mark.parametrize("links", [["a.yaml"], ["a.yaml", "b.yaml"]])
def test_symlink_loop_is_refused(tmp_path: Path, links: list[str]) -> None:
    """A link that loops back on itself is refused and stays a link."""
    paths = [tmp_path / name for name in links]
    for path, target in zip(paths, [*paths[1:], paths[0]], strict=True):
        path.symlink_to(target)

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=paths[0])

    assert_that(str(error.value)).contains("symlink loop")
    assert_that(all(path.is_symlink() for path in paths)).is_true()
    assert_that(sorted(path.name for path in tmp_path.iterdir())).is_equal_to(links)


def test_dangling_symlink_creates_its_target(tmp_path: Path) -> None:
    """A link to a missing file keeps the link and creates the file it names."""
    link = tmp_path / "c.yaml"
    target = tmp_path / "real" / "settings.yaml"
    link.symlink_to(target)

    set_config_value("workers", 3, config_path=link)

    assert_that(link.is_symlink()).is_true()
    assert_that(target.read_text()).contains("workers: 3")


@_POSIX_MODES
def test_special_mode_bits_are_not_copied(tmp_path: Path) -> None:
    """Only the permission bits are kept; setuid is dropped."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("workers: 2\n")
    config_path.chmod(0o4750)

    set_config_value("workers", 3, config_path=config_path)

    assert_that(stat.S_IMODE(config_path.stat().st_mode)).is_equal_to(0o750)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("﻿workers: 2\n", "﻿workers: 3\n"),
        ("﻿# Winnow settings\n", "﻿# Winnow settings\nworkers: 3\n"),
    ],
)
def test_yaml_byte_order_mark_is_kept(
    tmp_path: Path,
    before: str,
    after: str,
) -> None:
    """A YAML file starting with a BOM keeps it, and its comments."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text(before, encoding="utf-8")

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(after)


def test_json_byte_order_mark_is_kept(tmp_path: Path) -> None:
    """A JSON file with a BOM, which the reader refuses, is left as it is."""
    config_path = tmp_path / "c.json"
    original = '﻿{"workers": 2}\n'.encode()
    config_path.write_bytes(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(error.value.context.operation).is_equal_to("load_config")
    assert_that(config_path.read_bytes()).is_equal_to(original)


@pytest.mark.parametrize(
    ("name", "original"),
    [
        ("c.yaml", b"workers: 2\r\ndry_run: false\n"),
        ("c.yaml", b"workers: 2\rdry_run: false\r"),
        ("c.yaml", b"workers: 2\r\ndry_run: false\r"),
        ("c.json", b'{\r\n  "workers": 2\n}\n'),
    ],
)
def test_mixed_line_endings_are_refused(
    tmp_path: Path,
    name: str,
    original: bytes,
) -> None:
    """Line ends that cannot be kept faithfully leave the file untouched."""
    config_path = tmp_path / name
    config_path.write_bytes(original)

    with pytest.raises(ConfigError) as error:
        set_config_value("workers", 3, config_path=config_path)

    assert_that(str(error.value)).contains("line endings")
    assert_that(config_path.read_bytes()).is_equal_to(original)


def test_next_line_character_ends_a_line_as_the_parser_reads_it(
    tmp_path: Path,
) -> None:
    """U+0085 ends a comment line, so the key after it is set in place."""
    config_path = tmp_path / "c.yaml"
    config_path.write_text("# c\x85workers: 2\n", encoding="utf-8")

    set_config_value("workers", 3, config_path=config_path)

    assert_that(config_path.read_text(encoding="utf-8")).is_equal_to(
        "# c\x85workers: 3\n",
    )
