# lgtm-hq organization rulesets (winnow)

Source-of-truth payload for the **`checks-winnow`** org ruleset. Synced to GitHub with
`lgtm-ci`’s `scripts/ci/org/sync-ruleset.sh` (org admin + `gh` auth required).

Required status checks on the default branch:

| Required check            | Workflow file        | GitHub check context                           |
| ------------------------- | -------------------- | ---------------------------------------------- |
| `test`                    | `test-ci.yml`        | `test / Python Compatibility`                  |
| `security-audit-required` | `security-audit.yml` | `🔐 Security Audit`                            |
| `lintro-code-quality`     | `quality-ci.yml`     | `lintro-code-quality / 🛠️ Lintro Code Quality` |

The test check is reported directly by `reusable-test-python.yml`
(`job-name: Python Compatibility`). Quality and security still use thin gate jobs where
the ruleset display name differs from the work reusable check path.

The filename column lists workflow files under
`.github/workflows/{test,quality,security}-*.yml`.

## Bypass configuration

`checks-winnow.json` allows these actors to bypass required checks on pull requests
(`bypass_mode: pull_request`):

| Actor type          | `actor_id` | Meaning                      |
| ------------------- | ---------- | ---------------------------- |
| `OrganizationAdmin` | `null`     | Organization administrators  |
| `RepositoryRole`    | `2`        | Triage — allowed PR bypass   |
| `RepositoryRole`    | `4`        | Maintain — allowed PR bypass |
| `RepositoryRole`    | `5`        | Admin — allowed PR bypass    |

These mirror the `bypass_actors` entries in `checks-winnow.json` and other org check
rulesets.
