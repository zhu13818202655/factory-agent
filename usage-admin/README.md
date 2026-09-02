# usage-admin

Independent production service for authorized, multi-tenant usage metering, operational reports,
and **tenant master data**. It does not import `factory_agent` or `mock_mes`, and it
never calls MES endpoints.

## Responsibilities

- **Usage queries & reports**: summary, timeseries, dimensions, users, MES category statistics
  (`/usage/mes-categories`, `/usage/mes-failures`, `/usage/by-tenant`, `/usage/mes-operations`),
  models / capabilities / errors, and CSV/XLSX exports.
- **Tenant master data**: owns and writes `tenant_registry` (factory name + AppKey + status),
  `platform_principal` (internal operations accounts), and `admin_audit`. factory-agent reads
  `tenant_registry` read-only; every other table in the shared database is factory-agent owned and
  **read-only here** (table ownership, ADR-0003 §7).

## Authentication

Two channels, token first (D14~D16):

- `Authorization: Bearer <token>` — signed tokens from `POST /admin/v1/auth/login`, or the
  front-end token configured via `USAGE_ADMIN_API_TOKEN` (maps to the `admin` role).
- Trusted-gateway three headers (`X-Platform-Principal` / `X-Platform-Role` / `X-Platform-Tenants`)
  as the dev/test direct channel.

Roles: `viewer` (aggregates only) / `analyst` (+ user-level detail, export) / `admin`
(+ tenant & account management). Factory-account and account-registration writes are admin-only
and fully audited in `admin_audit`.

## Run locally

```bash
uv run --package usage-admin usage-admin
```

The liveness endpoint is `GET /health/live`. Readiness is `degraded` until
`USAGE_ADMIN_DATABASE_URL` is configured — point it at the database shared with factory-agent
(`factory_agent` in the local topology, ADR-0003 §7). Required secrets for production: set
`USAGE_ADMIN_TOKEN_SIGNING_SECRET` and `USAGE_ADMIN_API_TOKEN` (front-end). See
[`docs/API.md`](docs/API.md) for the full front-end contract.

## Migrations

This service's Alembic history contains **only** `tenant_registry`, `admin_audit`,
`platform_principal`, and `usage_export`; every metering table's DDL lives in the factory-agent
history. It uses its own version table `alembic_version_usage_admin` so both services can migrate
one shared database in any order.

```bash
USAGE_ADMIN_DATABASE_URL=postgresql://... uv run --package usage-admin usage-admin-migrate upgrade head
```

## Tests

```bash
uv run --package usage-admin pytest usage-admin/tests/unit
```

Integration tests additionally require `USAGE_ADMIN_TEST_DATABASE_URL` pointing at a disposable
database with the migrations applied.
