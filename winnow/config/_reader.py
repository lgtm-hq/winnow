"""Dynaconf file reading, normalization, and validation of raw config data."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import cast

from dynaconf import Dynaconf
from pydantic import ValidationError

from winnow.config._env import _load_env_data, _sync_symlink_env_overrides
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig

_LOGGER = logging.getLogger(__name__)


def _yaml_error_types() -> tuple[type[Exception], ...]:
    """Return the YAMLError classes raised by installed and vendored ruamel.

    Dynaconf parses YAML with its vendored ruamel.yaml, whose ``YAMLError`` is
    unrelated to the installed ruamel.yaml's class, so both must be caught. The
    modules ship no type stubs, hence the runtime lookup.

    Returns:
        YAMLError classes available in this environment.
    """
    error_types: list[type[Exception]] = []
    try:
        error_types.append(
            cast("type[Exception]", import_module("ruamel.yaml.error").YAMLError),
        )
    except ImportError:  # pragma: no cover - ships with dynaconf[yaml]
        pass
    try:
        error_types.append(
            cast(
                "type[Exception]",
                import_module("dynaconf.vendor.ruamel.yaml.error").YAMLError,
            ),
        )
    except ImportError:  # pragma: no cover - ships with dynaconf
        pass
    return tuple(error_types)


_CONFIG_LOAD_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    *_yaml_error_types(),
)

_INTERNAL_DYNACONF_KEYS = frozenset({"LOAD_DOTENV"})
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


def _load_dynaconf_data(
    config_path: Path | None,
    *,
    load_env: bool,
) -> dict[str, object]:
    """Load raw settings through Dynaconf and normalize key casing.

    Args:
        config_path: Optional config file path to load.
        load_env: Whether to include prefixed environment variables.

    Returns:
        Normalized raw configuration mapping.

    Raises:
        ConfigError: If Dynaconf cannot parse the config file.
    """
    settings_files = [str(config_path)] if config_path is not None else []
    try:
        settings = Dynaconf(
            envvar_prefix=None,
            settings_files=settings_files,
            environments=False,
            load_dotenv=False,
            merge_enabled=True,
        )
        raw_data = cast("Mapping[str, object]", settings.as_dict())
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

    user_data = {
        key: value
        for key, value in raw_data.items()
        if key not in _INTERNAL_DYNACONF_KEYS
    }
    normalized_data = _normalize_mapping(user_data)
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
        data: Raw mapping from Dynaconf.

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
        value: Raw value from Dynaconf.

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

    Guards against Dynaconf internals leaking into ``as_dict()`` output across
    Dynaconf versions and against unrelated ``WINNOW_*`` environment variables
    breaking every load under ``extra="forbid"`` validation.

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
