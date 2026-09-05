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
./reset.sh middleware  # destroy the data volumes and start again empty
```

If you run `./start.sh` without an argument, it prompts you to choose `all` or `middleware` and
prints what each option means. The Makefile targets call the same scripts:

```bash
make compose-up
make middleware-up
make middleware-reset   # destructive: wipes every local volume, then restarts
```

PostgreSQL listens on `127.0.0.1:3432` and initializes one shared `factory_agent` database (used by
both `agent-api` and `usage-admin`) plus a separate `mock_mes` database. Redis listens on
`127.0.0.1:3379`. Override host ports with `POSTGRES_PORT` and `REDIS_PORT`. The checked-in
usernames and passwords are development-only.
## Wiping local data

`stop.sh` keeps the named volumes, so databases, migration state, and the Redis AOF survive a
restart. `reset.sh` is the destructive counterpart: it runs `down --volumes`, which deletes the
volumes so `postgres/init-databases.sql` runs again on the next start and you get a factory-fresh
database.

```bash
./reset.sh middleware            # list the volumes, ask for "yes", wipe, restart
./reset.sh middleware --no-start # wipe and leave everything stopped
./reset.sh all -y                # complete stack, no confirmation prompt

make middleware-reset            # same as ./reset.sh middleware
make middleware-reset CONFIRM=1  # same as ./reset.sh middleware --yes
make compose-reset               # same as ./reset.sh all
```

The target defaults to an interactive choice when you omit it, and the prompt is skipped only with
`-y`/`--yes` (or `CONFIRM=1` through the Makefile). Non-interactive runs without that flag fail
instead of deleting anything.

A wiped volume comes back with no schema at all, so re-apply the migrations afterwards:

```bash
make middleware-reset
make migrate       # factory-agent first, then usage-admin
make migrate-status
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
