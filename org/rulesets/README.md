# lgtm-hq organization rulesets (winnow)

Source-of-truth payload for the **`checks-winnow`** org ruleset. Synced to GitHub with
`lgtm-ci`’s `scripts/ci/org/sync-ruleset.sh` (org admin + `gh` auth required).

Required status checks on the default branch:

| Gate job (workflow)       | Workflow file        | GitHub check context                             |
| ------------------------- | -------------------- | ------------------------------------------------ |
| `test-suite-coverage`     | `test-ci.yml`        | `test-suite-coverage / 🧪 Test Suite & Coverage` |
| `security-audit-required` | `security-audit.yml` | `🔐 Security Audit`                              |
| `lintro-code-quality`     | `quality-ci.yml`     | `lintro-code-quality / 🛠️ Lintro Code Quality`   |

See org ruleset comments on those jobs in `.github/workflows/`. The filename column
lists the specific workflow files referenced by the glob
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

These mirror the `bypass_actors` entries in `checks-py-lintro.json` and other org check
rulesets.
