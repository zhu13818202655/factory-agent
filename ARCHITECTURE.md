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
    Orchestrator --> Gateway[Model Gateway]
    Gateway --> Models[Reviewed registry deployments]
```

## factory-agent 全流程处理逻辑图

```plantuml
@startuml
skinparam componentStyle rectangle
title factory-agent 问答全流程（Story 1 口径）

skinparam defaultFontName "Noto Sans CJK SC"

actor "前端同事\n(PC 悬浮助手 / App)" as FE

rectangle "API 层  /v1" {
  (identity: resolve_credential) as EDGE
  (sessions/interactions SSE) as API
  (quick-questions / history / favorites / export) as PERS_API
}

rectangle "认证与授权（任何业务调用前完成）" {
  database "Mock MES\n/api/system/token" as TOKEN
  component "TokenCredentialExchange\n(换取/60s·2h 刷新/重试一次)" as EX
  component "TokenBackedMembershipResolver\n(role/dept/boundDepts 权威字段)" as MEM
  component "authorize_capability\n能力-角色矩阵 FR-001~012" as AUTH
  component "DataScope\n(仅来自 token，不可由用户/LLM 扩大)" as SCOPE
}

rectangle "意图与口径约束" {
  component "CapabilityIntentParser\n(LLM 别名→意图 + 时间/名称槽)" as INTENT
  component "口径门：近一年上限 · Uid 空值规则\n(超限友好终止，零 MES 调用)" as GATES
  component "quick_questions 角色化\n+ 友好拒绝(告知可查范围)" as FRIENDLY
}

rectangle "执行（L1 确定性 DAG，单一 bounded 执行器）" {
  component "recipes fr001~fr012\n(api 步 + 本地 compute)" as RECIPE
  component "ScopedExecutor→HongzhaoMesAdapter\n仅 data_api 调 MES；Bearer+app_key/timestamp/sign" as EXEC
  component "CachedDirectorySource\n基础数据共享缓存(键不含 scope) + 业务范围缓存" as CACHE
  database "Mock MES 业务接口\n(99全厂/02绑定车间/01本组/00本人)" as MES
  component "DuckDB 沙箱(只读)" as SANDBOX
}

rectangle "结果与产物" {
  component "ResultTable + 指标注册表\n(unavailable 不伪造；交期预警标记字段)" as RT
  component "卡片/SSE result 事件 / XLSX 导出\n(presigned 即时下载，不留存)" as OUT
  component "usage metering → 共享 PostgreSQL\n(业务提交后独立事务；仅 factory-agent 写计量表)" as USAGE
}

rectangle "数据 API 单边界" {
  database "customer MES / mock-mes" as MESEND
}

FE --> EDGE
EDGE --> EX
EX --> TOKEN
EX --> MEM
MEM --> AUTH
AUTH --> SCOPE
SCOPE --> INTENT
INTENT --> GATES
GATES --> FRIENDLY
GATES --> RECIPE : time_range/filters
RECIPE --> EXEC
EXEC --> CACHE
CACHE --> MESEND : 基础数据全量
EXEC --> MESEND : 业务数据(按角色行级过滤)
MESEND --> EXEC
EXEC --> SANDBOX : 本地 compute(JOIN/GROUP BY/RANK)
SANDBOX --> RT
RT --> OUT
OUT --> FE
RT --> USAGE
@enduml
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

The baseline below follows the accepted ADRs under `docs/adr/` (`0002` for runtime, storage, and
migrations; `0004` for logging and configuration; `0006` for model access). Customer-specific
authentication, API fields, deployment platform, object-storage endpoint, model provider, and
business formulas remain replaceable configuration or Adapter concerns.

| Concern | Choice | Boundary |
| :--- | :--- | :--- |
| Runtime and API | Python 3.12, FastAPI, Uvicorn, Pydantic v2 | HTTP and framework types stop at `api/` |
| Package and quality | uv workspace, Hatchling, Ruff, Pyright strict, pytest, Bandit, pip-audit | One lockfile; root, Mock, and usage-admin remain separate packages |
| MES integration | HTTPX async client, OpenAPI 3.1, JSON Schema | Only `data_api/` knows URLs, auth transport, or customer payloads |
| Durable application data | PostgreSQL 16, Psycopg 3, Alembic | Sessions, messages, interactions, audit metadata, favorites, and artifact metadata only |
| Mock MES data | Separate PostgreSQL database, Psycopg 3, Alembic, deterministic seed | Never a production dependency and never shares application tables |
| Interaction processing | One in-memory DuckDB connection per interaction | Validated authorized rows only; no persistence or external/file access |
| Model access | LiteLLM Router SDK with a reviewed deployment registry (ADR-0006) | Business code only names logical aliases; keys live in the environment; fallback/retry/cooldown are owned by the Router |
| Cache | TTL-based application cache; Redis optional and added only if measurements justify it | Optional optimization; PostgreSQL/MES remain authoritative |
| Export | XlsxWriter from `ResultTable`; delivery follows the customer-confirmed no-server-retention policy | XLSX only in the first release; external text is always written as text |
| Artifact storage | Filesystem fake for unit tests; S3-compatible port with SeaweedFS as the Apache-2.0 reference implementation | PostgreSQL stores metadata, never file contents; application code uses no vendor API, and the production endpoint waits for deployment review |
| Observability | Structured JSON logs and OpenTelemetry-compatible traces/metrics | Sensitive fields and raw prompts are filtered before emission |
| Delivery | OCI images and Docker Compose for development/integration | Production orchestrator and managed services remain deployment decisions |

Dependencies are added when the code that needs them is implemented. PostgreSQL is a separate
runtime service; Redis and object storage are added only when measurements justify them. The model
gateway embeds the LiteLLM Router SDK behind a port (ADR-0006) and never leaks into domain or
application code.

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

The boundaries, configuration objects, test doubles, and application wiring are implemented, as are
the security and observability infrastructure and the first complete business path through every
layer. Later capabilities register as recipes on the single reviewed execution path rather than as
new parallel architectures. Remaining alignment and release work is tracked in the numbered Stories
under `.github/story/`.

## Repository Shape

The root builds `src/factory_agent`. `mock-mes/` is a separately runnable uv workspace member
used only for development and tests. `usage-admin/` is a separately built production uv workspace
member for authorized multi-tenant usage aggregation, operational APIs, and reports. Each service
owns its package, tests, Dockerfile, migrations, and configuration. No service imports another;
metering facts are written by factory-agent directly into the shared PostgreSQL in a separate
transaction after its business commit, and usage-admin reads them — there
is no HTTP usage-event contract between the services, and either could be split into separate
repositories later. Every shared table has
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

`/home/admin2/proj/report-agent` was the read-only migration source for proven behavior; the
migration is complete and the repository never depends on it at runtime. The boundary rewrites it
forced are now standing invariants:

- identity comes from trusted credential resolution, never from request-body `user_id`/`tenant_id`;
  there is no allow-all production default (scope is `DataScope`/`PlatformScope`);
- MES data is reachable only through `MesDataSource` and reviewed local recipes — Vanna,
  Text-to-SQL production execution, and direct business-database queries are rejected;
- XLSX is the only initial artifact renderer (DOCX/PDF renderers and flight templates do not
  migrate);
- durable event replay and trusted identity are native implementations, not claimed reuse (the
  source used process-local SSE subscribers and request-body identity).

Pure state-transition, bounded-context, typed-model-response, error-category, and renderer-
separation behavior was ported through characterization tests.
