# report-agent Migration Inventory

This inventory characterizes `/home/admin2/proj/report-agent` as a source of behavior only. The
factory-agent repository must not import it or its local Vanna fork. Paths below are source
provenance, not implementation authority.

## Characterization Checklist

Items marked complete are proven by `tests/characterization/`, `tests/unit/`, `tests/contract/`, or
`tests/integration/` in factory-agent. The characterization baseline is a frozen, read-only snapshot
(`tests/characterization/report_agent_baseline.json`); it is never re-read from the source tree at
test time and does not update when the source repository changes.

### State and orchestration

- [x] Characterize every legal and illegal transition in `src/report_agent/state_machine.py`.
- [x] Prove `FAILED` is reachable from every non-terminal state and terminal states cannot restart.
- [x] Verify transition history preserves from-state, to-state, and reason.
- [x] Verify parsing, clarification, authorization, fetch, analysis, and preview ordering.
- [x] Verify authorization denial causes zero downstream data calls.

### Follow-up context

- [x] Verify an explicit follow-up period replaces the previous period.
- [x] Verify empty patch lists and null fields do not erase prior values.
- [x] Verify merge resets stale missing/conflict results.
- [x] Verify clarification stops at the configured maximum rounds.
- [x] Verify history trimming removes oldest complete turns and respects the character budget.
- [x] Verify artifact-bearing replies are summarized without retaining detail rows.

### Interaction and transport

- [x] Verify a streaming request persists its interaction and initial user message first.
- [x] Verify SSE starts with `interaction.started` and emits exactly one terminal event.
- [x] Verify cancellation persists a cancelled terminal state.
- [x] Verify non-finite JSON values are sanitized before SSE serialization.
- [x] Verify unauthorized session access is indistinguishable from not found.
- [x] Verify message sequence values remain unique within an interaction.

### Persistence

- [x] Characterize repository get, upsert, user-scoped listing, and cursor pagination.
- [x] Characterize interaction, message, artifact, and audit cascade behavior.
- [x] Run source Alembic upgrade/downgrade tests against a clean disposable PostgreSQL database.
- [x] Explicitly exclude unrelated legacy `tasks`, `runs`, `subtasks`, and `replays` tables.
- [x] Replace caller-supplied tenant/user query filters with trusted identity and scope values.

### Model gateway

- [x] Characterize empty request, timeout, HTTP error, malformed JSON, missing choice, and missing
  content failures from `src/report_agent/llm/`.
- [x] Characterize raw, fenced, and embedded JSON extraction.
- [x] Record and reject the classifier's fallback-to-report behavior for factory-agent.
- [x] Prove sensitive values do not enter model requests, logs, errors, or snapshots.

### Export and SQL security

- [ ] Characterize strict template failures and renderer selection independently of local paths.
- [ ] Characterize path traversal rejection, but replace filesystem ownership with `ArtifactStore`.
- [ ] Retain SQL guard tests as security references only; do not migrate SQL execution.
- [x] Prove factory-agent production code imports no `report_agent`, `vanna`, or `mock_mes`.

## Deviations Recorded While Completing the Checklist

Three items were satisfied differently from their original wording. They are marked complete because
the underlying risk is covered, but the difference is deliberate and belongs in review.

| Item | What was actually done |
| :--- | :--- |
| Analysis and preview ordering | factory-agent has no analysis or preview stage. The source `FETCHING -> ANALYZING -> PREVIEWING -> RENDERING` chain collapses into `EXECUTING -> COMPOSING`, because bounded execution returns a `ResultTable` rather than a rendered report. Ordering is verified for the stages that survive: parse, clarify, authorize, execute, compose. |
| Artifact and audit cascade | Interaction, message, and SSE-event cascade is proven on real PostgreSQL. Artifact and audit tables do not exist yet; their cascade is deferred to the Story that introduces export and durable audit. |
| Source Alembic upgrade/downgrade | The Source Decisions table rejects direct migration of the source revisions, so running them would prove nothing about factory-agent. The equivalent guarantee is proven instead against factory-agent's own rebuilt baseline in `tests/integration/test_session_migration.py`, which upgrades and downgrades a clean disposable PostgreSQL database and reflects the result back to detect drift from `persistence/tables.py`. |

The remaining unchecked items are export and SQL-guard concerns that belong to the Story that
introduces result rendering and artifact download.

## Source Decisions

| Source | Decision | Factory-agent target and constraint |
| :--- | :--- | :--- |
| `src/report_agent/state_machine.py` | Adapt | Domain state values and application executor; retain tested transitions, rename report states. |
| `src/report_agent/service.py` pipeline | Adapt selectively | Bounded application orchestration; authorization remains before every business-data call. |
| `src/report_agent/schemas.py` interaction/message models | Adapt | Stable IDs, sequence, lifecycle, and timestamps; reject request identity and flight fields. |
| `FilterSpec`, `DraftFilterSpec`, report fields | Reject as-is | Replace with reviewed Canonical resource inputs and trusted `DataScope`. |
| `src/report_agent/context.py` | Adapt | Bounded history without prior detail rows or authorization scope. |
| `src/report_agent/conflict_checker.py` | Adapt behavior | Patch only explicitly supplied fields; replace report-specific types. |
| `src/report_agent/repository.py` | Adapt interface | Session/repository ports; every durable query derives tenant and user ownership from trusted context. |
| `alembic/versions/*` | Reject direct migration | Rebuild factory-agent migrations; source baseline mixes unrelated legacy tables. |
| `src/report_agent/api/router.py` | Adapt transport | API/SSE framing and terminal semantics only; never trust request `tenant_id` or `user_id`. |
| `src/report_agent/api/server.py` | Adapt composition | Factory composition root, bounded startup, and explicit readiness. |
| `src/report_agent/llm/types.py` | Adapt | Typed model gateway requests, responses, and failures. |
| `src/report_agent/llm/client.py` | Reject direct client | The application calls only the configured LiteLLM logical-alias gateway. |
| `src/report_agent/export/*` | Adapt selectively | Renderer separation behind `ResultTable` and `ArtifactStore`; reject local path ownership. |
| `src/report_agent/permissions.py` | Reject | `AllowAllPermissionGate` is prohibited; replace with reviewed identity and scope semantics. |
| `src/report_agent/dikong_sql/*` | Reject | MES access is Canonical HTTP through `factory_agent.data_api` only. |
| `src/report_agent/text2sql/*` | Reject | No unrestricted SQL or Vanna runtime dependency. |
| `src/report_agent/models/event.py` | Adapt | `UsageEvent` archive payload in `ports/session.py`, versioned by `SCHEMA_VERSION`, written directly to the metering tables (no cross-service contract). |
| `src/report_agent/models/table.py` | Adapt | Approved `ResultTable` contract in its owning Story. |
| `libs/vanna/*` | Reject dependency | No direct, indirect, editable, or path-based dependency. |

## Local Vanna Fork Evidence

The source repository contains `libs/vanna/` as a nested Git checkout. Its package metadata declares
`vanna` 2.0.2, while source instructions install it separately with
`uv pip install -e ./libs/vanna`. Direct imports occur in `text2sql/agent.py`, `text2sql/tools.py`,
and `text2sql/llm.py`. This fork, its filesystem and PostgreSQL runners, memory tools, and user
resolver are outside the factory-agent runtime boundary.

## Temporary Assumptions

- Source behavior is characterized before selective migration; this document does not approve a
  source behavior as a customer rule.
- Factory resource filters and clarification fields wait for the Canonical contract and product
  confirmation.
- No report-agent migration may weaken authorization, tenant isolation, sensitive-data filtering,
  or the read-only MES boundary.
