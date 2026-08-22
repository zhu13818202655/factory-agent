# report-agent Migration Inventory

This inventory characterizes `/home/admin2/proj/report-agent` as a source of behavior only. The
factory-agent repository must not import it or its local Vanna fork. Paths below are source
provenance, not implementation authority.

## Characterization Checklist

### State and orchestration

- [ ] Characterize every legal and illegal transition in `src/report_agent/state_machine.py`.
- [ ] Prove `FAILED` is reachable from every non-terminal state and terminal states cannot restart.
- [ ] Verify transition history preserves from-state, to-state, and reason.
- [ ] Verify parsing, clarification, authorization, fetch, analysis, and preview ordering.
- [ ] Verify authorization denial causes zero downstream data calls.

### Follow-up context

- [ ] Verify an explicit follow-up period replaces the previous period.
- [ ] Verify empty patch lists and null fields do not erase prior values.
- [ ] Verify merge resets stale missing/conflict results.
- [ ] Verify clarification stops at the configured maximum rounds.
- [ ] Verify history trimming removes oldest complete turns and respects the character budget.
- [ ] Verify artifact-bearing replies are summarized without retaining detail rows.

### Interaction and transport

- [ ] Verify a streaming request persists its interaction and initial user message first.
- [ ] Verify SSE starts with `interaction.started` and emits exactly one terminal event.
- [ ] Verify cancellation persists a cancelled terminal state.
- [ ] Verify non-finite JSON values are sanitized before SSE serialization.
- [ ] Verify unauthorized session access is indistinguishable from not found.
- [ ] Verify message sequence values remain unique within an interaction.

### Persistence

- [ ] Characterize repository get, upsert, user-scoped listing, and cursor pagination.
- [ ] Characterize interaction, message, artifact, and audit cascade behavior.
- [ ] Run source Alembic upgrade/downgrade tests against a clean disposable PostgreSQL database.
- [ ] Explicitly exclude unrelated legacy `tasks`, `runs`, `subtasks`, and `replays` tables.
- [ ] Replace caller-supplied tenant/user query filters with trusted identity and scope values.

### Model gateway

- [ ] Characterize empty request, timeout, HTTP error, malformed JSON, missing choice, and missing
  content failures from `src/report_agent/llm/`.
- [ ] Characterize raw, fenced, and embedded JSON extraction.
- [ ] Record and reject the classifier's fallback-to-report behavior for factory-agent.
- [ ] Prove sensitive values do not enter model requests, logs, errors, or snapshots.

### Export and SQL security

- [ ] Characterize strict template failures and renderer selection independently of local paths.
- [ ] Characterize path traversal rejection, but replace filesystem ownership with `ArtifactStore`.
- [ ] Retain SQL guard tests as security references only; do not migrate SQL execution.
- [ ] Prove factory-agent production code imports no `report_agent`, `vanna`, or `mock_mes`.

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
| `src/report_agent/models/event.py` | Adapt | Versioned allowlist usage-event envelope. |
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
