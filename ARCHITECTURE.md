# Architecture

## System Boundary

`factory-agent` is a multi-tenant, read-only API orchestration service. One deployment serves users
from many companies and factories. For each MES business interaction it authorizes the user, resolves
one active `TenantContext`, obtains an effective tenant-local `DataScope`, calls MES APIs through a
typed adapter, validates responses, performs bounded local processing, and composes grounded results.
Platform usage operations use a separate `PlatformScope` and do not enter the MES execution path.

```mermaid
flowchart LR
    Client --> Agent[factory-agent API]
    Agent --> Auth[Identity and tenant memberships]
    Auth --> Context[Active TenantContext and DataScope]
    Context --> Orchestrator[DAG Orchestrator]
    Orchestrator --> Adapter[MesDataSource Adapter]
    Adapter --> MES[Customer MES or mock-mes]
    Orchestrator --> Sandbox[Interaction DuckDB]
    Orchestrator --> Gateway[LiteLLM Gateway]
    Gateway --> Models[vLLM then remote fallbacks]
```

## Dependency Direction

```text
api -> application -> domain
          |             ^
          v             |
   ports/protocols -----+
          ^
          |
data_api / repository / llm / observability
```

The domain owns vocabulary and invariants. Infrastructure implements protocols. Framework,
database, HTTP, and provider types do not cross into domain code.

## Technology Baseline

The baseline below is the implementation choice proposed in
`docs/adr/0002-runtime-storage-and-migration-baseline.md`. Customer-specific authentication,
API fields, deployment platform, object-storage endpoint, model provider, and business formulas remain
replaceable configuration or Adapter concerns.

| Concern | Choice | Boundary |
| :--- | :--- | :--- |
| Runtime and API | Python 3.12, FastAPI, Uvicorn, Pydantic v2 | HTTP and framework types stop at `api/` |
| Package and quality | uv workspace, Hatchling, Ruff, Pyright strict, pytest, Bandit, pip-audit | One lockfile; root, Mock, and usage-admin remain separate packages |
| MES integration | HTTPX async client, OpenAPI 3.1, JSON Schema | Only `data_api/` knows URLs, auth transport, or customer payloads |
| Durable application data | PostgreSQL 16, Psycopg 3, Alembic | Sessions, messages, interactions, audit metadata, favorites, and artifact metadata only |
| Mock MES data | Separate PostgreSQL database, Psycopg 3, Alembic, deterministic seed | Never a production dependency and never shares application tables |
| Interaction processing | One in-memory DuckDB connection per interaction | Validated authorized rows only; no persistence or external/file access |
| Model access | LiteLLM Proxy behind an OpenAI-compatible application port | Providers, keys, retries, and fallback stay outside the application |
| Cache | Redis, introduced only for Story 8 | Optional optimization; PostgreSQL/MES remain authoritative |
| Export | XlsxWriter from `ResultTable` | XLSX only in the first release; external text is always written as text |
| Artifact storage | Filesystem fake for unit tests; S3-compatible port with SeaweedFS as the Apache-2.0 reference implementation | PostgreSQL stores metadata, never file contents; application code uses no vendor API, and the production endpoint waits for deployment review |
| Observability | Structured JSON logs and OpenTelemetry-compatible traces/metrics | Sensitive fields and raw prompts are filtered before emission |
| Delivery | OCI images and Docker Compose for development/integration | Production orchestrator and managed services remain deployment decisions |

Dependencies are added only when their owning Story starts. PostgreSQL, Redis, LiteLLM, and object
storage are separate runtime services; they are not embedded into domain or application code.

## Target Package Skeleton

```text
src/factory_agent/
    api/              # HTTP/SSE request and response boundary
    application/      # use cases, conversation and capability orchestration
    domain/           # immutable identity, scope, result and metric values
    ports/            # identity, MES, model, repository and artifact protocols
    data_api/         # customer MES HTTP adapter, credentials and envelope handling
    execution/        # reviewed DAG, pagination and interaction DuckDB
    persistence/      # PostgreSQL repositories and Alembic integration
    llm/              # LiteLLM adapter and structured output validation
    export/           # ResultTable card and XLSX renderers
    observability/    # redacted audit, logs, traces and metrics
```

Story 1 creates these boundaries, configuration objects, test doubles, and application wiring even
where the first implementation is a typed `NotConfigured` adapter. Stories 2 through 4 fill the
security and infrastructure behavior. Story 5 is the first complete business path through every
layer; Stories 6 through 8 add recipes rather than new parallel architectures.

## Repository Shape

The root builds `src/factory_agent`. `mock-mes/` is a separately runnable uv workspace member
used only for development and tests. `usage-admin/` is a separately built production uv workspace
member for authorized multi-tenant usage aggregation, operational APIs, and reports. Each service
owns its package, tests, Dockerfile, migrations, and configuration. No service imports another;
metering facts are written by factory-agent directly into the shared PostgreSQL inside its business
transaction and usage-admin reads them (Story 11) — there is no HTTP usage-event contract between
the services, and either could be split into separate repositories later. Every shared table has
exactly one owner: usage-admin owns and writes `tenant_registry` (which factory-agent reads
read-only to resolve the AppKey for MES calls, ADR-0003 §4.3), `admin_audit`,
`platform_principal`, and `usage_export`; factory-agent owns and writes all business and metering
tables that usage-admin only reads.

## Request Invariants

1. Resolve identity and authorized tenant memberships.
2. Resolve one trusted active `TenantContext` and tenant-local `DataScope` for an MES interaction;
    platform aggregation instead resolves a separately authorized `PlatformScope`.
3. Classify intent and collect missing slots.
4. Select a reviewed L1 capability or a bounded L2 plan.
5. Execute only scope-intersected API calls within budgets.
6. Validate responses and load isolated in-memory tables.
7. Compute a `ResultTable`, then compose a grounded response and audit record.
8. Destroy interaction-local detail data.

Irreversible choices and boundary changes are recorded under `docs/adr/`.

## report-agent Migration Boundary

`/home/admin2/proj/report-agent` is a source of proven behavior, not a runtime dependency. Migration
uses characterization tests and rewrites factory-specific boundaries rather than copying the package.

| Source | Decision | Factory target |
| :--- | :--- | :--- |
| `state_machine.py` | Migrate pure transition behavior, rename states | `domain/` and application tests |
| interaction/message models in `schemas.py` | Adapt IDs, sequence and lifecycle; remove flight fields | domain values and API views |
| `repository.py` and Alembic revisions | Reuse repository shape and migration lessons; rewrite every query with tenant/user ownership | `ports/` and `persistence/` |
| `context.py` and follow-up patch prompts | Reuse bounded-history and patch behavior; exclude prior detail and scope | `application/` |
| `api/router.py` | Reuse endpoint/SSE shape only | `api/`; identity comes from trusted middleware, never request `user_id/tenant_id` |
| `llm/` | Reuse typed request/response and error tests | `llm/`; base URL and provider fallback point only to LiteLLM |
| `export/` | Reuse renderer/router separation | `export/`; implement card/XLSX from `ResultTable` |
| `permissions.py` | Interface inspiration only | Replaced by Story 2 `DataScope`; no allow-all production default |
| `dikong_sql/`, `text2sql/`, Vanna, flight prompts/analyzers/charts | Do not migrate | MES data is available only through `MesDataSource` and reviewed local recipes |
| DOCX/PDF renderers and flight templates | Do not migrate in the first release | XLSX is the only initial artifact |

The source implementation has process-local SSE subscribers and accepts identity in request payloads;
therefore durable event replay and trusted identity are new implementations, not claimed reuse.
