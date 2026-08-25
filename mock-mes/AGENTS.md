# Mock MES Rules

These rules apply to `mock-mes/`.

- This project is a deterministic simulator of the customer MES, not a production dependency or
  product shortcut. Since Story 5 it implements the customer's real 27 endpoints.
- Never import `factory_agent`; compatibility is verified through the OpenAPI contract.
- Generated data is determined by `(scenario, seed, virtual_now)`.
- Preserve tenant (AppKey), relation, quantity, payroll, and progress invariants. In particular
  `je = sl * price` must hold, the three payroll sources (`Type` 0/1/2) must reconcile with their
  originating endpoints, and scanned-operation sets must stay self-consistent across
  `WorktypeProgressQuery`, `SclzdBarcodeQuery`, `YskQuery`, and `WskQuery`.
- Simulate MES-side row-level filtering by bearer identity (`company`, `dept`,
  `move_admin_role="00"` = own data only). Never expose a way for the caller to widen it.
- Every response uses the `{code, message, result, timestamp}` envelope; list endpoints return
  `result.total` and, where the customer does, `result.footer`.
- Reproduce the customer's `code=0` messages verbatim and the HTTP 404 wrong-path case.
- `sign` is a deterministic placeholder. The real signing algorithm belongs to the customer and
  must not be reimplemented here.
- Organization is a single department (workshop) layer. Do not add group levels or assignment
  effective dates.
- Use PostgreSQL migrations for schema changes; startup code never creates production tables.
- Admin clock and fault endpoints are enabled only in test or development environments.
- Fault behavior must be opt-in and scoped to a test, request, or resettable scenario.
- Include hard cases without silently changing an existing fixture's expected business numbers.
- Add model/generator unit tests and consumer contract tests for every endpoint.
