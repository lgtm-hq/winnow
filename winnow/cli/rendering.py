"""Shared rendering helpers for Winnow CLI commands.

These utilities centralize how commands build a Rich console and format
common values (byte counts and timestamps) so reporting output stays
consistent across subcommands.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    import click

_SIZE_UNITS: tuple[str, ...] = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

__all__ = [
    "console_from_context",
    "create_console",
    "format_size",
    "format_timestamp",
]


def create_console(*, no_color: bool = False) -> Console:
    """Create a Rich console configured for Winnow output.

    Args:
        no_color: When ``True``, disable ANSI color styling.

    Returns:
        A console instance with syntax highlighting disabled so file paths
        and numbers render literally.
    """
    return Console(no_color=no_color, highlight=False)


def console_from_context(ctx: click.Context) -> Console:
    """Build a console honoring the root command's ``--no-color`` flag.

    Args:
        ctx: Active Click context whose object may carry ``no_color``.

    Returns:
        A console configured from the shared context state.
    """
    context_obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    no_color = bool(context_obj.get("no_color", False))
    return create_console(no_color=no_color)


def _format_unit(size: float, unit: str) -> str:
    """Render a scaled size value with its unit suffix.

    Args:
        size: Size already scaled into ``unit``.
        unit: Unit suffix to append.

    Returns:
        Whole-number bytes or a one-decimal value for larger units.
    """
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable binary size string.

    Args:
        size_bytes: Non-negative number of bytes.

    Returns:
        A string such as ``"512 B"`` or ``"1.5 MiB"``.

    Raises:
        ValueError: If ``size_bytes`` is negative.
    """
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")

    size = float(size_bytes)
    for unit in _SIZE_UNITS[:-1]:
        if size < 1024.0:
            return _format_unit(size=size, unit=unit)
        size /= 1024.0
    return _format_unit(size=size, unit=_SIZE_UNITS[-1])


def format_timestamp(moment: datetime) -> str:
    """Format a datetime for tabular display.

    Args:
        moment: Timestamp to render.

    Returns:
        The timestamp formatted as ``YYYY-MM-DD HH:MM:SS``.
    """
    return moment.strftime(_TIMESTAMP_FORMAT)
