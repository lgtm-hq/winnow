"""Config file reading, normalization, and validation of raw config data."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml.error import YAMLError

from winnow.config._env import (
    _load_env_data,
    _sync_symlink_env_overrides,
    warn_about_dynaconf_variables,
)
from winnow.config._files import read_config_layers
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig

_LOGGER = logging.getLogger(__name__)

_CONFIG_LOAD_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    YAMLError,
)

_KNOWN_TOP_LEVEL_KEYS = frozenset(WinnowConfig.model_fields)


def validate_config_data(
    data: Mapping[str, object],
    *,
    file_path: Path | None = None,
) -> WinnowConfig:
    """Validate raw configuration data with Pydantic.

    Args:
        data: Raw configuration data.
        file_path: Related file path for error context.

    Returns:
        Validated configuration model.

    Raises:
        ConfigError: If Pydantic rejects the configuration data.
    """
    try:
        return WinnowConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            "Invalid Winnow configuration",
            operation="validate_config",
            file_path=file_path,
            details={"errors": exc.errors(include_url=False)},
        ) from exc


def _load_config_data(
    config_path: Path | None,
    *,
    load_env: bool,
) -> dict[str, object]:
    """Load raw settings from the config file and environment.

    The config file is read with its ``.local`` companion, flat keys and cast
    markers as :mod:`winnow.config._files` describes.

    Args:
        config_path: Optional config file path to load.
        load_env: Whether to include prefixed environment variables.

    Returns:
        Normalized raw configuration mapping.

    Raises:
        ConfigError: If the config file cannot be read or parsed, or an
            environment value cannot be cast.
    """
    warn_about_dynaconf_variables(os.environ)
    try:
        raw_data = read_config_layers(config_path) if config_path is not None else {}
        env_data, explicit_env_keys = (
            _load_env_data(dict(os.environ)) if load_env else ({}, frozenset())
        )
    except _CONFIG_LOAD_ERRORS as exc:
        raise ConfigError(
            "Unable to load Winnow configuration",
            operation="load_config",
            file_path=config_path,
            details={"error": str(exc)},
        ) from exc

    normalized_data = _normalize_mapping(raw_data)
    _merge_mapping(normalized_data, env_data)
    _sync_symlink_env_overrides(normalized_data, explicit_env_keys)
    return _drop_unknown_keys(data=normalized_data, file_path=config_path)


def _merge_mapping(
    data: dict[str, object],
    overrides: Mapping[str, object],
) -> None:
    """Merge overrides into config data, preserving unrelated nested keys."""
    for key, value in overrides.items():
        existing_value = data.get(key)
        if isinstance(existing_value, dict) and isinstance(value, Mapping):
            _merge_mapping(existing_value, value)
            continue
        data[key] = value


def _normalize_mapping(data: Mapping[str, object]) -> dict[str, object]:
    """Lowercase mapping keys recursively.

    Args:
        data: Raw mapping read from the config file.

    Returns:
        Mapping with lowercase string keys.
    """
    normalized: dict[str, object] = {}
    for key, value in data.items():
        normalized[str(key).lower()] = _normalize_value(value)
    return normalized


def _normalize_value(value: object) -> object:
    """Normalize nested config values.

    Args:
        value: Raw value read from the config file.

    Returns:
        Normalized value.
    """
    if isinstance(value, Mapping):
        return _normalize_mapping(value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _drop_unknown_keys(
    data: dict[str, object],
    *,
    file_path: Path | None,
) -> dict[str, object]:
    """Drop top-level keys that are not Winnow configuration fields.

    Keeps stray keys in a config file and unrelated ``WINNOW_*`` environment
    variables from breaking every load under ``extra="forbid"`` validation.

    Args:
        data: Normalized configuration mapping.
        file_path: Source file path used in the warning message.

    Returns:
        Mapping restricted to known top-level configuration keys.
    """
    known_data = {
        key: value for key, value in data.items() if key in _KNOWN_TOP_LEVEL_KEYS
    }
    dropped_keys = sorted(set(data) - set(known_data))
    if dropped_keys:
        _LOGGER.warning(
            "Ignoring unknown configuration keys %s from %s",
            dropped_keys,
            file_path if file_path is not None else "environment",
        )
    return known_data
