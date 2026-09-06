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

## Adding a subcommand

1. Choose the appropriate compositor from `winnow.cli.standards` and decorate the
   callback (e.g. `@standard_command_options`).
2. Register the command on `main` in `winnow/cli/__init__.py` via
   `main.add_command(your_command)`.
3. Use `--dry-run` for any write operation and `--yes` for destructive prompts.
4. Read `ctx.obj["no_color"]` from the root context to honour the user's colour output
   preference.
