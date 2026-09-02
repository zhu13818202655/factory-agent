# Usage Admin Rules

These rules apply to `usage-admin/`.

- This is an independently built production service; never import `factory_agent` or `mock_mes`.
- Never call MES endpoints or store MES business values, prompts, answers, or scope ID lists.
- Platform authorization uses a reviewed `PlatformScope`; it never reuses tenant MES roles.
- Metering tables are written by factory-agent in a separate transaction after its business
  commit (failures are alerted, never blocking or rolling back an answer); this service never
  ingests events and only reads the fact/rollup tables (Story 11 direct write).
- Database schema changes use Alembic migrations; startup never creates tables.
- Keep liveness local. Readiness may report missing optional development configuration without secrets.
- Unit tests use no network or real database.
- **Table ownership (breaking change, Story 9 / product doc §4.4)**: this service owns and writes
  `tenant_registry`, `admin_audit`, `platform_principal`, and `usage_export`; every other table in
  the shared PostgreSQL (business tables and all metering tables, including `mes_call_fact` and
  `mes_operation_category`) is factory-agent owned and **read-only here**. No table may appear in
  both migration histories; this service's Alembic directory contains only its four tables.
- **Separate Alembic version table**: `alembic_version_usage_admin` (factory-agent uses the default
  `alembic_version`) so both services can migrate one shared database in any order.
- **AppKey is a customer MES credential (D9)**: stored plaintext in `tenant_registry` only; every
  outbound representation goes through `mask_app_key` (first 6 chars + `***`) and must never appear
  in logs, traces, errors, exports, or test snapshots.
- **Deleting a factory account disables it (D10)**; history is never physically removed.
- **Authentication is token-first (D14~D16)**: `Authorization: Bearer <token>` (from
  `/auth/login` or `USAGE_ADMIN_API_TOKEN`) wins over the trusted-gateway three headers (dev/test
  direct channel). Writes to tenant registry and account registration require the `admin` role and
  are recorded in `admin_audit`.
