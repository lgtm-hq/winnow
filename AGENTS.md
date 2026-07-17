# AGENTS.md

## Cursor Cloud specific instructions

Winnow is a single local-first Python CLI/library (`winnow-media`, package dir `winnow/`).
There are no long-running services, databases, or web servers — development and testing
happen entirely through `uv` and the `winnow` CLI.

Standard dev commands live in the `Makefile` and `README.md`; use those rather than
duplicating them. Key non-obvious notes:

- Tooling is managed by [`uv`](https://docs.astral.sh/uv/) (installed to `~/.local/bin`)
  and the lint helper `markdownlint-cli2` (installed via npm to `~/.npm-global/bin`). Both
  paths are added to `PATH` in `~/.bashrc`. If a tool is "not found", run
  `export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"`.
- Run everything through `uv run ...` (e.g. `uv run winnow --help`, `uv run pytest`). Do not
  invoke a system `python`/`pytest`; the project deps live in the uv-managed venv.
- Lint uses `lintro` (`make lint`), which orchestrates ~26 tools. In CI these run inside the
  `ghcr.io/lgtm-hq/py-lintro` Docker image. Locally most non-Python tools (gitleaks, prettier,
  semgrep, shellcheck, shfmt, taplo, actionlint, osv-scanner) are absent and lintro SKIPs them
  gracefully — that does not fail the run. Only `markdownlint` hard-fails when missing, which is
  why `markdownlint-cli2` is installed. The Python tools (ruff, black, mypy, bandit, pydoclint,
  yamllint) run locally and must stay clean.
- Tests: `make test` runs unit tests with an 85% coverage gate (integration excluded by default
  via `-m "not integration"` in `pyproject.toml`). `make test-integration` builds a wheel with
  `uv build` and runs `scripts/ci/release/smoke-test-distribution.sh` (needs `bash`).
- The CLI is an early-stage skeleton: only `winnow`, `winnow --help`, and `winnow --version`
  exist; there are no media-processing subcommands yet.
