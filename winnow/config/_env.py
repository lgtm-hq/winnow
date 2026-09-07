"""Environment-variable overlay and symlink-setting reconciliation."""

from __future__ import annotations

from collections.abc import Mapping

from dynaconf.utils.parse_conf import boolean_fix, parse_conf_data
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
