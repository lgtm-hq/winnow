"""Config-path discovery and resolution for reads and writes."""

from __future__ import annotations

from pathlib import Path

from winnow.config.defaults import cwd_config_path, user_config_path
from winnow.exceptions import ConfigError


def find_config_path(
    *,
    cwd: Path | None = None,
    home_config_dir: Path | None = None,
) -> Path | None:
    """Find the first Winnow configuration file in lookup order.

    Args:
        cwd: Working directory to inspect before the user config directory.
        home_config_dir: User config directory override, primarily for tests.

    Returns:
        Existing config path, or None when no config file is present.
    """
    cwd_path = cwd_config_path(cwd)
    if cwd_path.is_file():
        return cwd_path

    user_path = user_config_path(home_config_dir)
    if user_path.is_file():
        return user_path

    return None


def _resolve_read_path(
    config_path: Path | None,
    cwd: Path | None,
    home_config_dir: Path | None,
) -> Path | None:
    """Resolve the config path used for reads.

    Args:
        config_path: Explicit config path.
        cwd: Working directory used for discovery.
        home_config_dir: User config directory override.

    Returns:
        Existing config path, or None when no file should be read.

    Raises:
        ConfigError: If an explicit config path does not exist.
    """
    if config_path is None:
        return find_config_path(cwd=cwd, home_config_dir=home_config_dir)
    if not config_path.is_file():
        raise ConfigError(
            "Winnow configuration file not found",
            operation="load_config",
            file_path=config_path,
        )
    return config_path


def _resolve_write_path(
    config_path: Path | None,
    cwd: Path | None,
    home_config_dir: Path | None,
) -> Path:
    """Resolve the config path used for writes.

    Args:
        config_path: Explicit config path.
        cwd: Working directory used for discovery and default writes.
        home_config_dir: User config directory override.

    Returns:
        Path that should be written.
    """
    if config_path is not None:
        return config_path
    discovered_path = find_config_path(cwd=cwd, home_config_dir=home_config_dir)
    if discovered_path is not None:
        return discovered_path
    return cwd_config_path(cwd)
