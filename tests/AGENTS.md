# Test Rules

These rules apply to `tests/` and `mock-mes/tests/`.

- Unit tests use no network and no real database. In-process ASGI transport is allowed.
- Contract tests verify OpenAPI/JSON Schema compatibility and Adapter consumer behavior.
- Integration tests use real PostgreSQL, Redis, DuckDB, or pytest-managed HTTP processes.
- E2E tests cover user text through authorization, orchestration, result, audit, and export.
- Do not assert only status codes; assert scope, calls, rows, audit, and grounded values.
- A denied authorization test must assert zero downstream business-data calls.
- Never place production-like personal data, secrets, or tokens in fixtures or snapshots.
- LLM unit tests use deterministic in-process fakes. Temporary upstreams bind random ports and
  are always cleaned up by fixtures.
- Golden and eval baselines change only with an explained, reviewed behavior change.
- Keep tests deterministic by injecting time, random seeds, IDs, and external boundaries.
