# factory-agent

`factory-agent` is a read-only factory MES assistant. Development follows nine numbered Markdown
Stories in `.github/story/`. The MES contract and Mock MES mirror the customer's real endpoints as
documented in [docs/reference/弘兆MES接口整体说明-V2.md](docs/reference/弘兆MES接口整体说明-V2.md).

## Start here

- [Stories](.github/story): the ordered implementation checklists.
- [Roadmap](ROADMAP.md): the nine-Story sequence and working approach.
- [Product requirements](docs/product/requirements.md): authoritative functional and quality scope.
- [Repository rules](AGENTS.md): architecture boundaries and development conventions.

Original requirements and earlier plans are immutable provenance snapshots under
`docs/requirements/source/` and `docs/reference/source-plans/`; they are not the current workflow.

## Services

Three buildable units live in this repository and never import each other:

- `factory-agent` (repository root): the read-only MES assistant. Since Story 11 it also writes
  usage metering (`usage_event`, the `*_fact` tables, `mes_operation_category`, `tenant_usage_*`)
  directly into the shared PostgreSQL in a separate transaction after its business commit — no
  outbox, no publisher, no cross-service usage-event contract. A metering failure is alerted and
  never rolls back or blocks an answer.
- `usage-admin/`: independently built production service for usage dashboards, tenant master data,
  and exports. It owns and writes `tenant_registry`, `admin_audit`, `platform_principal`, and
  `usage_export`; every other table in the shared database is read-only for it.
- `mock-mes/`: offline simulator mirroring the customer MES; development only, excluded from the
  production Compose topology.

Both production services migrate one shared database with separate Alembic version tables, so
migrations can run in any order.

## Development

```bash
make bootstrap
make check
make dev
```

Run the simulator separately with `make dev-mock`. Both APIs expose `GET /health/live` and
`GET /health/ready`.

For container-based local debugging, start PostgreSQL and Redis with `make middleware-up`, then
start the complete application stack with `make compose-up`. See
[deploy/compose/README.md](deploy/compose/README.md) for ports, databases, and cleanup commands.
