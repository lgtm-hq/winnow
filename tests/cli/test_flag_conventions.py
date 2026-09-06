"""Sweep tests enforcing the cross-command flag conventions.

Every command reachable from :func:`winnow.cli.main` is checked against the
normative rules in ``winnow/cli/standards.py``. New commands opt into the
destructive or format-exception rules by adding their qualified name to the
tables below in their own pull request.
"""

from __future__ import annotations

import click
import pytest
from assertpy import assert_that

from winnow.cli import main
from winnow.cli.standards import FORMAT_CHOICES

# Commands that need both --dry-run and --yes.
_DESTRUCTIVE: frozenset[str] = frozenset(
    {"cache clear", "cache prune", "clean", "config reset"},
)
# Commands with --yes but no --dry-run (nothing to preview).
_YES_ONLY: frozenset[str] = frozenset({"init"})
# Commands whose --format choices intentionally differ from FORMAT_CHOICES.
_FORMAT_EXCEPTIONS: dict[str, str] = {
    "config show": "dumps a document (yaml|json), not tabular rows",
}
# Long flags that must carry the listed short alias wherever they appear.
_SHORT_FLAGS: dict[str, str] = {
    "--yes": "-y",
    "--format": "-f",
    "--output": "-o",
    "--workers": "-w",
}


def _walk(
    group: click.Group,
    prefix: str = "",
) -> list[tuple[str, click.Command]]:
    """Collect every command reachable from a group with its qualified name.

    Args:
        group: Click group to walk.
        prefix: Qualified-name prefix for nested groups.

    Returns:
        ``(qualified_name, command)`` pairs, excluding the root group itself.
    """
    found: list[tuple[str, click.Command]] = []
    for name in group.list_commands(click.Context(group)):
        command = group.get_command(click.Context(group), name)
        if command is None:  # pragma: no cover - defensive
            continue
        qualified = f"{prefix}{name}"
        found.append((qualified, command))
        if isinstance(command, click.Group):
            found.extend(_walk(command, prefix=f"{qualified} "))
    return found


def _options(command: click.Command) -> list[click.Option]:
    """Return the ``click.Option`` params declared on a command.

    Args:
        command: Command to inspect.

    Returns:
        Declared options in declaration order.
    """
    return [param for param in command.params if isinstance(param, click.Option)]


def _long_flags(command: click.Command) -> set[str]:
    """Return every long or secondary flag spelling declared on a command.

    Args:
        command: Command to inspect.

    Returns:
        Set of flag strings such as ``--yes`` or ``--no-cache``.
    """
    flags: set[str] = set()
    for option in _options(command):
        flags.update(option.opts)
        flags.update(option.secondary_opts)
    return flags


_COMMANDS = _walk(main)
_IDS = [name for name, _ in _COMMANDS]
# Root group included: every rule except the root-only ``--no-color`` rule
# applies to ``winnow`` itself as much as to its subcommands.
_ALL_COMMANDS = [("winnow", main), *_COMMANDS]
_ALL_IDS = [name for name, _ in _ALL_COMMANDS]
_TABLE_NAMES = sorted(_DESTRUCTIVE | _YES_ONLY | set(_FORMAT_EXCEPTIONS))


@pytest.mark.parametrize(("name", "command"), _COMMANDS, ids=_IDS)
def test_no_color_is_root_only(name: str, command: click.Command) -> None:
    """Rule 1: no subcommand declares ``--no-color``; the root owns it."""
    assert_that(_long_flags(command)).described_as(name).does_not_contain(
        "--no-color",
    )


@pytest.mark.parametrize(("name", "command"), _ALL_COMMANDS, ids=_ALL_IDS)
def test_no_command_declares_force(name: str, command: click.Command) -> None:
    """Rule 2: ``--force`` is never used; ``--yes`` is the confirmation skip."""
    assert_that(_long_flags(command)).described_as(name).does_not_contain("--force")


@pytest.mark.parametrize(("name", "command"), _ALL_COMMANDS, ids=_ALL_IDS)
def test_destructive_commands_pair_dry_run_with_yes(
    name: str,
    command: click.Command,
) -> None:
    """Rule 3: destructive commands carry both flags; ``--yes`` needs ``--dry-run``."""
    flags = _long_flags(command)
    if name in _DESTRUCTIVE:
        assert_that(flags).described_as(name).contains("--dry-run", "--yes")
    if "--yes" in flags and name not in _YES_ONLY:
        assert_that(flags).described_as(name).contains("--dry-run")


@pytest.mark.parametrize(("name", "command"), _ALL_COMMANDS, ids=_ALL_IDS)
def test_format_choices_match_standard(name: str, command: click.Command) -> None:
    """Rule 4: ``--format`` uses ``FORMAT_CHOICES`` unless listed as an exception."""
    if name in _FORMAT_EXCEPTIONS:
        return
    for option in _options(command):
        if "--format" not in option.opts:
            continue
        option_type = option.type
        assert_that(option_type).described_as(name).is_instance_of(click.Choice)
        if not isinstance(option_type, click.Choice):  # pragma: no cover
            continue
        choices = tuple(option_type.choices)
        assert_that(choices).described_as(name).is_equal_to(FORMAT_CHOICES)


@pytest.mark.parametrize(("name", "command"), _ALL_COMMANDS, ids=_ALL_IDS)
def test_standard_flags_carry_short_alias(
    name: str,
    command: click.Command,
) -> None:
    """Rule 5: standard long flags always ship their documented short alias."""
    for option in _options(command):
        for long_flag, short_flag in _SHORT_FLAGS.items():
            if long_flag in option.opts:
                assert_that(option.opts).described_as(
                    f"{name} {long_flag}",
                ).contains(short_flag)


@pytest.mark.parametrize("name", _TABLE_NAMES)
def test_convention_tables_have_no_stale_entries(name: str) -> None:
    """Rule 6: every table entry names a command that exists in the tree."""
    assert_that(_IDS).described_as(name).contains(name)
