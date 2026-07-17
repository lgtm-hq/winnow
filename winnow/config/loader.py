"""Dynaconf-backed loading and mutation helpers for Winnow configuration."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from dynaconf import Dynaconf
from dynaconf.utils.parse_conf import boolean_fix, parse_conf_data
from pydantic import TypeAdapter, ValidationError

from winnow.config.defaults import (
    ENVVAR_PREFIX,
    cwd_config_path,
    user_config_path,
)
from winnow.config.schema import default_config_data, render_config_yaml
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig
from winnow.models.enums import SymlinkPolicy

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
_ENVVAR_PREFIX = f"{ENVVAR_PREFIX}_"
_FOLLOW_SYMLINKS_KEY = "follow_symlinks"
_SYMLINK_POLICY_KEY = "symlink_policy"
_BOOL_ADAPTER = TypeAdapter(bool)
_SYMLINK_POLICY_ADAPTER = TypeAdapter(SymlinkPolicy)


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


def show_config(config: WinnowConfig | None = None) -> dict[str, object]:
    """Return a JSON-serializable view of a configuration.

    Args:
        config: Configuration to show, or defaults when omitted.

    Returns:
        Configuration data suitable for display or JSON encoding.
    """
    return default_config_data(config)


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


def _load_env_data(
    environ: Mapping[str, str],
) -> tuple[dict[str, object], frozenset[str]]:
    """Load Winnow environment overrides from an explicit environment snapshot.

    Args:
        environ: Environment mapping to inspect.

    Returns:
        Parsed override data and explicitly provided top-level setting keys.
    """
    data: dict[str, object] = {}
    explicit_keys: set[str] = set()
    for env_key, env_value in environ.items():
        if not env_key.startswith(_ENVVAR_PREFIX):
            continue
        setting_key = env_key.removeprefix(_ENVVAR_PREFIX)
        key_parts = [part.lower() for part in setting_key.split("__") if part]
        if not key_parts:
            continue
        if len(key_parts) == 1:
            explicit_keys.add(key_parts[0])
        parsed_value = parse_conf_data(boolean_fix(env_value), tomlfy=True)
        _set_env_value(data=data, key_parts=key_parts, value=parsed_value)

    return data, frozenset(explicit_keys)


def _set_env_value(
    data: dict[str, object],
    key_parts: list[str],
    value: object,
) -> None:
    """Set a parsed environment value into nested config data."""
    current = data
    for key_part in key_parts[:-1]:
        child = current.get(key_part)
        if not isinstance(child, dict):
            child = {}
            current[key_part] = child
        current = child
    current[key_parts[-1]] = value


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


def _sync_symlink_env_overrides(
    data: dict[str, object],
    explicit_env_keys: frozenset[str],
) -> None:
    """Reconcile legacy symlink settings when only one env override is present."""
    has_follow_override = _FOLLOW_SYMLINKS_KEY in explicit_env_keys
    has_policy_override = _SYMLINK_POLICY_KEY in explicit_env_keys
    if has_follow_override == has_policy_override:
        return
    if has_follow_override:
        _sync_symlink_settings(
            data=data,
            key=_FOLLOW_SYMLINKS_KEY,
            value=data.get(_FOLLOW_SYMLINKS_KEY),
        )
        return
    _sync_symlink_settings(
        data=data,
        key=_SYMLINK_POLICY_KEY,
        value=data.get(_SYMLINK_POLICY_KEY),
    )


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


def _sync_symlink_settings(
    data: dict[str, object],
    key: str,
    value: object,
) -> None:
    """Keep legacy and policy symlink settings consistent during single-key sets."""
    if key == _FOLLOW_SYMLINKS_KEY:
        try:
            follow_symlinks = _BOOL_ADAPTER.validate_python(value)
        except ValidationError:
            return
        data[_FOLLOW_SYMLINKS_KEY] = follow_symlinks
        if follow_symlinks:
            data[_SYMLINK_POLICY_KEY] = SymlinkPolicy.FOLLOW.value
            return
        if data.get(_SYMLINK_POLICY_KEY) == SymlinkPolicy.FOLLOW.value:
            data[_SYMLINK_POLICY_KEY] = SymlinkPolicy.SKIP.value
        return

    if key == _SYMLINK_POLICY_KEY:
        try:
            policy = _SYMLINK_POLICY_ADAPTER.validate_python(value)
        except ValidationError:
            return
        data[_SYMLINK_POLICY_KEY] = policy.value
        data[_FOLLOW_SYMLINKS_KEY] = policy is SymlinkPolicy.FOLLOW


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
