# CLI Reference

This document covers every command and option available on the current Winnow CLI
surface, plus the standard flag conventions that all future subcommands will follow.

> **Status:** Pre-alpha. Only the root command (`winnow`) ships today. Subcommands are
> tracked in [open issues](https://github.com/lgtm-hq/winnow/issues) (see the parent
> epic at [#2](https://github.com/lgtm-hq/winnow/issues/2)).

---

## Commands

### `winnow`

Root command. When invoked without a subcommand it prints the current version and exits
with code 0.

#### Usage

```text
winnow [OPTIONS] COMMAND [ARGS]...
```

#### Options

| Flag         | Description                 |
| ------------ | --------------------------- |
| `--version`  | Show the version and exit.  |
| `--no-color` | Disable colored output.     |
| `--help`     | Show this message and exit. |

#### Examples

```bash
winnow              # winnow X.Y.Z — use --help for commands.
winnow --version    # winnow, version X.Y.Z
winnow --help       # show available options
```

The version shown is illustrative; the CLI prints the installed `winnow-media` version.

### `winnow init`

Creates a configuration file through guided prompts.

| Flag            | Description                                                     |
| --------------- | --------------------------------------------------------------- |
| `--yes`, `-y`   | Overwrite an existing configuration file without prompting.     |
| `--config FILE` | Path to configuration file (defaults to the working directory). |

`init` uses the standard `--yes` flag rather than a bespoke `--force`; without it the
command asks before overwriting an existing file.

### `winnow config reset`

Resets the configuration file to validated defaults.

| Flag            | Description                                                   |
| --------------- | ------------------------------------------------------------- |
| `--dry-run`     | Print `Would reset configuration to defaults at <path>` only. |
| `--yes`, `-y`   | Skip the confirmation prompt.                                 |
| `--config FILE` | Path to configuration file.                                   |

Both `--dry-run` and the success message resolve the same target: the explicit
`--config` path, else the discovered configuration file, else the working-directory
default.

### `winnow cache`

Inspects and manages the perceptual-hash cache database (`cache.db` under
`cache.directory`). The database is never created by these commands: when the file
does not exist they print `no cache database at <path>` and exit 0.

#### `winnow cache show`

Prints a `Cache` table with one `hash` row (entry count) and a footer
`Database: <path> (<size>)`. Hit rate is not shown because the counters are
per-process. Without a database the row reads `0`.

| Flag            | Description                 |
| --------------- | --------------------------- |
| `--config FILE` | Path to configuration file. |

#### `winnow cache clear`

Removes every cached entry.

| Flag            | Description                                                |
| --------------- | ---------------------------------------------------------- |
| `--dry-run`     | Print `Would remove N entries.` without deleting anything. |
| `--yes`, `-y`   | Skip the `Remove N cached entries?` prompt.                |
| `--config FILE` | Path to configuration file.                                |

An empty cache prints `Nothing to remove.`; a declined prompt prints `Aborted.` and
exits 0; success prints `Removed N entries.`

#### `winnow cache prune`

Removes entries whose source files no longer exist.

| Flag            | Description                                                   |
| --------------- | ------------------------------------------------------------- |
| `--dry-run`     | List each stale path, then `N stale entries (dry run).`       |
| `--yes`, `-y`   | Skip the `Prune N stale entries?` prompt.                     |
| `--config FILE` | Path to configuration file.                                   |

No stale entries prints `No stale entries.`; a declined prompt prints `Aborted.` and
exits 0; success prints `Pruned N entries.` (`N` counts rows, so a path cached under
several algorithms removes more rows than the prompt's path count).

---

## Standard Flag Conventions

`winnow/cli/standards.py` defines reusable Click option factories that every upcoming
subcommand will use. The sections below document each flag and the composite decorators
that group them, so contributors pick the right factory and users know what to expect
across the CLI.

### Individual options

| Flag         | Short | Type    | Default | Description                              |
| ------------ | ----- | ------- | ------- | ---------------------------------------- |
| `--dry-run`  | —     | flag    | `false` | Preview changes without modifying files. |
| `--yes`      | `-y`  | flag    | `false` | Skip confirmation prompts.               |
| `--no-color` | —     | flag    | `false` | Disable colored output (root only).      |
| `--format`   | `-f`  | choice  | `table` | Output format (see values below).        |
| `--output`   | `-o`  | path    | none    | Output file path.                        |
| `--workers`  | `-w`  | int ≥ 1 | `4`     | Number of worker threads.                |
| `--config`   | —     | path    | none    | Path to configuration file.              |

`--format` accepts: `json`, `csv`, `table`, `markdown`.

#### Cache options

The cache group is applied as a unit via `cache_options()`:

| Flag                            | Type | Default | Description                |
| ------------------------------- | ---- | ------- | -------------------------- |
| `--enable-cache` / `--no-cache` | flag | `true`  | Enable or disable caching. |
| `--cache-path`                  | path | none    | Cache directory path.      |

### Cross-command conventions

| Convention        | Rule                                                                              |
| ----------------- | --------------------------------------------------------------------------------- |
| Confirmation skip | `--yes`/`-y` everywhere; never `--force`                                          |
| Worker count      | `--workers`/`-w`; purpose-specific variants only when genuinely distinct          |
| Cache toggle      | `--enable-cache/--no-cache`                                                       |
| Output format     | `--format`/`-f` with `OutputFormat` choices; document dumps are listed exceptions |
| Color             | `--no-color` on the root command only; subcommands read `ctx.obj["no_color"]`     |
| Destructive ops   | `--dry-run` + confirmation unless `--yes`                                         |

`tests/cli/test_flag_conventions.py` sweeps every registered command against these
rules. New destructive commands or format exceptions register themselves in that
module's tables (`_DESTRUCTIVE`, `_YES_ONLY`, `_FORMAT_EXCEPTIONS`).

### Composite option groups

Subcommands attach options through one of three compositor decorators defined in
`winnow.cli.standards`:

| Compositor                   | Included options                      |
| ---------------------------- | ------------------------------------- |
| `standard_command_options`   | `--config`, `--dry-run`, `--yes`      |
| `reporting_command_options`  | standard + `--format`, `--output`     |
| `processing_command_options` | standard + `--workers`, cache options |

Note the usage asymmetry: the compositors above are applied directly
(`@standard_command_options`) because they accept the callback, while the individual
option factories are functions that must be **called** first to return a decorator
(`@dry_run_option()`, `@yes_option()`).

---

## Exit codes

Every `winnow` invocation ends with one of four exit codes. Commands raise `WinnowError`
subclasses and let them propagate; the root `WinnowGroup` (`winnow/cli/errors.py`)
renders them once, as an error panel on stderr, and picks the code. Scripts can
therefore tell "you called it wrong" (2), "it failed" (1), and "you stopped it" (130)
apart.

| Code | Meaning                                                                                                                             | Source                                                              |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 0    | Success, including "nothing to do" and a decline the command handles itself                                                         | command returns                                                     |
| 1    | Failure: any `WinnowError` subclass, a command-reported failure, or a declined `click.confirm(..., abort=True)` prompt (`Aborted!`) | `WinnowGroup.invoke` / `ctx.exit(ExitCode.FAILURE)` / Click `Abort` |
| 2    | Usage error: bad flag, missing argument, unknown command                                                                            | Click                                                               |
| 130  | Interrupted by Ctrl-C                                                                                                               | `WinnowGroup.invoke`                                                |

Declined prompts split by how the command asks: `clean`, `cache clear`, `cache prune`,
and the REPL print `Aborted.` and return (0); `config reset` and `init` use
`click.confirm(..., abort=True)`, so a decline is Click's `Abort` and exits 1.

Use the `ExitCode` enum from `winnow.cli.errors` rather than bare integers when a
command needs to report a failure itself (for example `doctor` exiting with
`ExitCode.FAILURE` when a hard check fails).

---

## Adding a subcommand

1. Choose the appropriate compositor from `winnow.cli.standards` and decorate the
   callback (e.g. `@standard_command_options`).
2. Register the command on `main` in `winnow/cli/__init__.py` via
   `main.add_command(your_command)`.
3. Use `--dry-run` for any write operation and `--yes` for destructive prompts.
4. Read `ctx.obj["no_color"]` from the root context to honour the user's colour output
   preference.
5. Raise `WinnowError` subclasses for domain failures instead of wrapping them in
   `click.ClickException`; the root handler maps them to exit code 1.
