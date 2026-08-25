# Contract Rules

These rules apply to `contracts/`.

- `mes-canonical.openapi.yaml` is the stable consumer contract for the MES adapter and Mock MES.
  Since Story 5 it mirrors the customer's real 27 endpoints, not an invented resource model.
- Every endpoint is `POST` + `application/json`, carries `Authorization: Bearer {accessToken}`,
  and includes `app_key` / `timestamp` / `sign` in the request body.
- Every response uses the envelope `{code, message, result, timestamp}`. `code` is `1` (success)
  or `0` (failure); failures are distinguished by `message` text, not by a code taxonomy.
- Every list response exposes `result.list` and `result.total`. Some endpoints add
  `result.footer` totals; `footer` is an independent reconciliation source, never a substitute
  for the detail rows.
- Permission denial has no dedicated error code. It surfaces as filtered-out (empty) data.
- `operationId` uses the customer endpoint name (for example `GongziMxQuery`). Do not reintroduce
  `A1_` / `C1_`-style synthetic identifiers.
- Breaking removals, type changes, semantic changes, and pagination changes require review.
- Additive optional response fields are backward-compatible unless documented otherwise.
- Batch ID filters are preferred; where the customer only supports single-key lookup
  (for example `WorktypeProgressQuery`), the N+1 must be bounded by an execution budget.
- Customer contracts and examples are versioned under `contracts/customer/<date>/` and sanitized
  before entering Git.
- Generated clients are never edited manually and live under the consuming project.
- Examples contain synthetic data only and must validate against their schemas.
