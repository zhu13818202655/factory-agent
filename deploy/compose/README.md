# Local Compose

This directory has two development entry points:

- `compose.yaml` runs the complete local stack: PostgreSQL, Redis, SeaweedFS (the
  S3-compatible object store that holds retained exports), and `agent-api`. The platform
  statistics surface (`/v1/statistics`) runs inside `agent-api`, so there is no second
  application service.
- `middleware.yaml` runs PostgreSQL 16, Redis 7, and SeaweedFS for host-based debugging or for the
  application code you start outside Docker.
- `assistant-web` builds and serves the two frontend submodules (`frontend` Web assistant and
  `frontend-h5` H5 assistant) as one Caddy site, and comes in two opt-in compose profiles that
  share the same image (`deploy/docker/web.Dockerfile`, repository root as build context because
  the Dockerfile copies both submodule trees):
  - `--profile web-domain` (`assistant-web-domain`): self-managed domain `hz.jundadt.com.cn`.
    Caddy issues certificates automatically via alidns DNS-01; the Alibaba Cloud RAM credentials
    (`ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`, DNS-edit permission only) come from
    `.env`, and ports 80/443 are published (`HTTP_PORT` / `HTTPS_PORT`).
  - `--profile web-srv-network` (`assistant-web-srv`): the container joins the external
    `docker network create srv-domain` network and serves plain HTTP on port 80 to whatever
    reverse proxy already lives there (caddy, nginx, or traefik — this project does not care
    which). No host ports are published; the `default` network is kept so it can still reach
    `agent-api`.
  Without a profile, the web services stay stopped and the local dev stack is unaffected.

  The two Caddy configs differ only in the site address/TLS block; routing is identical:
  `/` serves the Web assistant, `/h5/` serves the H5 assistant, and `/v1/*` is reverse
  proxied verbatim to the backend — both frontends now share one convention: production
  `VITE_API_BASE_URL` is empty, so all API calls are same-origin `/v1/...` (never strip
  that prefix, or the backend 404s). SPA fallbacks rewrite to `index.html`. `BACKEND_URL`
  selects the backend (defaults to `http://agent-api:8000` inside compose; set an external
  address in `.env` otherwise). The runtime profile is chosen by the `WEB_PROFILE` env var
  (`domain` | `srv-network`), which selects between the two baked-in Caddyfiles.

  The real `.env.*` files are git-ignored in both frontend submodules, so the Dockerfile copies
  `.env.example` to `.env.production` during the build (frontend's tier defaults are also
  built into `vite.config.ts`, so the copy is belt-and-suspenders; for `frontend-h5` the copy
  additionally blanks `VITE_API_BASE_URL` so production calls same-origin `/v1/`).

  Build acceleration uses China mirrors throughout: `REGISTRY` (default `gh-proxy.org/docker`)
  and `NPM_REGISTRY` (default `https://registry.npmmirror.com`) build args, plus
  `GOPROXY=https://goproxy.cn,direct` for the Caddy `alidns` plugin build.

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

PostgreSQL listens on `127.0.0.1:3432` and initializes the `factory_agent` database (one database,
one owner user). Redis listens on
`127.0.0.1:3379`. SeaweedFS exposes its S3 API on `127.0.0.1:8333` (loopback only; containers reach
it as `seaweedfs:8333`). Override host ports with `POSTGRES_PORT`, `REDIS_PORT`, and
`SEAWEEDFS_S3_PORT`. The checked-in usernames and passwords are development-only.

Exports default to this object store: `FACTORY_AGENT_S3_ENDPOINT_URL` points `agent-api` at the
`seaweedfs` service and the bucket name comes from `FACTORY_AGENT_S3_BUCKET`. Leaving that endpoint
empty falls back to the local directory backend, which is what unit tests and host-only runs use.
The S3 credentials in `.env.example` seed the gateway on first start; both must be set, because
omitting them would leave SeaweedFS in anonymous "Allow All" mode.

Platform report exports use the same gateway but their own bucket
(`FACTORY_AGENT_STATISTICS_S3_BUCKET`, default `factory-agent-statistics-exports`, ADR-0009 §2.6).
The two buckets are seeded together from the comma-separated `S3_BUCKET` value on the `seaweedfs`
service. Leaving `FACTORY_AGENT_STATISTICS_S3_ENDPOINT_URL` empty falls back to
`FACTORY_AGENT_STATISTICS_EXPORT_STORE_DIR`, and if both are empty exports live in memory only —
they are then lost on restart, so that combination is for dev/test runs only. Downloads on both
surfaces stay backend-proxied: the service re-checks ownership, writes an audit event, and refuses
to release bytes when that audit fails.
## Wiping local data

`stop.sh` keeps the named volumes, so databases, migration state, the Redis AOF, and retained export
objects survive a restart. `reset.sh` is the destructive counterpart: it runs `down --volumes`,
which deletes the volumes so `postgres/init-databases.sql` runs again on the next start and you get
a factory-fresh database. Wiping also discards every export object in SeaweedFS (both the business
artifact and the statistics report bucket), so download ids handed out before the reset become
404 — artifact exports are regenerated through history/favorite re-ask.

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
make migrate
make migrate-status
```

`start.sh all` and the compose file also apply migrations automatically: `agent-api` runs
`python -m factory_agent.persistence.migrations upgrade head` before launching uvicorn
(`migrate && exec uvicorn ...`). The step is idempotent (tracked by the single `alembic_version`
table), and a migration failure aborts startup instead of serving against a wrong schema. Manual
`make migrate` stays available for host-side runs.

The LLM gateway is resolved from `configs/knowledge/models.yaml` through
environment variables, so compose forwards `FACTORY_AGENT_LLM_API_KEY`,
`FACTORY_AGENT_LLM_API_BASE`, `FACTORY_AGENT_LLM_MODEL`, and
`FACTORY_AGENT_LLM_KEY_DEEPSEEK` into `agent-api`. Set them (at minimum the
API key) in `deploy/compose/.env`; with an empty key every model deployment is
dropped at startup and chat interactions fail fast with
`gateway_not_configured`.

`agent-api` loads the whole of `deploy/compose/.env` through `env_file`, so any setting that is
not listed under `environment:` — MES timeout, MES page sizes, fan-out concurrency, session
budgets, time-parse and scope-guard modes — is tuned by editing that one file and restarting the
service. Entries under `environment:` win over the file, which is what keeps
`FACTORY_AGENT_ENVIRONMENT`, `FACTORY_AGENT_HOST` and `FACTORY_AGENT_PORT` container-only.

`compose.yaml` is a local development topology; production deployments use the
customer MES gateway instead of any local simulator.

There is one logical PostgreSQL database (`factory_agent`, ADR-0003 §7), one owner user
(`factory_agent`), and one Alembic history recorded in the single `alembic_version` table.
`init-databases.sql` only creates the database and its owner; no second role and no cross-role
grants are needed.

The application writes its business tables (`agent_*`) and every metering table (`usage_event`,
`*_fact`, `mes_operation_category`, `tenant_usage_*`) directly into that database in a separate
transaction after the business commit — there is no outbox and no cross-service usage-event
transport (ADR-0003 §5). The statistics surface never ingests usage events: it reads the metering
tables read-only for operational reporting, and owns only `tenant_registry`, `admin_audit`,
`platform_principal`, and `usage_export` (ADR-0003 §7). Business code reads `tenant_registry`
read-only through `src/factory_agent/ports/tenant_registry.py` for the suspended-tenant gate and
MES AppKey resolution.

`usage_event` is partitioned by month, so a partition must exist before an event can land in it.
The migration seeds the current and next month, and the `agent-api` lifespan task keeps that window
present afterwards (`FACTORY_AGENT_USAGE_PARTITION_SWEEP_INTERVAL_SECONDS`, default daily). A
maintenance failure is alerted, never silent — a missing partition means lost billing rows.
