"""Environment-variable overlay and symlink-setting reconciliation."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Mapping
from typing import Final

from pydantic import TypeAdapter, ValidationError

from winnow.config.defaults import (
    CONFIG_DIR_ENVVAR,
    DATA_DIR_ENVVAR,
    ENVVAR_PREFIX,
)
from winnow.models.enums import SymlinkPolicy

_ENVVAR_PREFIX = f"{ENVVAR_PREFIX}_"
_DIRECTORY_ENVVARS = frozenset({CONFIG_DIR_ENVVAR, DATA_DIR_ENVVAR})
"""Location variables that share the prefix but are not settings overrides."""
_FOLLOW_SYMLINKS_KEY = "follow_symlinks"
_SYMLINK_POLICY_KEY = "symlink_policy"
_BOOL_ADAPTER = TypeAdapter(bool)
_SYMLINK_POLICY_ADAPTER = TypeAdapter(SymlinkPolicy)
_TRUE_WORDS: Final = frozenset({"t", "true", "enabled", "1", "on", "yes"})
"""Words the ``@bool`` marker reads as true; anything else is false."""
_CAPITALIZED_BOOLEANS: Final = frozenset({"True", "False"})


def _cast_bool(text: str) -> bool:
    """Read a ``@bool`` marker value."""
    return text.strip().lower() in _TRUE_WORDS


def _cast_none(_text: str) -> None:
    """Read a ``@none`` or ``@null`` marker, whatever follows it."""


_CAST_MARKERS: Final[dict[str, Callable[[str], object]]] = {
    "@str": str,
    "@int": int,
    "@float": float,
    "@bool": _cast_bool,
    "@json": json.loads,
    "@none": _cast_none,
    "@null": _cast_none,
}
"""Explicit cast markers: ``WINNOW_WORKERS="@int 4"`` casts the rest as int."""


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
        if not env_key.startswith(_ENVVAR_PREFIX) or env_key in _DIRECTORY_ENVVARS:
            continue
        setting_key = env_key.removeprefix(_ENVVAR_PREFIX)
        key_parts = [part.lower() for part in setting_key.split("__") if part]
        if not key_parts:
            continue
        if len(key_parts) == 1:
            explicit_keys.add(key_parts[0])
        parsed_value = _parse_env_value(env_key=env_key, raw=env_value)
        _set_env_value(data=data, key_parts=key_parts, value=parsed_value)

    return data, frozenset(explicit_keys)


def _parse_env_value(*, env_key: str, raw: str) -> object:
    """Cast one environment string to a config value.

    A leading cast marker (``@int``, ``@float``, ``@bool``, ``@str``,
    ``@json``, ``@none``, ``@null``) casts the rest of the string explicitly.
    Otherwise the string is read as a TOML value, so ``true``, ``42``,
    ``1.5``, ``[1, 2]``, ``{a = 1}``, ``"quoted"`` and ``2024-01-01`` get their
    TOML types, with ``True`` and ``False`` accepted as booleans too. Anything
    that is not a TOML value is kept as the original string.

    Args:
        env_key: Environment variable name, for error messages.
        raw: Environment variable value.

    Returns:
        The cast value.

    Raises:
        ValueError: If a cast marker's value cannot be cast.
    """
    marker, _, rest = raw.partition(" ")
    cast = _CAST_MARKERS.get(marker)
    if cast is not None:
        try:
            return cast(rest)
        except ValueError as exc:
            message = f"{env_key}: cannot cast {rest!r} with {marker}"
            raise ValueError(message) from exc
    text = raw.lower() if raw.strip() in _CAPITALIZED_BOOLEANS else raw
    try:
        return tomllib.loads(f"key={text}")["key"]
    except tomllib.TOMLDecodeError:
        return raw


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
