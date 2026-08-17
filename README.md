# factory-agent

`factory-agent` is a read-only factory MES assistant. The repository is being built through
the phased implementation board in `docs/implementation-kanban.md`.

## Development

```bash
make bootstrap
make check
make dev
```

Run the simulator separately with `make dev-mock`. Both APIs expose `GET /health/live` and
`GET /health/ready`.
