# ADR-0002: Runtime, Storage, And Migration Baseline

- Status: Proposed
- Date: 2026-08-21
- Owners: Project maintainers

## Context

The customer MES API is not available, but the product capabilities and the API-only integration
boundary are sufficiently clear to build the complete application skeleton. The repository also
needs a deliberate migration path from `/home/admin2/proj/report-agent`; copying that application
would import direct business-database access, request-supplied identity, allow-all authorization,
flight-domain code, and process-local streaming behavior that conflict with factory-agent invariants.

## Decision

- Build the application with Python 3.12, FastAPI, Pydantic v2, HTTPX, uv, Ruff, Pyright, and pytest.
- Use PostgreSQL 16 with Psycopg 3 and Alembic for durable application metadata. Run Mock MES against
  a separate PostgreSQL database and migration history. Run usage-admin and factory-agent against
  the same PostgreSQL database, each with its own migration history and Alembic version table
  (Story 11 direct write). usage-admin owns the tenant registry table `tenant_registry` (factory
  name, AppKey, status) — its schema, migrations, and writes — and factory-agent reads it read-only
  to resolve the AppKey for MES calls. Every other table is owned by exactly one service.
- When services share one database, every table has exactly one owner that holds its DDL and CRUD
  while the other service only reads. usage-admin owns `tenant_registry`, `admin_audit`,
  `platform_principal`, and `usage_export`; factory-agent owns the business tables (`agent_*`) and
  every metering table (`usage_event`, `*_fact`, `mes_operation_category`, `tenant_usage_*`). No
  table may appear in both migration histories.
- Keep each service's Alembic version table separate: usage-admin uses
  `alembic_version_usage_admin` while factory-agent uses the default `alembic_version` (Story 11
  reverted its one-off `alembic_version_factory_agent`). A shared row would make each service abort
  on the other's revision ids, because the two revision histories are unrelated.
- Use one in-memory DuckDB connection per interaction for bounded processing of validated,
  authorized data. DuckDB is not a durable store.
- Use LiteLLM Proxy as the only product model gateway. The application uses logical aliases through
  an OpenAI-compatible port and does not own provider credentials or fallback routing.
- Introduce Redis only for Story 8 caching. Redis is optional and never authoritative.
- Generate first-release exports with XlsxWriter from `ResultTable`. Store artifact metadata in
  PostgreSQL and access content through an artifact-store port. Use a filesystem fake for unit tests
  and a vendor-neutral S3-compatible adapter for deployed environments. SeaweedFS is the Apache-2.0
  reference implementation for integration tests and private deployment; application code must not
  use SeaweedFS-specific APIs, and production endpoint selection remains an approval gate.
- Emit structured JSON logs and OpenTelemetry-compatible telemetry after applying data
  classification and redaction.
- Use OCI images and Docker Compose for development and integration. Do not select a production
  orchestrator, managed database, object-storage product, or telemetry backend in this ADR.
- Write metering facts directly into the shared database inside factory-agent's business
  transaction (Story 11); usage-admin only reads the metering tables. There is no HTTP usage-event
  contract between the services.
- Treat report-agent as a read-only migration source. Port behavior through characterization tests;
  do not add it as a package, workspace member, submodule, or runtime dependency.
- Migrate pure state transitions, bounded context handling, typed model responses, error categories,
  and renderer separation. Rewrite repositories, API identity, SSE replay, authorization,
  observability, and artifact access for factory-agent boundaries.
- Reject Vanna, Text-to-SQL production execution, direct PostgreSQL/TDengine business queries,
  flight prompts/analyzers/charts, DOCX/PDF renderers, and request-body identity.

## Consequences

- Story 1 can create every target package, Protocol, fake, configuration entry, and composition root
  before customer fields or formulas are known.
- Stories 2 through 4 fill the stable security and infrastructure boundaries; Story 5 validates them
  with the first complete capability; later Stories add recipes rather than new execution paths.
- factory-agent and usage-admin share one PostgreSQL database with separate credentials and
  migration lifecycles; Mock MES keeps its own database. Every shared table has exactly one owner:
  usage-admin owns `tenant_registry`, `admin_audit`, `platform_principal`, and `usage_export`;
  factory-agent owns and writes the metering tables that usage-admin only reads. Schema changes to
  shared tables require synchronized review by both services (ADR-0003 §4.3).
- Separate Alembic version tables let both services run migrations against one database in any
  order (usage-admin pins `alembic_version_usage_admin`; factory-agent uses the default
  `alembic_version`); migrations stay independent and each service may only create, alter, or drop
  the tables it owns. Table prefixes make ownership visible in review.
- One owner per table keeps schema changes in a single service: usage-admin's migration history
  contains only `tenant_registry`, `admin_audit`, `platform_principal`, and `usage_export`; every
  other table's DDL lives in factory-agent's history.
- Source behavior from report-agent is preserved only when a factory-agent test states the intended
  behavior. No automatic synchronization with the source repository exists.
- New dependencies are added in the Story that first executes them, not preinstalled for empty
  skeleton packages.

## Approval Gates

Human approval remains required before selecting or changing customer authentication, role/scope
semantics, sensitive-field classification, production outbound hosts, credentials, model providers,
production object storage, production orchestration, retention enforcement, or deployment state.

## Revisit When

Revisit this baseline when customer API constraints make a selected Adapter boundary impossible,
measured workloads invalidate PostgreSQL/DuckDB/Redis assumptions, the frontend requires a different
streaming contract, or production platform requirements are delivered.
