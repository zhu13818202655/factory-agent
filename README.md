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
