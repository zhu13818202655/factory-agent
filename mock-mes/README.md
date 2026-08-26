# mock-mes

Deterministic MES simulator used only for local development, tests, and demonstrations.
It is independently runnable and is never a production dependency of `factory-agent`.

```bash
uv run --package mock-mes mock-mes
```

The API exposes `GET /health/live`, `GET /health/ready`, and 27 customer-shaped
MES endpoints under `/api/`. Business endpoints use `POST` JSON requests with a
Bearer token and common `app_key`, `timestamp`, and `sign` body fields.

## Deterministic data

The dataset is determined by `MOCK_MES_SCENARIO`, `MOCK_MES_SEED`, and
`MOCK_MES_VIRTUAL_NOW`. Supported scenarios are `small` and `standard`.

```bash
uv run --package mock-mes mock-mes-seed --scenario small --seed 20260821
```

The command builds the dataset in memory from `(scenario, seed, virtual_now)` and prints the
reproducible dataset hash and manual piecework totals. There is no database and no migration step.

The default `small` dataset includes deterministic identities for a factory owner,
workshop manager, own-data-only worker, and a second company. It covers multiple
workshops, orders, parallel worktypes, scanned and unscanned work, manual defects,
delayed orders, and zero plans. `(scenario, seed, virtual_now)` determines the
complete dataset.

## Synthetic identity and faults

The token endpoint returns a customer-shaped credential bundle including a
deterministic JWT-shaped `accessToken`, `sign`, timestamp, plaintext AppKey, and
empty roles/permissions. Mock identities also accept the legacy test tokens
`MOCK-TOKEN-01009`, `MOCK-TOKEN-01008`, and `MOCK-TOKEN-01001`.

Bearer identity drives company, workshop, and own-data filtering. Request-body
filters narrow results only; they cannot widen the identity's visibility.

For one request, set `X-Mock-Fault` to `latency`, `429`, `5xx`, `404`, `duplicate_page`,
`missing_page`, `wrong_total`, `footer_mismatch`, `null`, or `field_drift`.
`X-Mock-Latency-Ms` is bounded to 2000 ms. Faults affect only the request and never persist.

## Unconfirmed assumptions

All IDs, names, role codes, quantities, piece rates, payroll values, and relationships are
deterministic development fixtures. Unconfirmed business formulas are not represented as facts.

Customer field mappings and unconfirmed metric semantics are recorded in
[`docs/api/field-dictionary.md`](../docs/api/field-dictionary.md) and the questionnaire.
Unconfirmed values must be surfaced as `unavailable`.
