"""Defaults and filesystem locations for Winnow configuration."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_FILE_NAME = ".winnow-config.yaml"
ENVVAR_PREFIX = "WINNOW"
CONFIG_DIR_ENVVAR = "WINNOW_CONFIG_DIR"
DATA_DIR_ENVVAR = "WINNOW_DATA_DIR"
XDG_CONFIG_HOME_ENVVAR = "XDG_CONFIG_HOME"
XDG_DATA_HOME_ENVVAR = "XDG_DATA_HOME"


def cwd_config_path(cwd: Path | None = None) -> Path:
    """Return the config path for a working directory.

    Args:
        cwd: Directory to resolve from, or the current working directory.

    Returns:
        Expected working-directory config path.
    """
    return (cwd if cwd is not None else Path.cwd()) / CONFIG_FILE_NAME


def _resolve_user_dir(
    *,
    override_envvar: str,
    xdg_envvar: str,
    fallback: Path,
) -> Path:
    """Resolve a per-user Winnow directory from the environment.

    The environment is read at call time (never cached at import) so tests
    can ``monkeypatch.setenv`` and observe the change immediately.

    Args:
        override_envvar: Winnow-specific variable naming the directory itself.
        xdg_envvar: XDG base-directory variable; ``winnow`` is appended.
        fallback: Directory used when neither variable is set.

    Returns:
        The first of ``override_envvar`` (``expanduser()`` applied),
        ``xdg_envvar / "winnow"``, or ``fallback`` that is set.
    """
    override = os.environ.get(override_envvar)
    if override:
        return Path(override).expanduser()
    xdg_home = os.environ.get(xdg_envvar)
    if xdg_home:
        return Path(xdg_home).expanduser() / "winnow"
    return fallback


def user_config_dir() -> Path:
    """Return the per-user Winnow config directory.

    Resolution order: ``WINNOW_CONFIG_DIR``, then ``XDG_CONFIG_HOME/winnow``,
    then ``~/.config/winnow``.

    Returns:
        XDG-style user configuration directory for Winnow.
    """
    return _resolve_user_dir(
        override_envvar=CONFIG_DIR_ENVVAR,
        xdg_envvar=XDG_CONFIG_HOME_ENVVAR,
        fallback=Path.home() / ".config" / "winnow",
    )


def user_data_dir() -> Path:
    """Return the per-user Winnow data directory.

    Durable per-user state such as the saga session log lives here.
    Resolution order: ``WINNOW_DATA_DIR``, then ``XDG_DATA_HOME/winnow``,
    then ``~/.local/share/winnow``.

    Returns:
        XDG-style user data directory for Winnow.
    """
    return _resolve_user_dir(
        override_envvar=DATA_DIR_ENVVAR,
        xdg_envvar=XDG_DATA_HOME_ENVVAR,
        fallback=Path.home() / ".local" / "share" / "winnow",
    )


def user_config_path(config_dir: Path | None = None) -> Path:
    """Return the per-user Winnow config file path.

    Args:
        config_dir: User config directory override, primarily for tests.

    Returns:
        Expected per-user config path.
    """
    return (
        config_dir if config_dir is not None else user_config_dir()
    ) / CONFIG_FILE_NAME
