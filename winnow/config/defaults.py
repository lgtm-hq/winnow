"""Defaults and filesystem locations for Winnow configuration."""

from __future__ import annotations

from pathlib import Path

CONFIG_FILE_NAME = ".winnow-config.yaml"
ENVVAR_PREFIX = "WINNOW"


def cwd_config_path(cwd: Path | None = None) -> Path:
    """Return the config path for a working directory.

    Args:
        cwd: Directory to resolve from, or the current working directory.

    Returns:
        Expected working-directory config path.
    """
    return (cwd if cwd is not None else Path.cwd()) / CONFIG_FILE_NAME


def user_config_dir() -> Path:
    """Return the per-user Winnow config directory.

    Returns:
        XDG-style user configuration directory for Winnow.
    """
    return Path.home() / ".config" / "winnow"


def user_config_path(config_dir: Path | None = None) -> Path:
    """Return the per-user Winnow config file path.

    Args:
        config_dir: User config directory override, primarily for tests.

    Returns:
        Expected per-user config path.
    """
    return (
        config_dir if config_dir is not None else user_config_dir()
    ) / CONFIG_FILE_NAME
