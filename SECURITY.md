# Security Baseline

## Data Classes

| Class | Examples | Default handling |
|---|---|---|
| Public | API version, health status | May be logged |
| Internal | API names, aggregate factory metrics | Authenticated access; sanitized logs |
| Confidential | Employee identity, piecework and payroll detail | Scoped access; excluded from prompts/logs |
| Secret | API keys, database passwords, tokens | Environment/secret store only; never Git |

## Mandatory Controls

- One deployment serves many company/factory tenants. Authentication resolves authorized tenant
  memberships before an active `TenantContext` and `DataScope` are created for an MES interaction.
- Platform operations use a separately authenticated `PlatformScope` and never reuse factory roles
  or the MES business execution path for cross-tenant aggregation.
- User and model IDs can narrow but never broaden an effective scope.
- MES clients are read-only, use explicit outbound allowlists, bounded timeouts, and typed errors.
- External responses are schema-validated before local processing.
- DuckDB is interaction-local with file, extension, external scan, DDL, and DML capabilities denied.
- Logs contain trace metadata, not prompts, raw records, authorization headers, or credentials.
- Test data is deterministic and synthetic. Coding agents receive no customer or production access.

## Secrets

`.env` is ignored. `.env.example` contains names and non-secret defaults only. Production secrets
come from the deployment secret store and are passed by reference. A credential accidentally
written to Git must be revoked; deleting the line is not sufficient remediation.

## Threats Covered by Tests

- Unauthorized tenant selection, cross-tenant identifiers in an active MES interaction, and attempts
  to use factory roles for platform aggregation.
- Prompt injection attempting to alter API hosts, scope, or SQL policy.
- Sensitive canaries leaking into prompts, logs, traces, exports, or errors.
- Pagination truncation and malformed external payloads producing plausible wrong totals.
- DuckDB filesystem, extension, attachment, and mutation escape attempts.
- Provider timeout/fallback behavior without exposing provider credentials to product code.

Permission semantics, sensitive-field classification, retention, outbound hosts, destructive
migrations, production access, and secret rotation always require human approval.
