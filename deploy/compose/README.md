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

PostgreSQL listens on `127.0.0.1:5432` and initializes separate `factory_agent`, `mock_mes`, and
`usage_admin` databases. Redis listens on `127.0.0.1:6379`. Override host ports with
`POSTGRES_PORT` and `REDIS_PORT`. The checked-in usernames and passwords are development-only.
Remove local data explicitly with:

```bash
docker compose -f deploy/compose/middleware.yaml down --volumes
```

`compose.yaml` is a local development topology. Mock MES must not be included in a production
deployment.

`usage-admin` intentionally uses its own PostgreSQL database instead of directly reading the
`factory-agent` database. The two services have different ownership and data scopes: `factory-agent`
stores tenant-scoped interaction metadata and outbox records for the factory assistant, while
`usage-admin` stores redacted platform usage events and rollups for operational reporting. Keeping
the databases separate avoids cross-service schema coupling, prevents platform analytics queries
from reading factory business state, and lets usage ingestion or reporting fail independently from
factory question answering.
