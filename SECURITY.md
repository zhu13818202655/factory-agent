# Security Baseline

## Data Classes

| Class | Examples | Default handling |
|---|---|---|
| Public | API version, health status | May be logged |
| Internal | API names, aggregate factory metrics | Authenticated access; sanitized logs |
| Confidential | Employee identity, piecework and payroll detail | Scoped access; excluded from prompts/logs |
| Secret | API keys, database passwords, tokens | Environment/secret store only; never Git |

## Mandatory Controls

- One deployment serves many factory tenants. **AppKey is the tenant ID (one factory, one AppKey).**
  A trusted `TenantContext` and `DataScope` are created from the customer credential bundle before
  any MES interaction; the credential bundle is never accepted from a request body, query string,
  natural language, or model output.
- Row-level filtering beyond the caller's own record is performed by the customer MES. `DataScope`
  records this as `mes_filtered` and never claims that wider range. A range narrowed by MES-side
  filtering is reported as a structured state, not as a complete result.
- Platform operations use a separately authenticated `PlatformScope` and never reuse factory
  identities or the MES business execution path for cross-tenant aggregation.
- User and model IDs can narrow but never broaden an effective scope.
- MES clients are read-only, use explicit outbound allowlists, bounded timeouts, and typed errors.
- Credential values (`app_key`, `accessToken`, `sign`, `movepassword`) are Secret class and stay
  inside `src/factory_agent/data_api/`.
- External responses are schema-validated before local processing.
- DuckDB is interaction-local with file, extension, external scan, DDL, and DML capabilities denied.
- Logs contain trace metadata, not prompts, raw records, authorization headers, or credentials.
- Test data is deterministic and synthetic. Coding agents receive no customer or production access.

## Secrets

`.env` is ignored. `.env.example` contains names and non-secret defaults only. Production secrets
come from the deployment secret store and are passed by reference. A credential accidentally
written to Git must be revoked; deleting the line is not sufficient remediation.

## Threats Covered by Tests

- Unauthorized tenant selection, cross-tenant identifiers in an MES interaction, and attempts
  to use factory identities for platform aggregation.
- Credential bundle values supplied through request bodies, query strings, natural language, or
  model output.
- Prompt injection attempting to alter API hosts, scope, or SQL policy.
- Sensitive canaries leaking into prompts, logs, traces, exports, or errors.
- Pagination truncation and malformed external payloads producing plausible wrong totals.
- DuckDB filesystem, extension, attachment, and mutation escape attempts.
- Provider timeout/fallback behavior without exposing provider credentials to product code.

Permission semantics, sensitive-field classification, retention, outbound hosts, destructive
migrations, production access, and secret rotation always require human approval.
