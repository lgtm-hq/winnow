"""Standard Click options and decorators for Winnow commands."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum, auto
from pathlib import Path
from typing import Any, TypeAlias, TypeVar, cast

import click

ClickCallback: TypeAlias = Callable[..., Any]
CommandCallback = TypeVar("CommandCallback", bound=ClickCallback)
OptionDecorator: TypeAlias = Callable[[ClickCallback], ClickCallback]


class OutputFormat(StrEnum):
    """Supported output formats for reporting commands."""

    JSON = auto()
    CSV = auto()
    TABLE = auto()
    MARKDOWN = auto()


FORMAT_CHOICES = tuple(output_format.value for output_format in OutputFormat)
DEFAULT_WORKERS = 4

__all__ = [
    "DEFAULT_WORKERS",
    "FORMAT_CHOICES",
    "OutputFormat",
    "cache_options",
    "config_path_option",
    "dry_run_option",
    "format_option",
    "no_color_option",
    "output_option",
    "processing_command_options",
    "reporting_command_options",
    "standard_command_options",
    "workers_option",
    "yes_option",
]


def _option(*param_decls: str, **attrs: Any) -> OptionDecorator:
    """Create a typed Click option decorator.

    Args:
        *param_decls: Click option declarations.
        **attrs: Click option attributes.

    Returns:
        A decorator that adds the option to a Click command callback.
    """
    return cast("OptionDecorator", click.option(*param_decls, **attrs))


def _apply_option(
    command: CommandCallback,
    decorator: OptionDecorator,
) -> CommandCallback:
    """Apply a Click option decorator while preserving callback type.

    Args:
        command: Click command callback to decorate.
        decorator: Option decorator to apply.

    Returns:
        The decorated command callback.
    """
    return cast("CommandCallback", decorator(command))


def dry_run_option() -> OptionDecorator:
    """Create the standard dry-run option decorator.

    Returns:
        A decorator that adds ``--dry-run``.
    """
    return _option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Preview changes without modifying files.",
    )


def yes_option() -> OptionDecorator:
    """Create the standard confirmation-skip option decorator.

    Returns:
        A decorator that adds ``--yes`` and ``-y``.
    """
    return _option(
        "--yes",
        "-y",
        is_flag=True,
        default=False,
        help="Skip confirmation prompts.",
    )


def no_color_option() -> OptionDecorator:
    """Create the standard no-color option decorator.

    Returns:
        A decorator that adds ``--no-color``.
    """
    return _option(
        "--no-color",
        is_flag=True,
        default=False,
        help="Disable colored output.",
    )


def format_option(
    default: OutputFormat | str = OutputFormat.TABLE,
) -> OptionDecorator:
    """Create the standard output format option decorator.

    Args:
        default: Default output format, as an :class:`OutputFormat` or its
            string value.

    Returns:
        A decorator that adds ``--format`` and ``-f``.

    Raises:
        ValueError: If ``default`` is not a supported output format.
    """
    default_format = OutputFormat(default)
    return _option(
        "--format",
        "-f",
        type=click.Choice(FORMAT_CHOICES),
        default=default_format.value,
        show_default=True,
        help="Output format.",
    )


def output_option() -> OptionDecorator:
    """Create the standard output path option decorator.

    Returns:
        A decorator that adds ``--output`` and ``-o``.
    """
    return _option(
        "--output",
        "-o",
        type=click.Path(path_type=Path),
        default=None,
        help="Output file path.",
    )


def workers_option(default: int = DEFAULT_WORKERS) -> OptionDecorator:
    """Create the standard worker count option decorator.

    Args:
        default: Default number of workers.

    Returns:
        A decorator that adds ``--workers`` and ``-w``.
    """
    return _option(
        "--workers",
        "-w",
        type=click.IntRange(min=1),
        default=default,
        show_default=True,
        help="Number of worker threads.",
    )


def config_path_option() -> OptionDecorator:
    """Create the standard configuration path option decorator.

    Returns:
        A decorator that adds ``--config`` and stores it as ``config_path``.
    """
    return _option(
        "--config",
        "config_path",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="Path to configuration file.",
    )


def cache_options() -> OptionDecorator:
    """Create the standard cache options decorator.

    Returns:
        A decorator that adds cache toggle and path options.
    """

    def decorator(command: ClickCallback) -> ClickCallback:
        """Apply standard cache options to a Click command callback.

        Args:
            command: Click command callback to decorate.

        Returns:
            The decorated command callback.
        """
        command = _apply_option(
            command,
            _option(
                "--enable-cache/--no-cache",
                default=True,
                show_default=True,
                help="Enable or disable caching.",
            ),
        )
        command = _apply_option(
            command,
            _option(
                "--cache-path",
                type=click.Path(path_type=Path),
                default=None,
                help="Cache directory path.",
            ),
        )
        return command

    return decorator


def standard_command_options(command: CommandCallback) -> CommandCallback:
    """Apply standard options shared by most commands.

    The root command owns ``--no-color`` and stores its value in ``ctx.obj`` for
    subcommands.

    Args:
        command: Click command callback to decorate.

    Returns:
        The decorated command callback.
    """
    command = _apply_option(command, config_path_option())
    command = _apply_option(command, dry_run_option())
    command = _apply_option(command, yes_option())
    return command


def reporting_command_options(command: CommandCallback) -> CommandCallback:
    """Apply options for commands that emit reports.

    Args:
        command: Click command callback to decorate.

    Returns:
        The decorated command callback.
    """
    command = standard_command_options(command)
    command = _apply_option(command, format_option())
    command = _apply_option(command, output_option())
    return command


def processing_command_options(command: CommandCallback) -> CommandCallback:
    """Apply options for commands that process media in parallel.

    Args:
        command: Click command callback to decorate.

    Returns:
        The decorated command callback.
    """
    command = standard_command_options(command)
    command = _apply_option(command, workers_option())
    command = _apply_option(command, cache_options())
    return command
