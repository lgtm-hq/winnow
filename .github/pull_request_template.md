<!-- markdownlint-disable MD041 -- PR template does not start with a top-level heading -->

## Commit Summary (Conventional Commits)

- Title (required, present tense):

  ```text
  <type>(optional-scope)!: concise summary
  ```

  Examples: `feat(cli): add --group-by`, `fix(parser): handle empty config`,
  `refactor(core)!: rewrite engine`

- Type:
  - [ ] feat (minor)
  - [ ] fix / perf (patch)
  - [ ] docs
  - [ ] refactor
  - [ ] test
  - [ ] chore / ci / style

- Breaking change:
  - [ ] `!` in title or `BREAKING CHANGE:` footer included

### Release Trigger Rules (exact)

- A merged PR will bump the version based on its title (squash merge required):
  - `feat(...)` or `feat:` → MINOR bump
  - `fix(...)` / `fix:` or `perf(...)` / `perf:` → PATCH bump
  - Any title with `!` after the type (e.g. `feat!:` or `feat(scope)!:`) or a body
    containing `BREAKING CHANGE:` → MAJOR bump
- Use squash merge so the PR title becomes the merge commit title.
- Valid examples:
  - `feat(cli): add --group-by`
  - `fix(parser): handle empty config`
  - `perf: optimize grouping performance`
  - `feat(api)!: remove deprecated flags`

## What’s Changing

Describe the changes and why.

## Checklist

- [ ] Title follows Conventional Commits
- [ ] Tests added/updated
- [ ] Docs updated if user-facing
- [ ] Local CI passed (`make lint && make test`)

## Closes

<!-- Use `- Closes #123` for issues fully resolved by this PR. -->

- Closes #

## Details

Implementation notes, migration/breaking notes, and testing strategy.
