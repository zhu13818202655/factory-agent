# mock-mes

Deterministic MES simulator used only for local development, tests, and demonstrations.
It is independently runnable and is never a production dependency of `factory-agent`.

```bash
uv run --package mock-mes mock-mes
```

The API exposes `GET /health/live` and `GET /health/ready`.
