# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| `0.1.x` | ✅        |
| `< 0.1` | ❌        |

## Reporting Security Issues

Please **do not** create public GitHub issues for security vulnerabilities.

### How to Report

1. **GitHub advisory** (preferred):
   [Private security advisory](https://github.com/lgtm-hq/winnow/security/advisories/new)
2. **Email** (fallback): `security@lgtm-hq.io` with `SECURITY: winnow` in the subject

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

### Response Timeline

- **Acknowledgment**: Within 3 business days
- **Initial assessment**: Within 7 business days
- **Fix timeline**: Depends on severity (critical issues prioritized)

## Security Practices

- Dependencies managed via Renovate with org preset
- CI includes CodeQL, OSV scanning, OpenSSF Scorecard, and SBOM generation
- GitHub Actions pinned to full commit SHAs
