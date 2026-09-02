# Local Compose

This directory has two development entry points:

- `compose.yaml` runs the complete local stack: PostgreSQL, Redis, `agent-api`, `mock-mes`, and
  `usage-admin`.
- `middleware.yaml` runs PostgreSQL 16 and Redis 7 for host-based debugging or for the application
  code you start outside Docker.

Use the helper scripts from this directory:

```bash
./start.sh all         # complete local stack
./start.sh middleware  # PostgreSQL and Redis only
./check.sh all
./stop.sh all
```

If you run `./start.sh` without an argument, it prompts you to choose `all` or `middleware` and
prints what each option means. The Makefile targets call the same scripts:

```bash
make compose-up
make middleware-up
```

PostgreSQL listens on `127.0.0.1:3432` and initializes one shared `factory_agent` database (used by
both `agent-api` and `usage-admin`) plus a separate `mock_mes` database. Redis listens on
`127.0.0.1:3379`. Override host ports with `POSTGRES_PORT` and `REDIS_PORT`. The checked-in
usernames and passwords are development-only.
Remove local data explicitly with:

```bash
docker compose -f deploy/compose/middleware.yaml down --volumes
```

`compose.yaml` is a local development topology. Mock MES must not be included in a production
deployment.

`factory-agent` and `usage-admin` share one logical PostgreSQL database (`factory_agent`, ADR-0003
§7). Each service connects with its own user (`factory_agent` / `usage_admin`) and runs its own
Alembic migrations against it, tracked in separate version tables (`alembic_version` /
`alembic_version_usage_admin`); table ownership is exclusive per service. `factory-agent` writes
its business tables (`agent_*`) and every metering table (`usage_event`, `*_fact`,
`mes_operation_category`, `tenant_usage_*`) directly into that database in a separate transaction
after the business commit — there is no outbox and no cross-service usage-event transport
(ADR-0003 §5). `usage-admin` never ingests usage events: it reads the metering tables read-only for
operational reporting, and owns only `tenant_registry`, `admin_audit`, `platform_principal`, and
`usage_export` (ADR-0003 §7). `init-databases.sql` grants `usage_admin` the right to create its own
tables in the shared database and sets default privileges so each service can read the other's
tables (factory-agent reads `tenant_registry`; usage-admin reads metering). Mock MES keeps its own
`mock_mes` database.
