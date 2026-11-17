"""Environment-variable overlay and symlink-setting reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

import tomli
from pydantic import TypeAdapter, ValidationError

from winnow.config._markers import cast_marker
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
_CAPITALIZED_BOOLEANS: Final = frozenset({"True", "False"})
_DYNACONF_VARIABLE_SUFFIX: Final = "_FOR_DYNACONF"
_LOGGER = logging.getLogger(__name__)


def warn_about_dynaconf_variables(environ: Mapping[str, str]) -> None:
    """Warn about Dynaconf's own environment variables, which winnow ignores.

    Until #391 Dynaconf read variables such as ``INCLUDES_FOR_DYNACONF`` or
    ``SETTINGS_FILE_FOR_DYNACONF`` while loading winnow's config, so they
    could add files or change how values were read. winnow no longer reads
    them; a warning names each one that is set so the change is not silent.

    Args:
        environ: Environment mapping to inspect.
    """
    names = sorted(name for name in environ if name.endswith(_DYNACONF_VARIABLE_SUFFIX))
    if names:
        _LOGGER.warning(
            "Ignoring Dynaconf environment variables %s: winnow no longer uses "
            "Dynaconf and does not read them",
            names,
        )


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
    """Cast one environment string to a config value, as Dynaconf did.

    A leading cast marker (see :mod:`winnow.config._markers`) casts the rest
    of the string explicitly. Otherwise the string is read as a TOML 1.0 value,
    so ``true``, ``42``, ``1.5``, ``[1, 2]``, ``{a = 1}``, ``"quoted"`` and
    ``2024-01-01`` get their TOML types, with ``True`` and ``False`` accepted
    as booleans too. Anything that is not a TOML 1.0 value is kept as the
    string. The grammar is tomli 2.3's, the TOML 1.0 parser Dynaconf vendored,
    on every Python version; the standard library's ``tomllib`` follows TOML
    1.1 from Python 3.15 and would read ``12:30`` as a time there.

    Args:
        env_key: Environment variable name, for error messages.
        raw: Environment variable value.

    Returns:
        The cast value.

    Raises:
        ValueError: If a cast marker's value cannot be cast, or the marker is
            one winnow does not support.
    """
    text = raw.lower() if raw.strip() in _CAPITALIZED_BOOLEANS else raw
    try:
        matched, value = cast_marker(text)
    except ValueError as exc:
        message = f"{env_key}: {exc}"
        raise type(exc)(message) from exc
    if matched:
        return value
    try:
        return tomli.loads(f"key={text}")["key"]
    except (tomli.TOMLDecodeError, KeyError):
        return text


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
