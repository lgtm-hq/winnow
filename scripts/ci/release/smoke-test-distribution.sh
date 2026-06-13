#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Purpose: Install built distribution and verify CLI version before PyPI upload
#
# Environment variables:
#   DIST_PATH: Directory containing wheel artifacts (required)
#   EXPECTED_VERSION: Version string the CLI must report (required)
#   CLI_COMMAND: CLI executable name (default: winnow)
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
	cat <<'EOF'
Install a built wheel and verify the CLI reports the expected version.

Usage:
  scripts/ci/release/smoke-test-distribution.sh [--help|-h]

Environment variables:
  DIST_PATH          Directory containing *.whl artifacts (required)
  EXPECTED_VERSION   Bare semver expected from the CLI --version output (required)
  CLI_COMMAND        CLI executable name (default: winnow)
EOF
	exit 0
fi

: "${DIST_PATH:?DIST_PATH is required}"
: "${EXPECTED_VERSION:?EXPECTED_VERSION is required}"

cli_command="${CLI_COMMAND:-winnow}"

log_info() {
	echo "[smoke-test-distribution] $*"
}

if [[ ! -d "$DIST_PATH" ]]; then
	echo "[smoke-test-distribution] ERROR: DIST_PATH is not a directory: $DIST_PATH" >&2
	exit 1
fi

wheel_file="$(
	find "$DIST_PATH" -maxdepth 1 -name '*.whl' -type f | head -n 1
)"
if [[ -z "$wheel_file" ]]; then
	echo "[smoke-test-distribution] ERROR: No wheel found in $DIST_PATH" >&2
	exit 1
fi

log_info "Installing wheel: $wheel_file"

smoke_venv_dir="$(mktemp -d)"
trap 'rm -rf "$smoke_venv_dir"' EXIT

log_info "Creating isolated virtual environment in ${smoke_venv_dir}"
uv venv "${smoke_venv_dir}/.venv"

venv_python="${smoke_venv_dir}/.venv/bin/python"
uv pip install --python "$venv_python" "$wheel_file"

cli_path="${smoke_venv_dir}/.venv/bin/${cli_command}"

log_info "Verifying ${cli_command} --version reports ${EXPECTED_VERSION}"
actual_version="$(
	"$cli_path" --version |
		grep -oE '[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9._-]+)?' |
		head -1
)"

if [[ "$actual_version" != "$EXPECTED_VERSION" ]]; then
	echo "[smoke-test-distribution] ERROR: Expected ${EXPECTED_VERSION}, got ${actual_version}" >&2
	exit 1
fi

log_info "Smoke test passed"
