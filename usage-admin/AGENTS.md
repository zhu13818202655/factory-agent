# Usage Admin Rules

These rules apply to `usage-admin/`.

- This is an independently built production service; never import `factory_agent` or `mock_mes`.
- Never call MES endpoints or store MES business values, prompts, answers, or scope ID lists.
- Platform authorization uses a reviewed `PlatformScope`; it never reuses tenant MES roles.
- Ingest is idempotent by `event_id` and rejects conflicting payload digests.
- Database schema changes use Alembic migrations; startup never creates tables.
- Keep liveness local. Readiness may report missing optional development configuration without secrets.
- Unit tests use no network or real database.
