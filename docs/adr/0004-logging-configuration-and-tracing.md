# ADR-0004: Logging, Runtime Configuration, And Tracing

- Status: Proposed
- Date: 2026-08-22
- Owners: Project maintainers

## Context

`factory-agent` serves multiple tenants and handles MES data that can include employee identity,
piecework, payroll, and production details. Observability must help operators diagnose failures
without leaking business data, prompts, credentials, authorization scopes, or customer API payloads.

The repository currently has only the baseline rule: emit structured logs and
OpenTelemetry-compatible telemetry after redaction. This ADR defines the intended implementation
shape before adding production observability code.

## Decision

- Use Loguru for application logging, with a small repository-owned observability adapter under
  `src/factory_agent/observability/`.
- Keep Python standard `logging` compatibility by intercepting standard library, Uvicorn, FastAPI,
  HTTPX, SQLAlchemy, and Alembic logs into Loguru sinks.
- Emit structured JSON logs by default in containers. Human-readable colored logs are allowed only
  in local development.
- Use Pydantic Settings for all service configuration. Secrets use `SecretStr` or DSN secret types
  and are never included in readiness payloads, logs, traces, errors, or test snapshots.
- Do not add the OpenTelemetry SDK, Collector, or trace backend in the current Stories. Use
  structured logs, correlation IDs, and explicit duration/status fields for current diagnostics.
- Keep the observability adapter OpenTelemetry-compatible so production tracing can be added later
  without changing application and domain interfaces.
- LLM provider keys, provider fallback, provider retry policy, and provider routing stay in LiteLLM
  Proxy. The application configures only the LiteLLM Proxy endpoint, proxy credential, logical model
  aliases, request defaults, and application-level semantic repair limits.

## Logging Design

### Logger Shape

Application code should not call Loguru directly from every module. It should use a small adapter
that centralizes binding and redaction:

```python
logger = get_logger(__name__).bind(component="data_api")
logger.info("mes_call_completed", operation_id=operation_id, status="ok")
```

The adapter owns:

- sink setup at process startup;
- JSON vs console formatting;
- request and interaction context binding;
- redaction of known sensitive keys;
- conversion of exceptions into sanitized operational fields;
- forwarding standard `logging` records into Loguru.

### Required Fields

Every application log event should include these fields when available:

| Field | Description |
| :--- | :--- |
| `timestamp` | UTC ISO timestamp from the logger sink |
| `level` | Log level |
| `service` | `factory-agent`, `mock-mes`, or `usage-admin` |
| `environment` | `development`, `test`, or `production` |
| `component` | Logical package or adapter name |
| `event` | Stable event name, not free-form prose |
| `request_id` | Correlation ID for one inbound request |
| `interaction_id` | Current interaction identifier when safe and available |
| `tenant_id` | Active tenant ID when already authorized and safe to expose internally |
| `operation_id` | Canonical MES operation or internal operation name |
| `status` | `ok`, `degraded`, `denied`, `failed`, or equivalent bounded status |
| `duration_ms` | Duration for completed operations |
| `error_type` | Sanitized error category, not raw exception text when it may contain data |

### Forbidden Log Content

Logs must never contain:

- user questions, prompts, model raw responses, or final answers;
- MES request URLs with query parameters, request bodies, response bodies, or raw customer payloads;
- authorization headers, cookies, API keys, bearer tokens, DSNs with passwords, or signed URLs;
- employee names, employee numbers, payroll values, piecework quantities, order values, or row-level
  production records;
- `DataScope` employee or department ID lists;
- exported artifact contents or filenames derived from business text.

### Log Levels

| Level | Intended Use |
| :--- | :--- |
| `DEBUG` | Local-only diagnostics after redaction; disabled by default in production |
| `INFO` | Startup, shutdown, readiness summary, completed operations, accepted requests |
| `WARNING` | Degraded dependencies, retries, rate limits, recoverable validation repair |
| `ERROR` | Failed operations that need operator attention |
| `CRITICAL` | Process-level failure or data-safety stop condition |

### Loguru Configuration Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `FACTORY_AGENT_LOG_LEVEL` | `INFO` | Minimum application log level |
| `FACTORY_AGENT_LOG_FORMAT` | `json` in containers, `console` locally | Log sink format |
| `FACTORY_AGENT_LOG_SERIALIZE` | `true` | Emit JSON serialized records |
| `FACTORY_AGENT_LOG_INCLUDE_BACKTRACE` | `false` | Keep Loguru backtrace/diagnose disabled outside local debugging |
| `FACTORY_AGENT_LOG_SAMPLE_RATE` | `1.0` | Optional sampling for high-volume INFO logs |

`mock-mes` and `usage-admin` should mirror these with their own prefixes:
`MOCK_MES_LOG_*` and `USAGE_ADMIN_LOG_*`.

## Runtime Configuration Design

### Settings Pattern

Each service keeps a typed settings object:

- `FactoryAgentSettings` under `src/factory_agent/config.py`;
- `MockMesSettings` under `mock-mes/src/mock_mes/config.py`;
- `UsageAdminSettings` under `usage-admin/src/usage_admin/config.py`.

All settings use `BaseSettings` and an explicit env prefix. Values are read once through an
`lru_cache` getter and injected into the composition root. Tests should pass settings or dependency
overrides explicitly rather than mutating global state.

### Common Service Variables

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `<PREFIX>ENVIRONMENT` or `<PREFIX>ENV` | enum/string | `development` | Runtime environment |
| `<PREFIX>HOST` | string | `127.0.0.1` locally | Bind host |
| `<PREFIX>PORT` | int | service default | Bind port |
| `<PREFIX>POSTGRES_URL` / `<PREFIX>DATABASE_URL` | secret DSN | unset | Service-owned database |
| `<PREFIX>LOG_LEVEL` | string | `INFO` | Logging level |
| `<PREFIX>LOG_FORMAT` | enum | `json` | `json` or `console` |
| `<PREFIX>REQUEST_ID_HEADER` | string | `X-Request-ID` | Trusted correlation header name |

### Factory Agent Variables

| Variable | Type | Description |
| :--- | :--- | :--- |
| `FACTORY_AGENT_CANONICAL_MES_BASE_URL` | URL | Canonical MES or Mock MES base URL |
| `FACTORY_AGENT_POSTGRES_URL` | Postgres DSN | Main application metadata and outbox database |
| `FACTORY_AGENT_REDIS_URL` | Redis DSN | Optional cache endpoint; non-authoritative |
| `FACTORY_AGENT_ARTIFACT_ENDPOINT` | URL | S3-compatible artifact endpoint |
| `FACTORY_AGENT_ARTIFACT_BUCKET` | string | Artifact bucket/container name |
| `FACTORY_AGENT_MODEL_REGISTRY_PATH` | path | Reviewed model registry; see ADR-0006 |
| `FACTORY_AGENT_LLM_KEY_*` | secret | Provider keys named by the registry (ADR-0006) |
| `FACTORY_AGENT_LLM_DEFAULT_MODEL` | string | Logical model alias, not provider model name |
| `FACTORY_AGENT_LLM_FAST_MODEL` | string | Optional alias for routing/simple classification |
| `FACTORY_AGENT_LLM_REASONING_MODEL` | string | Optional alias for heavier reasoning |
| `FACTORY_AGENT_LLM_TEMPERATURE` | Decimal/float | Default bounded sampling temperature |
| `FACTORY_AGENT_LLM_TOP_P` | Decimal/float | Default bounded nucleus sampling value |
| `FACTORY_AGENT_LLM_TIMEOUT_SECONDS` | float | Per logical LLM request timeout |
| `FACTORY_AGENT_LLM_MAX_REPAIR_ATTEMPTS` | int | Application semantic/schema repair attempts, default at most `1` |

### LLM Config Boundary

> **Superseded by ADR-0006.** This project does not deploy a LiteLLM Proxy, so
> the application owns provider configuration through the reviewed registry and
> delegates fallback, retry, and cooldown to the LiteLLM Router SDK. The
> sensitive-data rules below remain fully in force.

The application sends requests to one LiteLLM Proxy through the `ModelGateway` port. The app may set:

- logical model alias;
- temperature and `top_p` defaults;
- timeout for the logical application call;
- maximum schema/semantic repair attempts;
- safe response schema and bounded output size.

The app must not own:

- provider API keys;
- direct provider base URLs;
- provider fallback chains;
- provider retry/backoff policy;
- provider-specific routing rules.

Those belong in LiteLLM Proxy configuration. The application may record sanitized fallback facts
returned by LiteLLM for usage metering, such as `fallback=true`, logical alias, final model name if
allowed, attempt count, status, token counts, and duration. It must not log prompts, completions,
request bodies, or provider credentials.

## Tracing Design

### Current Decision: Correlation Without OpenTelemetry

The current deployment does not justify the operational cost of an OpenTelemetry Collector and
trace backend. Do not install the OTel SDK or deploy tracing infrastructure yet. Correlate one
interaction with structured Loguru events carrying:

- `request_id` for the inbound HTTP request;
- `interaction_id` for the durable question-answer interaction;
- stable `operation_id` values for MES, LLM, database, sandbox, and artifact operations;
- `duration_ms`, bounded `status`, and sanitized `error_type` on completed operations.

Use `contextvars` in the observability adapter so these values are bound once and included in logs
without passing logging context through every function. Propagate `X-Request-ID` to approved HTTP
dependencies when useful. Accept an inbound request ID only after validating its format and length;
otherwise generate a new opaque ID.

### OpenTelemetry Trigger Conditions

Reconsider OpenTelemetry in Story 12 only when at least one of these conditions exists:

- requests cross enough independently operated services that correlation logs are insufficient;
- production incidents require a call tree rather than per-stage duration logs;
- the organization already operates an approved OTel Collector and trace backend;
- measured latency requires span-level analysis across MES, LiteLLM, PostgreSQL, Redis, or artifact
  storage;
- SLO, sampling, retention, access control, and cost requirements have been approved.

If none applies, production continues with structured logs and correlation IDs. OTel must not be
introduced only because the adapter supports it.

### Future Trace Attributes

If Story 12 approves OTel, allowed attributes are:

| Attribute | Description |
| :--- | :--- |
| `service.name` | Service name |
| `deployment.environment` | Environment |
| `factory_agent.interaction_id` | Safe interaction identifier |
| `factory_agent.tenant_id` | Active authorized tenant ID |
| `factory_agent.capability` | Capability ID |
| `factory_agent.mes.operation_id` | Canonical operation ID |
| `factory_agent.llm.model_alias` | Logical model alias |
| `factory_agent.error_type` | Sanitized error category |
| `http.method`, `http.route`, `http.status_code` | Standard HTTP metadata |

Forbidden attributes mirror forbidden log content: no prompts, answers, raw MES data, user text,
scope ID lists, credentials, DSNs with passwords, or row-level business values.

### Future Trace And Log Correlation

If OTel is enabled later, the Loguru adapter may additionally enrich records with OTel `trace_id`
and `span_id`. `request_id` and `interaction_id` remain stable correlation fields and do not depend
on OTel.

1. Find the sanitized error log.
2. Copy `trace_id`.
3. Open the trace in the configured backend.
4. Inspect spans and sanitized event metadata without exposing business data.

## Implementation Plan

1. **Story 2:** add Loguru, typed common/log settings, centralized sinks and standard logging
  interception.
2. **Story 2:** add request middleware and `contextvars` binding for validated `request_id`, trusted
  tenant context after authorization, and `interaction_id` when available.
3. **Story 2:** prove sensitive canary values do not appear in logs, errors, or snapshots.
4. **Story 4:** add typed LLM settings, LiteLLM adapter configuration, logical aliases, sampling
  defaults, timeout, and at most one semantic repair.
5. **Story 4:** emit redacted LLM/MES operation facts and usage events with correlation IDs.
6. **Story 12:** evaluate the OTel trigger conditions. Add SDK, Collector/exporter configuration,
  sampling, redaction tests, and runbooks only if tracing is approved and operationally justified.

## Consequences

- Loguru gives simple structured logging and good local developer ergonomics while the adapter keeps
  logging policy centralized.
- Correlation IDs and structured duration logs cover current diagnostics without requiring a
  Collector, trace backend, sampling policy, or trace storage operations.
- The observability adapter preserves a future vendor-neutral OpenTelemetry integration point.
- LiteLLM remains the provider fallback and credential boundary; application config stays focused on
  logical model behavior and safety limits.
- Observability is useful by default but cannot become a side channel for sensitive factory data.
