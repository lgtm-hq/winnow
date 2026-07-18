"""Public facade for Winnow configuration loading and mutation.

Implementation lives in sibling modules split by concern: ``_paths`` resolves
config file locations, ``_reader`` loads and normalizes Dynaconf data,
``_env`` overlays ``WINNOW_*`` environment variables, and ``_writer`` handles
set/reset mutation and file writing.
"""

from __future__ import annotations

from pathlib import Path

from winnow.config._paths import _resolve_read_path, find_config_path
from winnow.config._reader import _load_dynaconf_data, validate_config_data
from winnow.config._writer import (
    generate_default_config,
    reset_config,
    set_config_value,
)
from winnow.config.schema import default_config_data
from winnow.models.config import WinnowConfig

__all__ = [
    "find_config_path",
    "generate_default_config",
    "load_config",
    "reset_config",
    "set_config_value",
    "show_config",
    "validate_config",
    "validate_config_data",
]


def load_config(
    config_path: Path | None = None,
    *,
    cwd: Path | None = None,
    home_config_dir: Path | None = None,
    load_env: bool = True,
) -> WinnowConfig:
    """Load and validate Winnow configuration.

    Args:
        config_path: Explicit config path. When omitted, lookup checks the working
            directory before the user config directory.
        cwd: Working directory used for config discovery.
        home_config_dir: User config directory override, primarily for tests.
        load_env: Whether to apply ``WINNOW_*`` environment overrides.

    Returns:
        Validated configuration model.

    Raises:
        ConfigError: If the config file cannot be loaded or validated.
    """
    resolved_path = _resolve_read_path(
        config_path=config_path,
        cwd=cwd,
        home_config_dir=home_config_dir,
    )
    data = _load_dynaconf_data(config_path=resolved_path, load_env=load_env)
    return validate_config_data(data=data, file_path=resolved_path)


def validate_config(
    config_path: Path | None = None,
    *,
    cwd: Path | None = None,
    home_config_dir: Path | None = None,
    load_env: bool = True,
) -> WinnowConfig:
    """Validate a config source and return its parsed model.

    Args:
        config_path: Explicit config path. When omitted, lookup checks default
            locations.
        cwd: Working directory used for config discovery.
        home_config_dir: User config directory override, primarily for tests.
        load_env: Whether to apply ``WINNOW_*`` environment overrides.

    Returns:
        Validated configuration model.

    Raises:
        ConfigError: If the config source is invalid.
    """
    return load_config(
        config_path=config_path,
        cwd=cwd,
        home_config_dir=home_config_dir,
        load_env=load_env,
    )


def show_config(config: WinnowConfig | None = None) -> dict[str, object]:
    """Return a JSON-serializable view of a configuration.

    Args:
        config: Configuration to show, or defaults when omitted.

    Returns:
        Configuration data suitable for display or JSON encoding.
    """
    return default_config_data(config)
