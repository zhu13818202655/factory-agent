# factory-agent

`factory-agent` is a read-only factory MES assistant. Development follows nine numbered Markdown
Stories in `.github/story/`, using the Canonical contract and Mock MES until customer APIs arrive.

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
