# mock-mes

Deterministic MES simulator used only for local development, tests, and demonstrations.
It is independently runnable and is never a production dependency of `factory-agent`.

```bash
uv run --package mock-mes mock-mes
```

The API exposes `GET /health/live` and `GET /health/ready`.

## Deterministic data

The dataset is determined by `MOCK_MES_SCENARIO`, `MOCK_MES_SEED`, and
`MOCK_MES_VIRTUAL_NOW`. Supported scenarios are `small` and `standard`.

```bash
uv run --package mock-mes mock-mes-seed --scenario small --seed 20260821
```

Without `MOCK_MES_DATABASE_URL`, the command prints the reproducible dataset hash and manual
piecework totals. With a development/test PostgreSQL URL it resets tables created by:

```bash
uv run --package mock-mes mock-mes-migrate upgrade head
```

The default `small` dataset includes synthetic single-tenant identities (one user per tenant),
same-name employees, one employee in multiple groups, a mid-month transfer, cross-month work,
unsettled and rework records, defects, parallel operations, a zero plan, and a delayed order.

## Synthetic identity and faults

Canonical endpoints use synthetic bearer subjects `tenant-a-user`, `tenant-b-user`,
`single-tenant`, and `manager-a`. Each subject resolves to exactly one tenant membership; the
credential pair `(tenant_id, user_id)` is unique. The active `X-Tenant-Id` and supplied authorized
ID batches are checked against the deterministic server-side scope. These tokens and role mappings
are Mock-only behavior, not customer auth rules.

For one request, set `X-Mock-Fault` to `latency`, `429`, `5xx`, `duplicate_page`, `missing_page`,
`wrong_total`, `null`, or `field_drift`. `X-Mock-Latency-Ms` is bounded to 2000 ms. Faults affect
only `/v1/` and never persist into the next request.

## Unconfirmed assumptions

All IDs, names, role codes, organization types, statuses, effective-date boundaries, quantities,
piece rates, amounts, payroll values, and resource relationships are temporary Canonical development
fixtures. Time filters currently use UTC half-open intervals `[from, to)`. Piecework `amount` is a
stored synthetic value and does not establish a customer payroll formula.

Customer field mappings, enums, scope closure, transfer history, pagination limits, formulas, and
sensitive display rules remain open in
[`docs/api/customer-confirmation-questionnaire.md`](../docs/api/customer-confirmation-questionnaire.md)
and [`docs/product/requirements.md`](../docs/product/requirements.md).
