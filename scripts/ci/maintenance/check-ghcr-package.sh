#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Purpose: Check whether a GHCR container package exists before pruning it
#
# Environment variables:
#   OWNER: GitHub org or user that owns the package (required)
#   PACKAGE_NAME: Container package name (required)
#   GH_TOKEN: Token with packages:read scope, consumed by gh (required)
#   OWNER_TYPE: org or user (optional; looked up through the API when unset)
#
# Writes exists=true|false to $GITHUB_OUTPUT only for a definite answer from
# the owner's package endpoint: HTTP 200 is true, HTTP 404 is false. Any other
# status, a transport error or unreadable output exits 1 and writes nothing, so
# an unreadable package fails the job instead of passing as an absent one.
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
  OWNER_TYPE     org or user (optional; looked up with GET users/OWNER)

Writes exists=true (HTTP 200) or exists=false (HTTP 404) to $GITHUB_OUTPUT so
callers can gate cleanup jobs, keeping scheduled runs green while no image has
been published. GitHub also answers 404 for a private package the token cannot
see, so the token must have packages:read on the owner's packages.

Any other result (401, 403, 429, 5xx, a transport error, no HTTP status line,
or a failed owner lookup) exits 1 and writes nothing to $GITHUB_OUTPUT.
EOF
	exit 0
fi

: "${OWNER:?OWNER is required}"
: "${PACKAGE_NAME:?PACKAGE_NAME is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

# Print text with every token value replaced, so no message can carry one.
redact() {
	local text="$1" secret
	for secret in "${GH_TOKEN:-}" "${GITHUB_TOKEN:-}"; do
		if [[ -n "$secret" ]]; then
			text="${text//"$secret"/[redacted]}"
		fi
	done
	printf '%s' "$text"
}

# Report an error on stderr and exit 1 without writing to $GITHUB_OUTPUT.
fail() {
	echo "check-ghcr-package: error: $(redact "$*")" >&2
	exit 1
}

# An owner is a GitHub login; anything else could address another endpoint.
if [[ ! "${OWNER}" =~ ^[A-Za-z0-9_-]+$ ]]; then
	fail "OWNER must be a GitHub login (letters, digits, hyphens and underscores)"
fi

# Package names containing slashes must be URL-encoded for the Packages API.
encoded_package="${PACKAGE_NAME//\//%2F}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Run gh, keeping stdout and stderr apart; sets gh_status, never exits.
run_gh() {
	gh_status=0
	gh "$@" >"${workdir}/out" 2>"${workdir}/err" || gh_status=$?
}

# Print up to three lines of gh's stderr from the last run_gh call.
gh_detail() {
	local line detail="" count=0
	while ((count < 3)) && { IFS= read -r line || [[ -n "$line" ]]; }; do
		line="${line%$'\r'}"
		# Redact first: clipping could cut a token so it no longer matches.
		line="$(redact "$line")"
		detail+="${detail:+; }${line:0:300}"
		count=$((count + 1))
	done <"${workdir}/err"
	printf '%s' "${detail:-no error output}"
}

case "${OWNER_TYPE:-}" in
org | user)
	owner_type="${OWNER_TYPE}"
	;;
"")
	run_gh api "users/${OWNER}" --jq .type
	if ((gh_status != 0)); then
		fail "owner type lookup GET users/${OWNER} failed (gh exit ${gh_status}): $(gh_detail)"
	fi
	account_type="$(<"${workdir}/out")"
	case "$account_type" in
	Organization) owner_type=org ;;
	User) owner_type=user ;;
	*)
		printf -v shown '%q' "${account_type:0:100}"
		fail "owner type lookup GET users/${OWNER} returned unexpected type ${shown}"
		;;
	esac
	;;
*)
	printf -v shown '%q' "${OWNER_TYPE:0:100}"
	fail "OWNER_TYPE must be org or user, not ${shown}"
	;;
esac

if [[ "$owner_type" == org ]]; then
	endpoint="orgs/${OWNER}/packages/container/${encoded_package}"
else
	endpoint="users/${OWNER}/packages/container/${encoded_package}"
fi
operation="package probe GET ${endpoint}"

# --include puts the HTTP status line first on stdout, even when gh fails.
run_gh api --include "$endpoint"
status_line=""
IFS= read -r status_line <"${workdir}/out" || true
status_line="${status_line%$'\r'}"
http_status=""
if [[ "$status_line" =~ ^HTTP/[0-9.]+\ ([0-9]{3})(\ |$) ]]; then
	http_status="${BASH_REMATCH[1]}"
fi

case "$http_status" in
200)
	if ((gh_status != 0)); then
		fail "${operation} returned HTTP 200 but gh exited ${gh_status}: $(gh_detail)"
	fi
	echo "GHCR package '${PACKAGE_NAME}' found for ${owner_type} '${OWNER}'."
	echo "exists=true" >>"${GITHUB_OUTPUT}"
	;;
404)
	echo "GHCR package '${PACKAGE_NAME}' not found for ${owner_type} '${OWNER}'" \
		"(HTTP 404 from GET ${endpoint}); cleanup will be skipped."
	echo "A 404 also hides a private package the token cannot read; this check" \
		"assumes the token has packages:read on '${OWNER}' packages."
	echo "exists=false" >>"${GITHUB_OUTPUT}"
	;;
"")
	fail "${operation} gave no HTTP status (gh exit ${gh_status}): $(gh_detail);" \
		"a transport error or malformed response does not prove the package is absent"
	;;
*)
	fail "${operation} returned HTTP ${http_status} (gh exit ${gh_status}): $(gh_detail);" \
		"only HTTP 200 or 404 answers whether the package exists"
	;;
esac
