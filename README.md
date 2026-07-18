# Winnow

<!-- markdownlint-disable MD013 -- badge URLs exceed 88 characters -->

[![CI](https://github.com/lgtm-hq/winnow/actions/workflows/test-ci.yml/badge.svg)](https://github.com/lgtm-hq/winnow/actions/workflows/test-ci.yml)
[![PyPI](https://img.shields.io/pypi/v/winnow-media?label=pypi)](https://pypi.org/project/winnow-media/)
[![CodeQL](https://github.com/lgtm-hq/winnow/actions/workflows/codeql.yml/badge.svg)](https://github.com/lgtm-hq/winnow/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/lgtm-hq/winnow/badge)](https://scorecard.dev/viewer/?uri=github.com/lgtm-hq/winnow)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

<!-- markdownlint-enable MD013 -->

Winnow your media library — organize photos and videos, detect duplicates, and keep the
best copies. Local-first, offline-capable, and designed as a greenfield rewrite of
[py-organizer](https://github.com/lgtm-hq/py-organizer).

## Status

**Pre-alpha.** The published CLI is a skeleton: `winnow`, `winnow --help`, and
`winnow --version` are the only user-facing commands today. Domain models, exception
types, and CI are in place; scanning, deduplication, and organization workflows are
tracked in [open issues](https://github.com/lgtm-hq/winnow/issues) (see
[#2](https://github.com/lgtm-hq/winnow/issues/2) for the parent epic).

## Install

**Requirements:** Python 3.11+ and a package manager ([uv](https://docs.astral.sh/uv/)
recommended).

### From PyPI

Use uv's project workflow so the dependency and command run from the same managed
environment:

```bash
uv init winnow-demo
cd winnow-demo
uv add winnow-media
uv run winnow --version
```

Optional dependency groups (`face`, `cv`, `ai-detect`) are declared in `pyproject.toml`
but not wired to features yet.

### From source

```bash
git clone https://github.com/lgtm-hq/winnow.git
cd winnow
make setup    # uv sync --dev
uv run winnow --help
```

### Docker

```bash
docker pull ghcr.io/lgtm-hq/winnow:latest
docker run --rm ghcr.io/lgtm-hq/winnow:latest --version
```

Images are published to `ghcr.io/lgtm-hq/winnow` on merges to `main` and on version
tags. Platforms: `linux/amd64` and `linux/arm64`.

## Usage

Only the commands below work in the current release. There are no media-processing
subcommands yet.

```bash
uv run winnow              # prints version and points to --help
uv run winnow --help       # show available options
uv run winnow --version    # print package version
```

Example output:

```text
$ uv run winnow
winnow 0.2.0 — use --help for commands.

$ uv run winnow --version
winnow, version 0.2.0
```

### Planned usage (not implemented)

Future releases will add subcommands for scanning libraries, grouping duplicates, and
applying organization rules. Shapes will follow the domain models in `winnow/models/`
and the API-first plan in [ADR 0001](docs/adr/0001-api-first-platform.md). Do not expect
the examples below to work today:

```bash
# PLANNED — not available yet
uv run winnow scan /path/to/photos
uv run winnow duplicates review
uv run winnow organize --dry-run
```

## Features

### Available now

- **Skeleton CLI** — Click entry point with `--help` and `--version`
- **Domain models** — Pydantic v2 models for media files, duplicates, pipeline steps,
  and configuration ([`winnow/models/`](winnow/models/))
- **Exception hierarchy** — structured errors with context
  ([`winnow/exceptions.py`](winnow/exceptions.py))
- **Quality bar** — lintro lint/format, 85% test coverage gate, multi-version CI

### Roadmap

Winnow follows an
[API-first, CLI-first phasing plan](docs/adr/0001-api-first-platform.md):

| Phase       | Focus        | Highlights                                 |
| ----------- | ------------ | ------------------------------------------ |
| **Now**     | Foundation   | Models, CLI skeleton, CI/CD, ADRs          |
| **Phase 1** | Core product | Scan, dedupe, and organize via CLI and API |
| **Phase 2** | Web UI       | Presentation-only frontend over OpenAPI    |

Planned capabilities (not shipped):

- Scan local photo and video libraries with rich metadata extraction
- Detect duplicates via perceptual hashing and related signals
- Rank and keep the best copy; organize or quarantine the rest
- Local SQLite storage; no cloud dependency required
- OpenAPI schema as the stable integration surface for automation and UI

Track progress on the [issue board](https://github.com/lgtm-hq/winnow/issues) and in the
in-repo [documentation](docs/).

## Development

```bash
make setup          # install dev dependencies (uv sync --dev)
make lint           # uv run lintro chk
make fmt            # uv run lintro fmt
make test           # pytest with 85% coverage gate
make test-integration  # opt-in integration tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for commit conventions, signed commits, and PR
expectations.

## Documentation

- [Contributing](CONTRIBUTING.md)
- [CLI Reference](docs/cli-reference.md)
- [Architecture ADRs](docs/adr/README.md)
- [Security policy](SECURITY.md)
- [Governance](GOVERNANCE.md)
- [Changelog](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE).
