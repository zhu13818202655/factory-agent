# factory-agent

`factory-agent` is a read-only factory MES assistant. The customer MES contract is documented in
[docs/product/AI问答对外接口-整理.md](docs/product/AI问答对外接口-整理.md); product requirements
and confirmed customer answers live in [docs/product/需求及方案整理.md](docs/product/需求及方案整理.md).

## Start here

- [Product requirements](docs/product/需求及方案整理.md): authoritative functional and quality scope.
- [Repository rules](AGENTS.md): architecture boundaries and development conventions.

Superseded requirements and customer documents stay recoverable through git history and are not kept
as live provenance copies.

## Services

Two buildable units live in this repository and never import each other:

- `factory-agent` (repository root): the read-only MES assistant. It also writes usage metering
  (`usage_event`, the `*_fact` tables, `mes_operation_category`, `tenant_usage_*`) directly into
  the shared PostgreSQL in a separate transaction after its business commit — no outbox, no
  publisher, no cross-service usage-event contract. A metering failure is alerted and never rolls
  back or blocks an answer.
- `usage-admin/`: independently built production service for usage dashboards, tenant master data,
  and exports. It owns and writes `tenant_registry`, `admin_audit`, `platform_principal`, and
  `usage_export`; every other table in the shared database is read-only for it.

Both production services migrate one shared database with separate Alembic version tables, so
migrations can run in any order.

## Exports and object storage

Exports are generated on demand and retained on disk (never held only in memory), with the
customer-confirmed 90-day default retention window and lazy cleanup on access. Both services write
their artifacts to the same S3-compatible gateway but keep separate buckets, and both serve
downloads through their own backend-proxied endpoint: the service re-checks ownership, writes an
audit event, and refuses to release bytes when that audit fails (fail-closed).

| Service | Backend selection | Default bucket |
|---|---|---|
| `factory-agent` | `FACTORY_AGENT_S3_ENDPOINT_URL` set → S3; empty → `FACTORY_AGENT_EXPORT_STORE_DIR` local directory | `factory-agent-exports` |
| `usage-admin` | `USAGE_ADMIN_S3_ENDPOINT_URL` set → S3; empty → `USAGE_ADMIN_EXPORT_STORE_DIR`; both empty → memory (dev/test only, lost on restart) | `usage-admin-exports` |

The reference deployment uses single-node SeaweedFS; `make middleware-up` starts it alongside
PostgreSQL and Redis, and `make compose-up` starts the complete stack. See
[ADR-0009](docs/adr/0009-export-artifact-storage-backend.md) for the decisions behind the storage
split and [deploy/compose/README.md](deploy/compose/README.md) for the deployment details.

## Development

```bash
make bootstrap
make check
make dev
```

Both APIs expose `GET /health/live` and `GET /health/ready`.

For container-based local debugging, start PostgreSQL and Redis with `make middleware-up`, then
start the complete application stack with `make compose-up`. See
[deploy/compose/README.md](deploy/compose/README.md) for ports, databases, and cleanup commands.
