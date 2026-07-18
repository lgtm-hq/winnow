"""Config mutation: set/reset keys, default generation, and file writing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile

from winnow.config._env import _sync_symlink_settings
from winnow.config._paths import _resolve_write_path
from winnow.config._reader import _load_dynaconf_data, validate_config_data
from winnow.config.defaults import cwd_config_path
from winnow.config.schema import render_config_yaml
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig


def generate_default_config(
    config_path: Path | None = None,
    *,
    cwd: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Write a default config file for first-run setup.

    Args:
        config_path: Destination path. Defaults to ``.winnow-config.yaml`` in
            ``cwd``.
        cwd: Working directory used when ``config_path`` is omitted.
        overwrite: Whether to replace an existing file.

    Returns:
        Path to the generated config file.

    Raises:
        ConfigError: If the destination exists and overwrite is false, or the
            file cannot be written.
    """
    target_path = config_path if config_path is not None else cwd_config_path(cwd)
    if target_path.exists() and not overwrite:
        raise ConfigError(
            "Winnow configuration already exists",
            operation="generate_config",
            file_path=target_path,
        )
    _write_config_file(config=WinnowConfig(), config_path=target_path)
    return target_path


def set_config_value(
    key: str,
    value: object,
    *,
    config_path: Path | None = None,
    cwd: Path | None = None,
    home_config_dir: Path | None = None,
) -> WinnowConfig:
    """Set a dotted configuration key and persist the validated config.

    Only the user's sparse configuration data is written back: existing keys in
    the file are preserved as-is and defaults are never materialized into it.

    Args:
        key: Dotted config key, such as ``cache.enabled``.
        value: New raw value for the key.
        config_path: Explicit config path to write.
        cwd: Working directory used for config discovery and default writes.
        home_config_dir: User config directory override, primarily for tests.

    Returns:
        Persisted validated configuration model.

    Raises:
        ConfigError: If the key cannot be set or the resulting config is invalid.
    """
    target_path = _resolve_write_path(
        config_path=config_path,
        cwd=cwd,
        home_config_dir=home_config_dir,
    )
    user_data: dict[str, object] = (
        _load_dynaconf_data(config_path=target_path, load_env=False)
        if target_path.is_file()
        else {}
    )
    _set_nested_value(data=user_data, dotted_key=key, value=value)
    _sync_symlink_settings(data=user_data, key=key, value=value)
    updated_config = validate_config_data(data=user_data, file_path=target_path)
    _write_config_file(config=user_data, config_path=target_path)
    return updated_config


def reset_config(
    config_path: Path | None = None,
    *,
    cwd: Path | None = None,
    home_config_dir: Path | None = None,
) -> WinnowConfig:
    """Reset a config file to validated defaults.

    Args:
        config_path: Explicit config path to reset.
        cwd: Working directory used for config discovery and default writes.
        home_config_dir: User config directory override, primarily for tests.

    Returns:
        Default configuration model written to disk.

    Raises:
        ConfigError: If the config cannot be written.
    """
    target_path = _resolve_write_path(
        config_path=config_path,
        cwd=cwd,
        home_config_dir=home_config_dir,
    )
    config = WinnowConfig()
    _write_config_file(config=config, config_path=target_path)
    return config


def _set_nested_value(
    data: dict[str, object],
    dotted_key: str,
    value: object,
) -> None:
    """Set a dotted key inside a nested dictionary.

    Args:
        data: Configuration data to mutate.
        dotted_key: Dot-separated key path.
        value: Value to assign.

    Raises:
        ConfigError: If the dotted key is empty or traverses a scalar value.
    """
    key_parts = [part for part in dotted_key.split(".") if part]
    if not key_parts:
        raise ConfigError(
            "Configuration key cannot be empty",
            operation="set_config",
            details={"key": dotted_key},
        )

    current = data
    for key_part in key_parts[:-1]:
        child = current.get(key_part)
        if child is None:
            child = {}
            current[key_part] = child
        if not isinstance(child, dict):
            raise ConfigError(
                "Configuration key traverses a non-object value",
                operation="set_config",
                details={"key": dotted_key, "segment": key_part},
            )
        current = child
    current[key_parts[-1]] = value


def _write_config_file(
    config: WinnowConfig | Mapping[str, object],
    config_path: Path,
) -> None:
    """Write validated configuration content as YAML.

    Args:
        config: Configuration model, or a sparse user-data mapping that has
            already been validated.
        config_path: Destination path.

    Raises:
        ConfigError: If the file cannot be written.
    """
    temp_path: Path | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            dir=config_path.parent,
            encoding="utf-8",
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(render_config_yaml(config))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(config_path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise ConfigError(
            "Unable to write Winnow configuration",
            operation="write_config",
            file_path=config_path,
            details={"error": str(exc)},
        ) from exc
