# ADR 0001: API-First Platform, CLI-First Phasing

## Status

Accepted — June 30, 2026

## Context

- winnow is a greenfield rewrite of py-organizer.
- py-organizer accumulated 56% of its lines of code in `utils/` and `cli_utils/`, with
  unclear module boundaries and duplicated logic across CLI and future UI paths.
- winnow needs a stable integration surface (OpenAPI) and a delivery plan that ships
  value before a web UI exists.
- The target deployment model remains local-first and offline-capable.

## Decision

### Core architecture

- Place all business logic in the `winnow/` core package.
- Treat the CLI and HTTP API as thin adapters that delegate to a shared service layer.
- Make the OpenAPI schema a first-class deliverable; API contracts are the stable
  integration point for CLI, automation, and future UI.

### Module boundary rules

- Do not introduce a grab-bag `utils/` directory.
- Split modules by domain from day one (e.g., scanning, grouping, reporting, jobs).
- Adapters (`cli/`, `api/`) may contain parsing, routing, and presentation only — not
  domain rules.

### Error handling and typing

- No bare `except:` clauses; catch specific exceptions or re-raise with context.
- Use context managers for file handles and other resources that require cleanup.
- Enforce strict mypy with zero `# type: ignore` comments.

### Domain models

- Use Pydantic v2 for all domain models and API request/response schemas.

### Delivery phasing

- **Phase 1:** CLI + API — both consume the same service layer; ship core workflows
  without a web frontend.
- **Phase 2:** Web UI — presentation-only; communicates exclusively via HTTP to the API;
  no direct access to core internals or storage.

## Consequences

### Positive

- CLI and API stay aligned because they share one service layer and one contract.
- Domain logic is testable without subprocess or HTTP harnesses.
- OpenAPI enables client generation, contract tests, and UI work in parallel later.
- Module boundaries prevent the utils sprawl that dominated py-organizer.

### Negative

- Initial API design work precedes some CLI shortcuts that would be faster in a CLI-only
  prototype.
- Strict typing and Pydantic models add upfront ceremony for simple data shapes.

### Neutral

- Phase 2 UI must not bypass the API; any capability the UI needs must exist as an
  endpoint first.
- New domains should extend core modules and services before adding adapter code.

## References

- Parent epic: [#2](https://github.com/lgtm-hq/winnow/issues/2)
- py-organizer precedent:
  [ADR 0001 — FastAPI-first report platform](https://github.com/lgtm-hq/py-organizer/blob/main/docs/adr/0001-report-platform-fastapi.md)
