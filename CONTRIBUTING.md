# Contributing to Winnow

Thank you for contributing to Winnow!

## Development Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Build

```bash
git clone https://github.com/lgtm-hq/winnow.git
cd winnow
make setup
```

## Linting and Testing

We use [lintro](https://github.com/lgtm-hq/py-lintro) for linting and formatting.

```bash
make lint    # uv run lintro chk
make fmt     # uv run lintro fmt
make test    # pytest with 85% coverage gate
```

## Commits and Pull Requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) in PR titles
- Squash merge is required; the PR title becomes the merge commit
- Sign commits with `-s` per [DCO.md](DCO.md)
- Every PR must pass CI before merge

## Release Process

Releases are automated via CI on `main` using semantic versioning from conventional
commit PR titles.

## Questions

Open a [GitHub issue](https://github.com/lgtm-hq/winnow/issues) or see
[SECURITY.md](SECURITY.md) for security reports.
