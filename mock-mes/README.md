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

The data base is PostgreSQL — the **only** data source. There is
no in-memory dataset and no memory fallback; a missing `MOCK_MES_DATABASE_URL`
is a loud startup error. The API process is read-only; the generator process
writes. All 20 `mock_*` tables store the full customer-shaped record in a `payload`
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
  group leader per 10 workers) and a **小组 (group) model**: every 组长/员工
  belongs to a 车间下的生产小组 carried on the employee master (`group` /
  `group_id`), so 01 leaders can be scoped to their own group. The manager of
  the second workshop also binds the fourth workshop (cross-workshop
  multi-dept binding, 客户确认), 24 styles and ~3 new orders per workday. Every
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
- The anchored fixtures stay byte-identical on their historic
  dates; rolling rows add production-like variety (work calendar, shifts,
  delayed orders, defects, cross-workshop flows, one-worker-many-orders,
  scanned/unscanned mixes).

## Synthetic identity, roles and faults

Login identities are the **generated employee master**: the boss (99), one
manager per department (02, one of them bound across workshops), group leaders
(01) and workers (00) all come from PostgreSQL, so any generated account can
authenticate at ~500-person scale — there is no static identity fixture.

`POST /api/system/token` returns a customer-shaped credential bundle:
`accessToken` (JWT-shaped, carrying `user`/`dept`/`roles`), `sign`, `timestamp`,
plaintext `appkey`, plus the authoritative **`roles` code string** (00/01/02/99),
`dept` and `boundDepts` (a manager's bound 车间/部门 set). The bundle is issued
for a generated employee chosen by an optional dev-only `uid` body field;
without one the tenant's boss is used. Legacy `MOCK-TOKEN-<uid>` tokens are also
accepted (e.g. `MOCK-TOKEN-01009` boss, `01008` manager, `01012` group leader,
`01001` worker, `02001` COMPANY-B worker).

Bearer identity drives company and role-scope filtering **in SQL**: 99 whole
company; 02 the bound 车间/部门 (possibly several, cross-workshop); 01 their
bound 小组 on personal (uid-attributed) rows — 车间-level organisational tables
without uid stay dept-scoped for leaders (mock simplification, real-MES
behaviour is a joint-debug item); 00 own rows only. **Base-data interfaces
(员工/部门等) are NOT role-filtered** — they return the full company set.
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

Customer field mappings and metric semantics follow the customer interface contract in
`docs/product/AI问答对外接口-整理.md` and the confirmed answers in
`docs/product/需求及方案整理.md`. Unconfirmed values must be surfaced as `unavailable`.
