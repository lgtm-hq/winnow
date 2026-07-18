"""``winnow config`` command group for inspecting and editing configuration."""

from __future__ import annotations

import json
from pathlib import Path

import click

from winnow.cli.standards import config_path_option, yes_option
from winnow.config import (
    find_config_path,
    load_config,
    render_config_yaml,
    reset_config,
    set_config_value,
    validate_config,
)
from winnow.exceptions import ConfigError

__all__ = ["config"]

_SHOW_FORMATS = ("yaml", "json")


def _parse_value(raw: str) -> object:
    """Parse a raw CLI value into a JSON-native type when possible.

    Args:
        raw: Raw string value supplied on the command line.

    Returns:
        The parsed value, or the original string when it is not valid JSON.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@click.group(name="config")
def config() -> None:
    """Inspect and edit the Winnow configuration file."""


@config.command(name="show")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(_SHOW_FORMATS),
    default="yaml",
    show_default=True,
    help="Output format.",
)
@config_path_option()
def show(
    *,
    output_format: str,
    config_path: Path | None,
) -> None:
    """Show the effective configuration, merging defaults and overrides.

    Args:
        output_format: Rendering format for the configuration.
        config_path: Explicit configuration file path.

    Raises:
        ClickException: If the configuration cannot be loaded.
    """
    try:
        config_model = load_config(config_path=config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    if output_format == "json":
        click.echo(json.dumps(config_model.model_dump(mode="json"), indent=2))
        return
    click.echo(render_config_yaml(config_model), nl=False)


@config.command(name="set")
@click.argument("key")
@click.argument("value")
@config_path_option()
def set_value(
    *,
    key: str,
    value: str,
    config_path: Path | None,
) -> None:
    """Set a dotted configuration key and persist the validated result.

    Args:
        key: Dotted configuration key, such as ``cache.enabled``.
        value: New value; parsed as JSON when possible, else kept as a string.
        config_path: Explicit configuration file path.

    Raises:
        ClickException: If the key or value is rejected by validation.
    """
    try:
        set_config_value(
            key=key,
            value=_parse_value(value),
            config_path=config_path,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    target = config_path if config_path is not None else find_config_path()
    click.echo(f"Set {key} in {target}")


@config.command(name="reset")
@yes_option()
@config_path_option()
def reset(
    *,
    yes: bool,
    config_path: Path | None,
) -> None:
    """Reset the configuration file to validated defaults.

    Args:
        yes: Skip the confirmation prompt when true.
        config_path: Explicit configuration file path.

    Raises:
        ClickException: If the configuration cannot be written.
    """
    if not yes:
        click.confirm(
            "Reset configuration to defaults?",
            abort=True,
        )
    try:
        reset_config(config_path=config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    target = config_path if config_path is not None else find_config_path()
    click.echo(f"Reset configuration to defaults at {target}")


@config.command(name="validate")
@config_path_option()
def validate(
    *,
    config_path: Path | None,
) -> None:
    """Validate a configuration source and report the result.

    Args:
        config_path: Explicit configuration file path.

    Raises:
        ClickException: If the configuration is invalid.
    """
    resolved = config_path if config_path is not None else find_config_path()
    if resolved is None:
        click.echo("No configuration file found; defaults are valid.")
        return
    try:
        validate_config(config_path=resolved)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Configuration at {resolved} is valid.")
