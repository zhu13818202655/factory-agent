# Contract Rules

These rules apply to `contracts/`.

- `mes-canonical.openapi.yaml` is the stable consumer contract for MES adapters and Mock MES.
- Breaking removals, type changes, semantic changes, and pagination changes require review.
- Additive optional response fields are backward-compatible unless documented otherwise.
- Every list response exposes `items`, `total`, `page`, and `size`.
- Batch ID filters are preferred; unbounded N+1 behavior is prohibited.
- Customer contracts and examples are versioned and sanitized before entering Git.
- Generated clients are never edited manually and live under the consuming project.
- Examples contain synthetic data only and must validate against their schemas.
