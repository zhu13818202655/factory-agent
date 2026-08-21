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
2. `tenant_id`, `employee_ids`, and `dept_ids` come from `DataScope`. User or LLM output
   may narrow an authorized scope but can never broaden or replace it.
3. Only `src/factory_agent/data_api/` may call MES HTTP endpoints.
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
- `contracts/`: versioned OpenAPI and JSON Schema compatibility boundary.
- `configs/knowledge/`: reviewed API catalog, metrics, and L1 DAGs.
- `tests/support/`: in-process fakes and pytest-managed test processes, not services.
- `data/`: ignored runtime output only.

`factory_agent` and `mock_mes` must never import each other. They communicate only through
HTTP contracts. Read the nearest scoped `AGENTS.md` before modifying a governed directory.

## Story Workflow

1. Work through `.github/story/#1.md`, `#2.md`, and so on in numeric order.
2. Read the whole current Story, then implement its checklist directly. Do not split it into child
   work items or add dependency graphs, readiness states, risk scores, or acceptance sections.
3. Mark a task `[x]` only after its implementation is complete. Leave unfinished tasks unchecked.
4. When a customer API or business rule is unavailable, continue against the Canonical contract and
   Mock MES, and keep the temporary assumption visible in the Story or relevant product document.
5. After the Story checklist is complete, summarize the result for the user. The user reviews the
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

1. Allowed roles receive only their effective scope.
2. Denied roles perform zero business-data API calls.
3. User-provided IDs cannot override `DataScope`.
4. Cross-tenant IDs never enter calls, sandbox tables, caches, artifacts, or audits.
5. Sensitive canary values never appear in captured LLM requests or logs.

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
