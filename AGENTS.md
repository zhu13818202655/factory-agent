# AGENTS.md - factory-agent

## Mission

`factory-agent` is a read-only factory MES assistant. It produces authorized, auditable,
reproducible answers grounded in validated API data.

## Sources of Truth

Use these sources in order when requirements conflict:

1. The current numbered Story under `.github/story/`, including its scope, assumptions, and tasks.
2. `SECURITY.md` and accepted permission/data-classification ADRs.
3. OpenAPI and JSON Schema under `contracts/`.
4. Accepted requirements under `docs/product/`.
5. `ARCHITECTURE.md` and accepted ADRs under `docs/adr/`.
6. This file and the nearest scoped `AGENTS.md`.
7. Existing implementation patterns.

Files under `docs/requirements/source/` and `docs/reference/source-plans/` preserve provenance.
They are not implementation authority and may contain stale assumptions.

Do not silently resolve a conflict in a lower-priority source. Record the conflict and stop.

## Architecture Invariants

1. Authorization completes before any business-data API call.
2. One deployment serves many factory tenants. **AppKey is the tenant ID (one factory, one
   AppKey)**. Each MES business interaction binds a trusted `TenantContext` derived from the
   customer credential bundle (`accessToken`, plaintext `app_key`, `sign`, `timestamp`, `user`,
   `uname`), and its `tenant_id`, `employee_ids`, and `dept_ids` come from `DataScope`.
   Row-level filtering beyond the caller's own record is performed by the customer MES; our
   `DataScope` records this as `mes_filtered` and never claims that wider range itself.
   Platform operations use a separate `PlatformScope`. User or LLM output can never broaden or
   replace either scope.
3. Only `src/factory_agent/data_api/` may call MES HTTP endpoints. Customer field names, endpoint
   paths, credential values, and `code`/`message` semantics must not leave that package.
4. Application and domain code depend on `MesDataSource`, never customer URLs or payloads.
5. Validate every external response before registering it in the local sandbox.
6. Use one isolated DuckDB connection per interaction and destroy it afterward.
7. Sensitive fields never enter LLM prompts, logs, traces, errors, or test snapshots.
8. L1 capabilities use reviewed deterministic DAGs. L2 uses the same bounded executor.
9. Local SQL is read-only and cannot access files, extensions, external scans, DDL, or DML.
10. All MES operations are read-only. Coding agents never receive production credentials.

## Repository Boundaries

- `src/factory_agent/`: production application built by the root project.
- `mock-mes/`: self-contained simulator; never a production dependency.
- `usage-admin/`: independently built production service for authorized multi-tenant usage
   aggregation, operational APIs, and reports; it never calls MES endpoints.
- `contracts/`: versioned OpenAPI and JSON Schema compatibility boundary.
- `configs/knowledge/`: reviewed API catalog, metrics, and L1 DAGs.
- `tests/support/`: in-process fakes and pytest-managed test processes, not services.
- `data/`: ignored runtime output only.

`factory_agent`, `mock_mes`, and `usage_admin` must never import each other. They communicate only
through versioned HTTP and event contracts. Production Compose excludes Mock MES but includes
`usage-admin` when usage metering is enabled. Read the nearest scoped `AGENTS.md` before modifying a
governed directory.

## Story Workflow

1. Work through `.github/story/#1.md`, `#2.md`, and so on in numeric order.
2. Read the whole current Story, then implement its checklist directly. A Story may group work in
   parent and child checklist items and may include ADO-style state, dependencies, acceptance
   criteria, risks/open decisions, Technology Notes, and Release Notes when they improve execution
   or review.
3. Checklist evidence remains the completion source of truth. Mark a child item `[x]` only after its
   implementation is complete, and mark a parent item `[x]` only after all of its child items are
   complete. Leave unfinished items unchecked.
4. When a Story uses ADO-style state, use `New -> Active -> Resolved -> Closed`: implementation starts
   at `Active`, reaches `Resolved` only after its checklist and relevant engineering checks are
   complete, and reaches `Closed` only after human review. State never overrides checklist evidence.
5. When a customer API or business rule is unavailable, continue against the contract in
   `contracts/` and Mock MES, and keep the temporary assumption visible in the Story or relevant
   product document. Confirmed customer facts live in
   `docs/reference/弘兆MES接口整体说明-V2.md` (M/K identifiers); unconfirmed calculations must
   surface as an explicit `unavailable` state rather than a fabricated number.
6. After the Story checklist is complete, summarize the result for the user. The user reviews the
   implementation manually and decides whether follow-up changes are needed.

## Development Commands

```bash
make bootstrap
make lint
make typecheck
make test-unit
make test-contract
make test-integration
make test-e2e
make security
make check
```

Run the repository commands relevant to the code changed. `make check` remains the convenient full
code-quality check; it does not evaluate Story status or decide whether a Story is complete.

## Git Commit Workflow

Since code modifications are typically performed under the `root` user, but the Git repository for this project is owned by the `admin2` user, you must strictly follow this permission-switching process before every Git commit:

1. Transfer ownership of all files in the current directory to the `admin2` user (e.g., by running `chown -R admin2:admin2 .`).
2. Switch to the `admin2` user (e.g., by running `su admin2`).
3. Execute `git add`, `git commit`, and `git push` within the `admin2` user context.

**Note:** Never execute Git commit operations directly under the `root` user to avoid permission conflicts or abnormal repository states.

## Coding Standards

- Python 3.12+, uv, Ruff, Pyright strict mode, pytest, and typed public boundaries.
- Put `from __future__ import annotations` at the top of Python modules.
- Use Pydantic models at external boundaries and immutable domain values internally.
- Inject clocks, IDs, clients, repositories, and model gateways in tested code.
- Use `Decimal` with explicit rounding for money and timezone-aware datetimes.
- Do not add an abstraction without a current second use or a boundary reason.
- Generated clients and `uv.lock` change only through their generators or uv.

## Permission and Privacy Tests

Every data capability must prove:

1. Callers receive only their effective scope.
2. Denied requests perform zero business-data API calls.
3. User-provided IDs cannot override `DataScope`.
4. IDs outside the active `DataScope` never enter MES calls, sandbox tables, caches, artifacts, or
   business audit details; authorized platform aggregation never enters the MES execution path.
5. Credential values (`app_key`, `accessToken`, `sign`, `movepassword`) and sensitive canary values
   never appear in captured LLM requests, logs, traces, errors, events, or test snapshots.
6. A range narrowed by MES-side filtering is reported as a structured state, never presented as a
   complete result.

## LLM Rules

- Product code calls one LiteLLM OpenAI-compatible gateway using logical model aliases.
- Provider credentials, network retries, and provider fallback belong to LiteLLM.
- Schema validation and at most one semantic repair belong to the application.
- An LLM cannot construct raw URLs, auth headers, or unrestricted SQL.
- Final answers cite only values from `ResultTable` or approved metadata.
- Unit tests inject an in-process fake. Routing tests start temporary upstream processes.

## Security Stop Conditions

Stop for human approval before changing role/scope semantics, sensitive-field classification,
retention, outbound hosts, credentials, DuckDB filesystem access, destructive migrations,
major dependencies, model providers, customer environments, or deployment state.

## Story Review

Stories have no machine status, score, risk assessment, or automated completion gate. Keep the
checklist honest, record any remaining assumptions, run relevant engineering checks, and hand the
result to the user for manual review.
