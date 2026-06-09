# lgtm-hq organization rulesets (winnow)

Source-of-truth payload for the **`checks-winnow`** org ruleset. Synced to GitHub with
`lgtm-ci`’s `scripts/ci/org/sync-ruleset.sh` (org admin + `gh` auth required).

Required status checks on the default branch:

| Gate job (workflow)       | GitHub check context                             |
| ------------------------- | ------------------------------------------------ |
| `test-suite-coverage`     | `test-suite-coverage / 🧪 Test Suite & Coverage` |
| `security-audit-required` | `🔐 Security Audit`                              |
| `lintro-code-quality`     | `lintro-code-quality / 🛠️ Lintro Code Quality`   |

See workflow comments in `.github/workflows/{test,quality,security}-*.yml`.
