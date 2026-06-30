# Architecture Decision Records (ADR)

This directory contains Architecture Decision Records (ADRs) for the winnow project.

## What is an ADR?

An Architecture Decision Record (ADR) is a document that captures an important
architectural decision made along with its context and consequences.

## Why Use ADRs?

1. **Historical Context** — Understand why decisions were made
2. **Knowledge Transfer** — Onboard new contributors faster
3. **Prevent Revisiting** — Avoid rehashing settled discussions
4. **Transparency** — Make decision-making visible
5. **Learning** — Reflect on past decisions and their outcomes

## ADR Format

We use a simplified ADR format with the following sections:

1. **Title** — Short noun phrase
2. **Status** — Proposed, Accepted, Deprecated, Superseded
3. **Context** — What is the issue we're addressing?
4. **Decision** — What decision did we make?
5. **Consequences** — What are the trade-offs and impacts?

## Naming Convention

ADRs are numbered sequentially:

```text
0001-api-first-platform.md
0002-example-decision.md
```

Format: `NNNN-title-with-dashes.md`

## Workflow

### Creating a New ADR

1. Copy `template.md` to a new file
2. Increment the number from the last ADR
3. Fill in the sections
4. Open a pull request
5. Discuss and refine
6. Merge when consensus is reached

### Superseding an ADR

When a decision is no longer valid:

1. Create a new ADR with the updated decision
2. Update the old ADR:
   - Change status to "Superseded by ADR-NNNN"
   - Add link to new ADR
3. Keep the old ADR for historical reference

### Example

```markdown
# Status

~~Accepted~~ Superseded by [ADR-0002](0002-new-decision.md)

## Superseded

This decision was superseded on 2026-06-01 by [ADR-0002](0002-new-decision.md)
because...
```

## Index of ADRs

| ADR                                | Title                                 | Status   | Date       |
| ---------------------------------- | ------------------------------------- | -------- | ---------- |
| [0001](0001-api-first-platform.md) | API-First Platform, CLI-First Phasing | Accepted | 2026-06-30 |

## Template

See [template.md](template.md) for the ADR template.

## Resources

- [Michael Nygard's ADR Article](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [ADR GitHub Organization](https://adr.github.io/)
- [ADR Tools](https://github.com/npryce/adr-tools)

## Questions?

If you have questions about ADRs:

- Review existing ADRs for examples
- Check the template
- Open an issue with the `question` label
