# AGENTS.md - factory-agent

## Mission

`factory-agent` is a read-only factory MES assistant. It produces authorized, auditable,
reproducible answers grounded in validated API data.

## Sources of Truth

Use these sources in order when requirements conflict:

1. A ready Story under `workitems/stories/`, especially `acceptance` and `non_goals`.
2. `SECURITY.md` and accepted permission/data-classification ADRs.
3. OpenAPI and JSON Schema under `contracts/`.
4. `ARCHITECTURE.md` and accepted ADRs under `docs/adr/`.
5. This file and the nearest scoped `AGENTS.md`.
6. Existing implementation patterns.

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

Before editing:

1. Read the ready Story and its dependencies.
2. Verify required contracts and business definitions exist.
3. Name one falsifiable hypothesis and the cheapest check that could disprove it.
4. Make the smallest grounded edit and immediately run the closest focused validation.

Do not modify a ready Story's `goal`, `acceptance`, `non_goals`, or risk level. When blocked,
record the exact missing decision; never invent customer fields, formulas, or permissions.

## Required Commands

```bash
make bootstrap
make policy
make lint
make typecheck
make test-unit
make test-contract
make test-integration
make test-e2e
make security
make check
```

Use these repository commands instead of private command sequences. Fix a broken command as
part of the infrastructure Story.

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

## Definition of Done

A Story is complete only when its immutable acceptance commands pass, positive and negative
security paths are tested, relevant contracts and ADRs are current, model changes include eval
deltas, an independent reviewer has no unresolved high/medium findings, and `make check` passes.
