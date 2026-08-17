# Mock MES Rules

These rules apply to `mock-mes/`.

- This project is a deterministic simulator, not a production dependency or product shortcut.
- Never import `factory_agent`; compatibility is verified through the Canonical OpenAPI.
- Generated data is determined by `(scenario, seed, virtual_now)`.
- Preserve tenant, relation, quantity, payroll, and effective-date invariants.
- Use PostgreSQL migrations for schema changes; startup code never creates production tables.
- Admin clock and fault endpoints are enabled only in test or development environments.
- Resource endpoints support explicit pagination, `total`, and batch ID filters.
- Include hard cases without silently changing an existing fixture's expected business numbers.
- Fault behavior must be opt-in and scoped to a test, request, or resettable scenario.
- Add model/generator unit tests and Canonical consumer contract tests for every endpoint.
