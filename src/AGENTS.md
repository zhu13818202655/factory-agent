# Production Application Rules

These rules apply to `src/factory_agent/`.

- Keep dependency direction `api -> application -> domain`.
- Domain code must not import FastAPI, HTTP clients, database drivers, or LLM SDKs.
- Application use cases depend on typed protocols supplied through dependency injection.
- Only `data_api/` may import an HTTP client for MES traffic.
- Permission evaluation must finish before a business-data source method is called.
- Scope parameters come from `DataScope`, never directly from user or model output.
- Validate external payloads before they reach domain objects or DuckDB.
- Do not import `mock_mes`; package-boundary tests enforce this.
- Keep liveness checks local. Readiness checks may inspect required dependencies with bounded
  timeouts and must not leak connection details.
- New behavior requires the closest unit test before wider integration coverage.
