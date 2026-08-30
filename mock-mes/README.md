# mock-mes

Deterministic MES simulator used only for local development, tests, and demonstrations.
It is independently runnable and is never a production dependency of `factory-agent`.

```bash
# 1. Start the data base (PostgreSQL 16) — e.g. the Compose postgres service.
# 2. Apply the schema (Alembic; startup code never creates tables).
uv run --package mock-mes mock-mes-migrate upgrade head
# 3. Generate the production-like data window (idempotent, deterministic).
uv run --package mock-mes mock-mes-generate --fill-missing
# 4. Run the API.
uv run --package mock-mes mock-mes
```

The API exposes `GET /health/live`, `GET /health/ready`, and 27 customer-shaped
MES endpoints under `/api/`. Business endpoints use `POST` JSON requests with a
Bearer token and common `app_key`, `timestamp`, and `sign` body fields.
`/health/ready` verifies the live database connection.

## Data base

Since Story 10 the data base is PostgreSQL — the **only** data source. There is
no in-memory dataset and no memory fallback; a missing `MOCK_MES_DATABASE_URL`
is a loud startup error. The API process is read-only; the generator process
writes. All 20 `mock_*` tables store the full Story-5 record in a `payload`
JSONB column plus mirrored columns for SQL row-level filtering and SQL
aggregation.

Alembic migrations live in `migrations/` with a dedicated version table
(`alembic_version_mock_mes`) so mock-mes can share a PostgreSQL server with
factory-agent and usage-admin.

## Deterministic data

The generator is a separate process/CLI. Each day of the data window is decided
by `(seed, day)` plus the factory-scale settings:

```bash
uv run --package mock-mes mock-mes-generate --fill-missing
uv run --package mock-mes mock-mes-generate --day 2026-08-28 --days 1
uv run --package mock-mes mock-mes-generate --start 2026-08-01 --end 2026-08-31
```

- **Factory scale**: defaults to a realistic ~500-person plant — 500 employees
  across 5 departments, four role tiers (00 worker / 01 group leader / 02
  manager / 99 boss, one boss per factory, one manager per department, one
  group leader per 10 workers), 24 styles and ~3 new orders per workday. Every
  value is overridable via `MOCK_MES_HEADCOUNT`, `MOCK_MES_DEPARTMENTS`,
  `MOCK_MES_GROUP_SIZE`, `MOCK_MES_DAILY_ACTIVE_RATIO`,
  `MOCK_MES_SCANS_PER_WORKER`, `MOCK_MES_DAILY_HIRES`, `MOCK_MES_PLANS_PER_DAY`,
  `MOCK_MES_STYLES`, … (all with defaults).
- **Data window**: from `MOCK_MES_DATA_START` (default: Jan 1 of the previous
  year) up to `MOCK_MES_VIRTUAL_NOW` / today — future data is never generated.
- **缺日补齐**: the window is scanned day by day; days that already have a
  batch row are skipped, only missing days are generated. The same
  `(seed, day)` input is deterministic and idempotent; writes use batch COPY so
  the full year-plus window (~600 days, ~1M rows) generates in about a minute.
- **Batch ledger**: `mock_generate_batch` records `run_id`, day, seed, row
  count, the JSON-normalised data hash and status, so runs are auditable,
  replayable and comparable against the interface golden.
- The anchored Story-5/6/7 fixtures stay byte-identical on their historic
  dates; rolling rows add production-like variety (work calendar, shifts,
  delayed orders, defects, cross-workshop flows, one-worker-many-orders,
  scanned/unscanned mixes).

## Synthetic identity and faults

The token endpoint returns a customer-shaped credential bundle including a
deterministic JWT-shaped `accessToken`, `sign`, timestamp, plaintext AppKey, and
empty roles/permissions. Mock identities also accept the legacy test tokens
`MOCK-TOKEN-01009`, `MOCK-TOKEN-01008`, `MOCK-TOKEN-01001`, and `MOCK-TOKEN-02001`.

Bearer identity drives company, workshop, and own-data filtering **in SQL**.
Request-body filters narrow results only; they cannot widen the identity's
visibility.

For one request, set `X-Mock-Fault` to `latency`, `429`, `5xx`, `404`, `duplicate_page`,
`missing_page`, `wrong_total`, `footer_mismatch`, `null`, or `field_drift`.
`X-Mock-Latency-Ms` is bounded to 2000 ms. Faults affect only the request and never persist.

## Environment

All settings use the `MOCK_MES_` prefix: `HOST`, `PORT`, `SCENARIO`, `SEED`,
`VIRTUAL_NOW`, `DATABASE_URL` (required), `DATA_START`, `DATA_END`.
Credentials come only from the environment.

## Unconfirmed assumptions

All IDs, names, role codes, quantities, piece rates, payroll values, and relationships are
deterministic development fixtures. Unconfirmed business formulas are not represented as facts.

Customer field mappings and unconfirmed metric semantics are recorded in
[`docs/api/field-dictionary.md`](../docs/api/field-dictionary.md) and the questionnaire.
Unconfirmed values must be surfaced as `unavailable`.
