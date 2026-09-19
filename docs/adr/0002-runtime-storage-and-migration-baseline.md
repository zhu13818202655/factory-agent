# ADR-0002: Runtime, Storage, And Migration Baseline

- Status: Accepted
- Date: 2026-08-21
- Owners: Project maintainers

## Context

The customer MES integration boundary comes from the reviewed interface contract
(`docs/product/AI问答对外接口-整理.md`). The application needs a deliberate runtime, storage, and
migration baseline covering both the business surface and the platform statistics surface.

## Decision

- Build the application with Python 3.12, FastAPI, Pydantic v2, HTTPX, uv, Ruff, Pyright, and pytest.
- Use PostgreSQL 16 with Psycopg 3 and Alembic for durable application metadata. One database and
  one database user (`FACTORY_AGENT_POSTGRES_URL`) serve everything, built by **one** Alembic
  baseline and recorded in the single default `alembic_version` table.
- Every table has exactly one writer. The statistics surface owns `tenant_registry`,
  `admin_audit`, `platform_principal`, and `usage_export`; the business side owns the business
  tables (`agent_*`) and every metering table (`usage_event`, `*_fact`,
  `mes_operation_category`, `tenant_usage_*`). Business code reads the statistics surface's tables
  read-only, and only through `src/factory_agent/ports/tenant_registry.py`.
- Keep one migration history: `migrations/versions/` holds a single baseline revision with
  `down_revision = None`, so an empty database reaches the full schema in one `upgrade head`.
  Separate per-service version tables are gone — they only existed because two unrelated revision
  chains shared a database.
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
- Write metering facts directly into PostgreSQL after the business commit, in a separate
  transaction; the statistics surface only reads the metering tables. There is no HTTP usage-event
  contract (ADR-0003 §3.1).
- Give the statistics surface its own database access profile: separate connections and pool, a
  `statement_timeout` on every statement, and read-only read transactions. It never shares the
  session pipeline's engine or pool.
- Maintain the `usage_event` monthly partitions from the application: seed the current and next
  month in the migration, then keep that window present with a lifespan periodic task
  (ADR-0003 §7).
- Reject Vanna, Text-to-SQL production execution, direct PostgreSQL/TDengine business queries,
  flight prompts/analyzers/charts, DOCX/PDF renderers, and request-body identity.

## Consequences

- One PostgreSQL database with one credential and one migration lifecycle. Every table has exactly
  one writer: the statistics surface owns `tenant_registry`, `admin_audit`, `platform_principal`,
  and `usage_export`; the business side owns and writes the business and metering tables that the
  statistics surface only reads. Schema changes to `tenant_registry` require review by both
  readers.
- A single baseline keeps schema review honest: an empty database reaches the full schema in one
  `upgrade head`, and `downgrade base` reverses it. Module ownership is expressed by table-name
  prefixes (`agent_*` for business, bare names for the statistics surface's own tables) and by
  which module holds the DDL, since only one module may create, alter, or drop each table.
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
