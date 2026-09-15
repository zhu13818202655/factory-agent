# ADR-0002: Runtime, Storage, And Migration Baseline

- Status: Accepted
- Date: 2026-08-21
- Owners: Project maintainers

## Context

The customer MES integration boundary comes from the reviewed interface contract
(`docs/product/AI问答对外接口-整理.md`). The application needs a deliberate runtime, storage, and
migration baseline shared by `factory-agent` and `usage-admin`.

## Decision

- Build the application with Python 3.12, FastAPI, Pydantic v2, HTTPX, uv, Ruff, Pyright, and pytest.
- Use PostgreSQL 16 with Psycopg 3 and Alembic for durable application metadata. `factory-agent`
  and `usage-admin` run against the same PostgreSQL database, each with its own migration history
  and Alembic version table.
- Every table has exactly one owner that holds its DDL and CRUD while the other service only reads.
  usage-admin owns `tenant_registry`, `admin_audit`, `platform_principal`, and `usage_export`;
  factory-agent owns the business tables (`agent_*`) and every metering table (`usage_event`,
  `*_fact`, `mes_operation_category`, `tenant_usage_*`). No table may appear in both migration
  histories.
- Keep each service's Alembic version table separate: usage-admin uses
  `alembic_version_usage_admin` while factory-agent uses the default `alembic_version`. A shared
  version table would make each service abort on the other's revision ids, because the two revision
  histories are unrelated.
- Use one in-memory DuckDB connection per interaction for bounded processing of validated,
  authorized data. DuckDB is not a durable store.
- Model access follows ADR-0006: the application embeds the LiteLLM Router SDK and owns the
  reviewed model deployment registry (`configs/knowledge/models.yaml`). Business code only names
  logical aliases; provider keys, fallback, and retry policy are owned per ADR-0006.
- Caching is an optional optimization and never authoritative. First-release caches are TTL-based
  application caches (see `src/factory_agent/application/cache.py`); a dedicated store such as
  Redis is introduced only when measurements justify it.
- Export renders with XlsxWriter from `ResultTable` (see `src/factory_agent/export/`). Delivery
  follows the customer-confirmed retention policy: artifacts persist server-side with a 90-day
  default retention window (报表导出与文件留存策略 in
  `docs/product/需求及方案整理.md`).
- Emit structured JSON logs and OpenTelemetry-compatible telemetry after applying data
  classification and redaction (ADR-0004).
- Use OCI images and Docker Compose for development and integration. Do not select a production
  orchestrator, managed database, object-storage product, or telemetry backend in this ADR.
- Write metering facts directly into the shared database after factory-agent's business commit, in
  a separate transaction; usage-admin only reads the metering tables. There is no HTTP usage-event
  contract between the services (ADR-0003 §3.1).
- Reject Vanna, Text-to-SQL production execution, direct PostgreSQL/TDengine business queries,
  flight prompts/analyzers/charts, DOCX/PDF renderers, and request-body identity.

## Consequences

- factory-agent and usage-admin share one PostgreSQL database with separate credentials and
  migration lifecycles. Every shared table has exactly one owner:
  usage-admin owns `tenant_registry`, `admin_audit`, `platform_principal`, and `usage_export`;
  factory-agent owns and writes the business and metering tables that usage-admin only reads. Schema
  changes to shared tables require synchronized review by both services.
- Separate Alembic version tables let both services run migrations against one database in any
  order (usage-admin pins `alembic_version_usage_admin`; factory-agent uses the default
  `alembic_version`). Each service may only create, alter, or drop the tables it owns; table
  prefixes make ownership visible in review.
- New dependencies are added when the code that needs them is implemented, not preinstalled for
  skeleton packages.

## Approval Gates

Human approval remains required before selecting or changing customer authentication, role/scope
semantics, sensitive-field classification, production outbound hosts, credentials, model providers,
production object storage, production orchestration, retention enforcement, or deployment state.

## Revisit When

Revisit this baseline when customer API constraints make a selected Adapter boundary impossible,
measured workloads invalidate PostgreSQL/DuckDB/cache assumptions, the frontend requires a different
streaming contract, or production platform requirements are delivered.
