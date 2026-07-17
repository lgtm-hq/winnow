#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Purpose: Check whether a GHCR container package exists before pruning it
#
# Environment variables:
#   OWNER: GitHub org or user that owns the package (required)
#   PACKAGE_NAME: Container package name (required)
#   GH_TOKEN: Token with packages:read scope, consumed by gh (required)
#
# Writes exists=true|false to $GITHUB_OUTPUT.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
	cat <<'EOF'
Check whether a GHCR container package exists for an owner.

Usage:
  scripts/ci/maintenance/check-ghcr-package.sh [--help|-h]

Environment variables:
  OWNER          GitHub org or user that owns the package (required)
  PACKAGE_NAME   Container package name (required)
  GH_TOKEN       Token with packages:read scope (required)

Writes exists=true|false to $GITHUB_OUTPUT so callers can gate cleanup
jobs, keeping scheduled runs green while no image has been published.
EOF
	exit 0
fi

: "${OWNER:?OWNER is required}"
: "${PACKAGE_NAME:?PACKAGE_NAME is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

# Package names containing slashes must be URL-encoded for the Packages API.
encoded_package="${PACKAGE_NAME//\//%2F}"

if gh api "orgs/${OWNER}/packages/container/${encoded_package}" >/dev/null 2>&1 ||
	gh api "users/${OWNER}/packages/container/${encoded_package}" >/dev/null 2>&1; then
	echo "GHCR package '${PACKAGE_NAME}' found for '${OWNER}'."
	echo "exists=true" >>"${GITHUB_OUTPUT}"
else
	echo "GHCR package '${PACKAGE_NAME}' not found for '${OWNER}'; cleanup will be skipped."
	echo "exists=false" >>"${GITHUB_OUTPUT}"
fi
