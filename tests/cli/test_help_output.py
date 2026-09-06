"""Sweep test asserting ``--help`` never leaks docstring sections."""

from __future__ import annotations

from collections.abc import Iterator

import click
import pytest
from assertpy import assert_that

from winnow.cli import main


def _iter_commands(
    group: click.Group,
    ctx: click.Context,
    prefix: str,
) -> Iterator[tuple[str, click.Command, click.Context]]:
    """Yield every command reachable from ``group``, recursing into subgroups.

    Args:
        group: Click group whose commands should be enumerated.
        ctx: Context for ``group``; used as the parent of each child context.
        prefix: Space-separated qualified name of ``group`` (empty for the root).

    Yields:
        Tuples of ``(qualified_name, command, ctx)`` for each command found.
    """
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if command is None:
            continue
        qualified_name = f"{prefix} {name}".strip()
        child_ctx = click.Context(command, info_name=name, parent=ctx)
        yield qualified_name, command, child_ctx
        if isinstance(command, click.Group):
            yield from _iter_commands(
                group=command,
                ctx=child_ctx,
                prefix=qualified_name,
            )


_ROOT_CTX = click.Context(main, info_name="winnow")
_ALL_COMMANDS = list(_iter_commands(group=main, ctx=_ROOT_CTX, prefix=""))


@pytest.mark.parametrize(
    ("qualified_name", "command", "ctx"),
    _ALL_COMMANDS,
    ids=[entry[0] for entry in _ALL_COMMANDS],
)
def test_help_does_not_leak_docstring_sections(
    qualified_name: str,
    command: click.Command,
    ctx: click.Context,
) -> None:
    """Rendered help stops at the ``\\f`` line before ``Args:``/``Raises:``.

    Args:
        qualified_name: Space-separated command path, e.g. ``config set``.
        command: Command under test.
        ctx: Context built for ``command``.
    """
    help_text = command.get_help(ctx)
    assert_that(help_text, description=qualified_name).does_not_contain("Args:")
    assert_that(help_text, description=qualified_name).does_not_contain("Raises:")
