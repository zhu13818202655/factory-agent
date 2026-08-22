# usage-admin

Independent production service for authorized, multi-tenant usage metering and operational reports.
It does not import `factory_agent` or `mock_mes`, and it never calls MES endpoints.

## Run locally

```bash
uv run --package usage-admin usage-admin
```

The liveness endpoint is `GET /health/live`. Readiness is `degraded` until
`USAGE_ADMIN_DATABASE_URL` is configured; database connectivity is introduced with ingest storage.

## Migrations

```bash
USAGE_ADMIN_DATABASE_URL=postgresql://... uv run --package usage-admin usage-admin-migrate upgrade head
```
