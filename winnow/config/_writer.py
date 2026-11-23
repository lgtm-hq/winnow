"""Config mutation: set/reset keys, default generation, and file writing."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Final

from pydantic import TypeAdapter, ValidationError
from ruamel.yaml.error import YAMLError

from winnow.config._editor import (
    TextLayout,
    check_finite,
    dump_document,
    load_json,
    set_in_document,
    split_layout,
    writable_format,
)
from winnow.config._env import (
    _FOLLOW_SYMLINKS_KEY,
    _SYMLINK_POLICY_KEY,
    _sync_symlink_settings,
)
from winnow.config._files import (
    ConfigFormat,
    read_config_layers,
    read_config_text,
    read_value,
)
from winnow.config._paths import _resolve_write_path
from winnow.config._reader import (
    _CONFIG_LOAD_ERRORS,
    _KNOWN_TOP_LEVEL_KEYS,
    validate_config_data,
)
from winnow.config._yaml_splice import plan_edit
from winnow.config.defaults import cwd_config_path
from winnow.config.schema import _yaml_safe, render_config_yaml
from winnow.exceptions import ConfigError
from winnow.models.config import WinnowConfig
from winnow.models.enums import SymlinkPolicy

_PAIRED_ADAPTERS: dict[str, TypeAdapter[object]] = {
    _FOLLOW_SYMLINKS_KEY: TypeAdapter(bool),
    _SYMLINK_POLICY_KEY: TypeAdapter(SymlinkPolicy),
}
"""How the model reads each paired symlink setting."""
_PERMISSION_BITS: Final = 0o777
"""The mode bits a replaced file keeps; setuid, setgid and sticky are not."""


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

    The file is read as written and the one key is changed where the file
    sets it. In a YAML file only the text of the key's value is replaced, or
    the new key's lines inserted, and the rest of the file is never
    rewritten. A JSON file is written back as JSON. Values from the
    ``.local`` companion file or the environment, cast results and defaults
    never reach the file. Setting ``follow_symlinks`` or ``symlink_policy``
    also sets the other only when the file would otherwise contradict
    itself. When the file already sets the key to the value as written,
    nothing is written. A symlinked file is written through its link, and
    the file keeps its permission bits but not setuid, setgid or sticky
    bits. Edits that cannot be made that way are refused and the file is
    left untouched: TOML files, files the user cannot write, symlink loops,
    mixed line ends, non-finite numbers, values the format cannot hold,
    several YAML documents, keys an alias or merge key shares, values that
    hold an anchor used elsewhere, comments a replaced value would drop or
    move, and keys a cast string or a flat key holds.

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
    value = _tuples_as_lists(value)
    check_finite(value, key=key)
    _refuse_unresolvable_link(target_path)
    write_path = Path(os.path.realpath(target_path))
    raw = (
        _read_text(target_path, operation="load_config")
        if target_path.is_file()
        else None
    )
    mode = None if raw is None else _writable_mode(write_path)
    original, layout = (
        (None, TextLayout()) if raw is None else split_layout(raw, path=target_path)
    )
    as_read = None if original is None else layout.apply(original)
    try:
        document = load_json(original) if file_format is ConfigFormat.JSON else {}
        current = {} if as_read is None else read_config_text(as_read, target_path)
        as_written = (
            {}
            if as_read is None
            else read_config_text(as_read, target_path, cast=False)
        )
    except _CONFIG_LOAD_ERRORS as exc:
        raise _load_error(target_path, exc) from exc

    updates = _updates_for(key=key, value=value, current=current)
    if raw is not None and all(
        _already_set(as_written, update_key, update_value)
        for update_key, update_value in updates.items()
    ):
        return _validate_new_text(raw, target_path=target_path, updates=updates)
    edited = _edited_text(
        original,
        document,
        file_format,
        updates,
        target_path=target_path,
    )
    new_text = layout.apply(edited)
    updated_config = _validate_new_text(
        new_text,
        target_path=target_path,
        updates=updates,
    )
    if as_read is not None:
        _refuse_other_changes(as_read, new_text, target_path, updates)
    _write_text(new_text, config_path=write_path, mode=mode, newline="")
    return updated_config


def _edited_text(
    original: str | None,
    document: MutableMapping[str, object],
    file_format: ConfigFormat,
    updates: Mapping[str, object],
    *,
    target_path: Path,
) -> str:
    """Return a config file's content with the updates made.

    A YAML file is spliced, one key after the other: only each key's value
    text changes. A JSON or new file is rendered from ``document``.

    Args:
        original: The file's content, ``\\n`` line ends and no BOM, or
            ``None`` for a new file.
        document: A JSON file's parsed content, or an empty mapping.
        file_format: The file's format.
        updates: The keys to set, with their values.
        target_path: The file, for errors.

    Raises:
        ConfigError: If a key cannot be set, or the format cannot hold a
            value.
    """
    try:
        if file_format is ConfigFormat.YAML and original is not None:
            edited = original
            for update_key, update_value in updates.items():
                splice = plan_edit(
                    edited,
                    _key_parts(update_key),
                    _yaml_safe(update_value),
                )
                edited = splice.apply(edited)
            return edited
        for update_key, update_value in updates.items():
            set_in_document(document, _key_parts(update_key), _yaml_safe(update_value))
        return dump_document(document, file_format, original=original)
    except (ValueError, TypeError, YAMLError) as exc:
        raise ConfigError(
            f"config set cannot write this value in a {file_format.upper()} file",
            operation="set_config",
            file_path=target_path,
            details={"key": next(iter(updates)), "error": str(exc)},
        ) from exc


def _refuse_other_changes(
    old_text: str,
    new_text: str,
    target_path: Path,
    updates: Mapping[str, object],
) -> None:
    """Refuse new content that changes any key other than the ones set.

    Read alone, with and without casts, and with the companion file, every
    setting outside the updated keys must read back as it did before.

    Raises:
        ConfigError: If another setting changed or the content cannot be read.
    """
    try:
        readings = [
            (
                read_config_text(old_text, target_path, cast=cast),
                read_config_text(new_text, target_path, cast=cast),
            )
            for cast in (False, True)
        ]
        readings.append(
            (
                read_config_layers(target_path, main_text=old_text),
                read_config_layers(target_path, main_text=new_text),
            ),
        )
    except _CONFIG_LOAD_ERRORS as exc:
        raise _load_error(target_path, exc) from exc
    for before, after in readings:
        for update_key in updates:
            _drop_setting(before, _key_parts(update_key.lower()))
            _drop_setting(after, _key_parts(update_key.lower()))
        if not _same(before, after):
            raise ConfigError(
                "config set would change other settings of this file; the file "
                "is unchanged, edit it by hand",
                operation="set_config",
                file_path=target_path,
                details={"keys": list(updates)},
            )


def _drop_setting(settings: dict[str, object], parts: list[str]) -> None:
    """Remove a dotted setting from read settings, and parents it empties.

    A parent that is not a mapping is removed too: setting a key below it
    replaces it.
    """
    head, rest = parts[0], parts[1:]
    if head not in settings:
        return
    child = settings[head]
    if rest and isinstance(child, dict):
        _drop_setting(child, rest)
        if child:
            return
    del settings[head]


def _refuse_unresolvable_link(path: Path) -> None:
    """Refuse a config path that is a symlink loop or cannot be resolved.

    A link to a missing file is not refused: its target is created.

    Raises:
        ConfigError: If ``path`` is a symlink whose target cannot be reached
            for a reason other than not existing, such as a loop.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        if not path.is_symlink():
            return
        raise ConfigError(
            "config set cannot follow the configuration file's symlink: it is "
            "a symlink loop or cannot be resolved; fix the link or edit the "
            "file by hand",
            operation="set_config",
            file_path=path,
            details={"error": str(exc)},
        ) from exc


def _writable_mode(path: Path) -> int:
    """Return an existing config file's permission bits, if the user may write it.

    Raises:
        ConfigError: If the file is not writable.
    """
    if not os.access(path, os.W_OK):
        raise ConfigError(
            "config set cannot change the configuration file: it is not "
            "writable; change its permissions or edit it by hand",
            operation="write_config",
            file_path=path,
        )
    return path.stat().st_mode & _PERMISSION_BITS


def _tuples_as_lists(value: object) -> object:
    """Return a value with every tuple in it turned into a list."""
    if isinstance(value, list | tuple):
        return [_tuples_as_lists(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _tuples_as_lists(item) for key, item in value.items()}
    return value


def _already_set(
    settings: Mapping[str, object],
    key: str,
    value: object,
    *,
    cast: bool = False,
) -> bool:
    """Return whether read settings hold exactly ``value`` at ``key``.

    Args:
        settings: Settings read from a file, with or without casts.
        key: The dotted key.
        value: The value set.
        cast: Whether ``settings`` were read with casts applied; ``value``
            is read the same way. Uncast, ``"@int 4"`` is not ``4``.
    """
    found, existing = _setting_at(settings, key)
    return found and _same(existing, read_value(_yaml_safe(value), cast=cast))


def _same(left: object, right: object) -> bool:
    """Compare read values, telling ``true`` from ``1`` and ``1`` from ``1.0``."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same(item, right[name]) for name, item in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_same, left, right))
    return left == right


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
        ``key`` with its value, plus the paired symlink setting when the
        value the file gives it, or its default, would contradict the change.
    """
    matched = ".".join(_key_parts(key)).lower()
    synced = dict(current)
    synced[matched] = value
    _sync_symlink_settings(data=synced, key=matched, value=value)
    updates: dict[str, object] = {key: synced[matched]}
    for paired_key in (_FOLLOW_SYMLINKS_KEY, _SYMLINK_POLICY_KEY):
        if paired_key == matched or paired_key not in synced:
            continue
        effective = current.get(
            paired_key,
            WinnowConfig.model_fields[paired_key].default,
        )
        if _as_model_reads(paired_key, synced[paired_key]) != _as_model_reads(
            paired_key,
            effective,
        ):
            updates[paired_key] = synced[paired_key]
    return updates


def _as_model_reads(key: str, value: object) -> object:
    """Return a paired symlink setting as the model reads it, else as given."""
    try:
        return _PAIRED_ADAPTERS[key].validate_python(value)
    except ValidationError:
        return value


def _validate_new_text(
    new_text: str,
    *,
    target_path: Path,
    updates: Mapping[str, object],
) -> WinnowConfig:
    """Check new file content before it is written.

    Read on its own, the content must set each updated key to exactly its
    value, compared as :func:`_already_set` compares, both as written and
    with casts applied, so an earlier cast string cannot join the value. It
    must also give a valid configuration together with the companion file.

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
        written = read_config_text(new_text, target_path, cast=False)
        cast_read = read_config_text(new_text, target_path)
        layered = read_config_layers(target_path, main_text=new_text)
        for update_key, update_value in updates.items():
            if not (
                _already_set(written, update_key, update_value)
                and _already_set(cast_read, update_key, update_value, cast=True)
            ):
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
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
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


def _write_text(
    text: str,
    *,
    config_path: Path,
    mode: int | None = None,
    newline: str | None = None,
) -> None:
    """Replace a config file's content atomically.

    Args:
        text: The new content.
        config_path: Destination path.
        mode: Permission bits for the file; ``None`` keeps the private mode
            of a new temporary file.
        newline: How line ends are translated on write, as for :func:`open`;
            ``""`` writes ``text`` as it is.

    Raises:
        ConfigError: If the file cannot be written. The temporary file is
            removed whatever interrupts the write.
    """
    temp_path: Path | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            dir=config_path.parent,
            encoding="utf-8",
            newline=newline,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if mode is not None:
            temp_path.chmod(mode)
        temp_path.replace(config_path)
    except BaseException as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if not isinstance(exc, OSError):
            raise
        raise ConfigError(
            "Unable to write Winnow configuration",
            operation="write_config",
            file_path=config_path,
            details={"error": str(exc)},
        ) from exc
