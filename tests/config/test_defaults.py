"""Tests for per-user directory resolution in :mod:`winnow.config.defaults`."""

from __future__ import annotations

from pathlib import Path

import pytest
from assertpy import assert_that

from winnow.config import load_config, user_config_dir, user_data_dir
from winnow.config.defaults import (
    CONFIG_DIR_ENVVAR,
    DATA_DIR_ENVVAR,
    XDG_CONFIG_HOME_ENVVAR,
    XDG_DATA_HOME_ENVVAR,
)

_ALL_ENVVARS = (
    CONFIG_DIR_ENVVAR,
    DATA_DIR_ENVVAR,
    XDG_CONFIG_HOME_ENVVAR,
    XDG_DATA_HOME_ENVVAR,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at a scratch directory and clear every override.

    Args:
        tmp_path: Per-test scratch directory.
        monkeypatch: Pytest environment patcher.

    Returns:
        The patched home directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in _ALL_ENVVARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_user_data_dir_defaults_under_home(home: Path) -> None:
    """Without overrides the data directory is ``~/.local/share/winnow``."""
    assert_that(user_data_dir()).is_equal_to(home / ".local" / "share" / "winnow")


def test_user_data_dir_honours_xdg_data_home(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``XDG_DATA_HOME`` is used with ``winnow`` appended."""
    monkeypatch.setenv(XDG_DATA_HOME_ENVVAR, str(home / "xdg"))

    assert_that(user_data_dir()).is_equal_to(home / "xdg" / "winnow")


def test_user_data_dir_prefers_winnow_data_dir(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WINNOW_DATA_DIR`` wins over ``XDG_DATA_HOME`` and is used verbatim."""
    monkeypatch.setenv(XDG_DATA_HOME_ENVVAR, str(home / "xdg"))
    monkeypatch.setenv(DATA_DIR_ENVVAR, str(home / "override"))

    assert_that(user_data_dir()).is_equal_to(home / "override")


def test_user_data_dir_expands_user_in_override(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``~`` in ``WINNOW_DATA_DIR`` expands to the current home."""
    monkeypatch.setenv(DATA_DIR_ENVVAR, "~/state")

    assert_that(user_data_dir()).is_equal_to(home / "state")


def test_user_data_dir_reads_environment_at_call_time(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive calls observe environment changes made between them."""
    before = user_data_dir()
    monkeypatch.setenv(DATA_DIR_ENVVAR, str(home / "later"))

    assert_that(before).is_not_equal_to(user_data_dir())
    assert_that(user_data_dir()).is_equal_to(home / "later")


def test_user_config_dir_defaults_under_home(home: Path) -> None:
    """Without overrides the config directory is ``~/.config/winnow``."""
    assert_that(user_config_dir()).is_equal_to(home / ".config" / "winnow")


def test_user_config_dir_honours_xdg_config_home(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``XDG_CONFIG_HOME`` is used with ``winnow`` appended."""
    monkeypatch.setenv(XDG_CONFIG_HOME_ENVVAR, str(home / "xdg-config"))

    assert_that(user_config_dir()).is_equal_to(home / "xdg-config" / "winnow")


def test_user_config_dir_prefers_winnow_config_dir(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WINNOW_CONFIG_DIR`` wins over ``XDG_CONFIG_HOME``."""
    monkeypatch.setenv(XDG_CONFIG_HOME_ENVVAR, str(home / "xdg-config"))
    monkeypatch.setenv(CONFIG_DIR_ENVVAR, str(home / "config-override"))

    assert_that(user_config_dir()).is_equal_to(home / "config-override")


def test_directory_envvars_are_not_settings_overrides(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The directory variables never surface as unknown configuration keys."""
    monkeypatch.setenv(DATA_DIR_ENVVAR, str(home / "override"))
    monkeypatch.setenv(CONFIG_DIR_ENVVAR, str(home / "config-override"))

    with caplog.at_level("WARNING", logger="winnow.config"):
        load_config(config_path=None)

    assert_that(caplog.text).does_not_contain("data_dir").does_not_contain(
        "config_dir",
    )
