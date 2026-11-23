"""Config mutation: set/reset keys, default generation, and file writing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile

from winnow.config._editor import (
    dump_document,
    load_document,
    set_in_document,
    writable_format,
)
from winnow.config._env import (
    _FOLLOW_SYMLINKS_KEY,
    _SYMLINK_POLICY_KEY,
    _sync_symlink_settings,
)
from winnow.config._files import read_config_layers, read_config_text, read_value
from winnow.config._paths import _resolve_write_path
from winnow.config._reader import (
    _CONFIG_LOAD_ERRORS,
    _KNOWN_TOP_LEVEL_KEYS,
    validate_config_data,
)
from winnow.config.defaults import cwd_config_path
from winnow.config.schema import _yaml_safe, render_config_yaml
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
    """Set a dotted configuration key in the config file and nothing else.

    The file is read as written, the one key is changed where the file sets
    it, and the file is written back in its own format. Values from the
    ``.local`` companion file or the environment, cast results and defaults
    never reach it, and the rest of the file is kept as far as the round trip
    allows. Setting ``follow_symlinks`` or ``symlink_policy`` also sets the
    other when the file would otherwise contradict itself. TOML files are
    refused and left untouched.

    Args:
        key: Dotted config key, such as ``cache.enabled``.
        value: New raw value for the key.
        config_path: Explicit config path to write.
        cwd: Working directory used for config discovery and default writes.
        home_config_dir: User config directory override, primarily for tests.

    Returns:
        The validated configuration the file and its companion now give,
        without environment overrides.

    Raises:
        ConfigError: If the key cannot be set, the file cannot be read or
            written in its format, or the resulting config is invalid.
    """
    target_path = _resolve_write_path(
        config_path=config_path,
        cwd=cwd,
        home_config_dir=home_config_dir,
    )
    file_format = writable_format(target_path)
    _key_parts(key)
    original = (
        _read_text(target_path, operation="load_config")
        if target_path.is_file()
        else None
    )
    try:
        document = load_document(original, file_format)
        current = (
            read_config_text(original, target_path) if original is not None else {}
        )
    except _CONFIG_LOAD_ERRORS as exc:
        raise _load_error(target_path, exc) from exc

    updates = _updates_for(key=key, value=value, current=current)
    for update_key, update_value in updates.items():
        set_in_document(document, _key_parts(update_key), _yaml_safe(update_value))
    new_text = dump_document(document, file_format, original=original)

    updated_config = _validate_new_text(
        new_text,
        target_path=target_path,
        updates=updates,
    )
    if new_text != original:
        _write_text(new_text, config_path=target_path)
    return updated_config


def _key_parts(key: str) -> list[str]:
    """Split a dotted key into its non-empty parts.

    Raises:
        ConfigError: If the key has no parts.
    """
    key_parts = [part for part in key.split(".") if part]
    if not key_parts:
        raise ConfigError(
            "Configuration key cannot be empty",
            operation="set_config",
            details={"key": key},
        )
    return key_parts


def _updates_for(
    *,
    key: str,
    value: object,
    current: Mapping[str, object],
) -> dict[str, object]:
    """Return the keys to write for one ``config set``.

    Args:
        key: The key being set.
        value: Its new value.
        current: The settings the target file alone gives today.

    Returns:
        ``key`` with its value, plus the paired symlink setting when keeping
        the file consistent requires changing it.
    """
    synced = dict(current)
    synced[key] = value
    _sync_symlink_settings(data=synced, key=key, value=value)
    updates: dict[str, object] = {key: synced[key]}
    for paired_key in (_FOLLOW_SYMLINKS_KEY, _SYMLINK_POLICY_KEY):
        if paired_key != key and synced.get(paired_key) != current.get(paired_key):
            updates[paired_key] = synced[paired_key]
    return updates


def _validate_new_text(
    new_text: str,
    *,
    target_path: Path,
    updates: Mapping[str, object],
) -> WinnowConfig:
    """Check new file content before it is written.

    The content must set each updated key to its value when read on its own,
    and give a valid configuration together with the companion file.

    Args:
        new_text: The content about to be written.
        target_path: The file it will be written to.
        updates: The keys set, with their values.

    Returns:
        The validated configuration of the content and its companion.

    Raises:
        ConfigError: If the content does not read back as intended or the
            configuration is invalid.
    """
    try:
        written = read_config_text(new_text, target_path)
        layered = read_config_layers(target_path, main_text=new_text)
        for update_key, update_value in updates.items():
            expected = read_value(_yaml_safe(update_value))
            if _setting_at(written, update_key) != (True, expected):
                raise ConfigError(
                    "config set cannot change this key where the file sets it; "
                    "edit the file by hand",
                    operation="set_config",
                    file_path=target_path,
                    details={"key": update_key},
                )
    except _CONFIG_LOAD_ERRORS as exc:
        raise _load_error(target_path, exc) from exc

    top_keys = {_key_parts(update_key)[0].lower() for update_key in updates}
    known = {
        name: setting
        for name, setting in layered.items()
        if name in _KNOWN_TOP_LEVEL_KEYS or name in top_keys
    }
    return validate_config_data(data=known, file_path=target_path)


def _setting_at(settings: Mapping[str, object], key: str) -> tuple[bool, object]:
    """Look up a dotted key, matched lowercase, in read settings.

    Returns:
        ``(True, value)`` when the key is set, else ``(False, None)``.
    """
    current: object = settings
    for part in _key_parts(key):
        if not isinstance(current, Mapping) or part.lower() not in current:
            return False, None
        current = current[part.lower()]
    return True, current


def _read_text(path: Path, *, operation: str) -> str:
    """Read a config file's text.

    Raises:
        ConfigError: If the file cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise ConfigError(
            "Unable to load Winnow configuration",
            operation=operation,
            file_path=path,
            details={"error": str(exc)},
        ) from exc


def _load_error(path: Path, exc: Exception) -> ConfigError:
    """Wrap a read or parse failure of the target file."""
    return ConfigError(
        "Unable to load Winnow configuration",
        operation="load_config",
        file_path=path,
        details={"error": str(exc)},
    )


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


def _write_config_file(
    config: WinnowConfig | Mapping[str, object],
    config_path: Path,
) -> None:
    """Write validated configuration content as YAML.

    Args:
        config: Configuration model, or a sparse user-data mapping that has
            already been validated.
        config_path: Destination path.
    """
    _write_text(render_config_yaml(config), config_path=config_path)


def _write_text(text: str, *, config_path: Path) -> None:
    """Replace a config file's content atomically.

    Args:
        text: The new content.
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
            temp_file.write(text)
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
