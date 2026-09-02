"""Usage store boundary and implementations.

The service and API layers depend only on ``UsageStore``, so unit tests can
inject the in-memory implementation without a database or network. The
PostgreSQL implementation reads the metering tables owned and written by
factory-agent (``usage_event`` / ``*_fact`` / ``tenant_usage_*``) and
owns the writes to this service's tables (``tenant_registry`` /
``platform_principal`` / ``admin_audit`` / ``usage_export``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import psycopg
import psycopg.rows

from usage_admin.events import InteractionFact, LlmCallFact, MesCallFact


@dataclass(frozen=True, slots=True)
class RollupRow:
    tenant_id: str
    bucket_start: datetime
    metric: str
    value: float
    rollup_version: str
    rolled_up_at: datetime
    granularity: str = "hour"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    audit_id: str
    principal_id: str
    action: str
    target: str | None
    detail: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExportRecord:
    export_id: str
    principal_id: str
    format: str
    tenant_filter: dict[str, object]
    metric_version: str
    status: str
    artifact_key: str | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TenantRegistryRecord:
    """Tenant master data owned by this service.

    ``app_key`` is the primary key and the tenant identifier itself (D12);
    ``status`` is ``active`` or ``disabled`` (D10/D13). AppKey is stored in
    plaintext (D9) but every outbound response masks it.
    """

    app_key: str
    tenant_name: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlatformPrincipalRecord:
    """Internal platform operations account (D15), isolated from MES users."""

    principal_id: str
    username: str
    password_hash: str
    role: str
    tenant_scope: tuple[str, ...]
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MesOperationCategory:
    """operation_id -> billing category mapping (factory-agent owned, read-only)."""

    operation_id: str
    category: str
    version: str | None = None


class UsageStore(Protocol):
    """Persistent boundary used by ops, exports, and tenant/principal services."""

    async def list_interaction_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[InteractionFact]: ...

    async def list_llm_call_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[LlmCallFact]: ...

    async def list_rollup_rows(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime, granularity: str
    ) -> list[RollupRow]: ...

    async def list_tenants(self, start: datetime, end: datetime) -> list[str]: ...

    async def query_duration_percentiles(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> dict[str, dict[str, float | None]]: ...

    async def query_user_activity(
        self,
        tenant_ids: frozenset[str],
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[str, int]], int]: ...

    async def query_freshness(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> datetime | None: ...

    async def query_distinct_counts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> dict[str, int]: ...

    async def record_audit(self, entry: AuditEntry) -> None: ...

    async def purge_audit_before(self, cutoff: datetime) -> int: ...

    async def create_export(self, export: ExportRecord) -> None: ...

    async def get_export(self, export_id: str) -> ExportRecord | None: ...

    async def mark_export_ready(self, export_id: str, artifact_key: str) -> None: ...

    async def list_exports(self, principal_id: str, limit: int) -> list[ExportRecord]: ...

    # --- Tenant master data (owned by this service) ---

    async def list_tenant_registry(
        self, limit: int, offset: int
    ) -> tuple[list[TenantRegistryRecord], int]: ...

    async def list_all_tenant_registry(self) -> list[TenantRegistryRecord]: ...

    async def get_tenant_registry(self, app_key: str) -> TenantRegistryRecord | None: ...

    async def create_tenant_registry(self, record: TenantRegistryRecord) -> bool: ...

    async def update_tenant_registry(
        self,
        app_key: str,
        *,
        tenant_name: str | None,
        status: str | None,
        updated_at: datetime,
    ) -> TenantRegistryRecord | None: ...

    async def search_tenant_registry_names(self, fragment: str) -> list[str]: ...

    # --- Platform principal accounts (owned by this service) ---

    async def create_principal(self, record: PlatformPrincipalRecord) -> bool: ...

    async def get_principal(self, principal_id: str) -> PlatformPrincipalRecord | None: ...

    async def get_principal_by_username(self, username: str) -> PlatformPrincipalRecord | None: ...

    # --- MES metering facts (factory-agent owned; read-only here) ---

    async def list_mes_call_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[MesCallFact]: ...

    async def list_mes_operation_categories(self) -> list[MesOperationCategory]: ...


@dataclass
class InMemoryUsageStore:
    """In-memory ``UsageStore`` for unit tests and offline development."""

    interaction_facts: list[InteractionFact] = field(default_factory=list[InteractionFact])
    llm_call_facts: list[LlmCallFact] = field(default_factory=list[LlmCallFact])
    mes_call_facts: list[MesCallFact] = field(default_factory=list[MesCallFact])
    mes_operation_categories: list[MesOperationCategory] = field(
        default_factory=list[MesOperationCategory]
    )
    rollup_rows: list[RollupRow] = field(default_factory=list[RollupRow])
    audits: list[AuditEntry] = field(default_factory=list[AuditEntry])
    exports: dict[str, ExportRecord] = field(default_factory=dict[str, ExportRecord])
    tenant_registry: dict[str, TenantRegistryRecord] = field(
        default_factory=dict[str, TenantRegistryRecord]
    )
    principals: dict[str, PlatformPrincipalRecord] = field(
        default_factory=dict[str, PlatformPrincipalRecord]
    )

    async def list_interaction_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[InteractionFact]:
        return [
            fact
            for fact in self.interaction_facts
            if fact.tenant_id in tenant_ids and start <= fact.occurred_at < end
        ]

    async def list_llm_call_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[LlmCallFact]:
        return [
            fact
            for fact in self.llm_call_facts
            if fact.tenant_id in tenant_ids and start <= fact.occurred_at < end
        ]

    async def list_rollup_rows(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime, granularity: str
    ) -> list[RollupRow]:
        return [
            row
            for row in self.rollup_rows
            if row.tenant_id in tenant_ids
            and start <= row.bucket_start < end
            and row.granularity == granularity
        ]

    async def list_tenants(self, start: datetime, end: datetime) -> list[str]:
        tenants: set[str] = set()
        for fact in self.interaction_facts:
            if start <= fact.occurred_at < end:
                tenants.add(fact.tenant_id)
        for fact in self.llm_call_facts:
            if start <= fact.occurred_at < end:
                tenants.add(fact.tenant_id)
        for fact in self.mes_call_facts:
            if start <= fact.occurred_at < end:
                tenants.add(fact.tenant_id)
        return sorted(tenants)

    async def query_duration_percentiles(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> dict[str, dict[str, float | None]]:
        values: dict[str, list[float]] = {metric: [] for metric in _DURATION_COLUMN}
        for fact in self.interaction_facts:
            if fact.tenant_id not in tenant_ids:
                continue
            if not (start <= fact.occurred_at < end):
                continue
            for metric, column in _DURATION_COLUMN.items():
                value = getattr(fact, column, None)
                if isinstance(value, (int, float)):
                    values[metric].append(float(value))
        return {
            metric: {str(p): percentile(sorted(vals), p) for p in (50, 95, 99)}
            for metric, vals in values.items()
        }

    async def query_user_activity(
        self,
        tenant_ids: frozenset[str],
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[str, int]], int]:
        counts: dict[str, int] = {}
        for fact in self.interaction_facts:
            if fact.tenant_id not in tenant_ids:
                continue
            if not (start <= fact.occurred_at < end):
                continue
            if fact.event_type != "interaction_started":
                continue
            counts[fact.user_subject_id] = counts.get(fact.user_subject_id, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ordered[offset : offset + limit], len(ordered)

    async def query_freshness(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> datetime | None:
        latest: datetime | None = None
        for fact in [*self.interaction_facts, *self.llm_call_facts]:
            if fact.tenant_id not in tenant_ids:
                continue
            if start <= fact.received_at < end and (latest is None or fact.received_at > latest):
                latest = fact.received_at
        return latest

    async def query_distinct_counts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> dict[str, int]:
        users: set[str] = set()
        calls: set[str] = set()
        for fact in self.interaction_facts:
            if fact.tenant_id not in tenant_ids:
                continue
            if start <= fact.occurred_at < end:
                users.add(fact.user_subject_id)
        for fact in self.llm_call_facts:
            if fact.tenant_id not in tenant_ids:
                continue
            if start <= fact.occurred_at < end:
                calls.add(fact.logical_call_id)
        return {"users": len(users), "llm_logical_calls": len(calls)}

    async def record_audit(self, entry: AuditEntry) -> None:
        self.audits.append(entry)

    async def purge_audit_before(self, cutoff: datetime) -> int:
        before = len(self.audits)
        self.audits = [entry for entry in self.audits if entry.created_at >= cutoff]
        return before - len(self.audits)

    async def create_export(self, export: ExportRecord) -> None:
        self.exports[export.export_id] = export

    async def get_export(self, export_id: str) -> ExportRecord | None:
        return self.exports.get(export_id)

    async def mark_export_ready(self, export_id: str, artifact_key: str) -> None:
        export = self.exports.get(export_id)
        if export is not None:
            self.exports[export_id] = ExportRecord(
                export_id=export.export_id,
                principal_id=export.principal_id,
                format=export.format,
                tenant_filter=export.tenant_filter,
                metric_version=export.metric_version,
                status="ready",
                artifact_key=artifact_key,
                created_at=export.created_at,
                expires_at=export.expires_at,
            )

    async def list_exports(self, principal_id: str, limit: int) -> list[ExportRecord]:
        return [export for export in self.exports.values() if export.principal_id == principal_id][
            :limit
        ]

    async def list_mes_call_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[MesCallFact]:
        return [
            fact
            for fact in self.mes_call_facts
            if fact.tenant_id in tenant_ids and start <= fact.occurred_at < end
        ]

    async def list_mes_operation_categories(self) -> list[MesOperationCategory]:
        return list(self.mes_operation_categories)

    async def list_tenant_registry(
        self, limit: int, offset: int
    ) -> tuple[list[TenantRegistryRecord], int]:
        ordered = sorted(self.tenant_registry.values(), key=lambda record: record.created_at)
        return ordered[offset : offset + limit], len(ordered)

    async def list_all_tenant_registry(self) -> list[TenantRegistryRecord]:
        return list(self.tenant_registry.values())

    async def get_tenant_registry(self, app_key: str) -> TenantRegistryRecord | None:
        return self.tenant_registry.get(app_key)

    async def create_tenant_registry(self, record: TenantRegistryRecord) -> bool:
        if record.app_key in self.tenant_registry:
            return False
        self.tenant_registry[record.app_key] = record
        return True

    async def update_tenant_registry(
        self,
        app_key: str,
        *,
        tenant_name: str | None,
        status: str | None,
        updated_at: datetime,
    ) -> TenantRegistryRecord | None:
        existing = self.tenant_registry.get(app_key)
        if existing is None:
            return None
        updated = TenantRegistryRecord(
            app_key=existing.app_key,
            tenant_name=tenant_name if tenant_name is not None else existing.tenant_name,
            status=status if status is not None else existing.status,
            created_at=existing.created_at,
            updated_at=updated_at,
        )
        self.tenant_registry[app_key] = updated
        return updated

    async def search_tenant_registry_names(self, fragment: str) -> list[str]:
        needle = fragment.lower()
        return sorted(
            record.app_key
            for record in self.tenant_registry.values()
            if needle in record.tenant_name.lower()
        )

    async def create_principal(self, record: PlatformPrincipalRecord) -> bool:
        if any(principal.username == record.username for principal in self.principals.values()):
            return False
        self.principals[record.principal_id] = record
        return True

    async def get_principal(self, principal_id: str) -> PlatformPrincipalRecord | None:
        return self.principals.get(principal_id)

    async def get_principal_by_username(self, username: str) -> PlatformPrincipalRecord | None:
        for principal in self.principals.values():
            if principal.username == username:
                return principal
        return None


async def _connect(database_url: str) -> psycopg.AsyncConnection[Any]:
    return await psycopg.AsyncConnection.connect(
        database_url,
        row_factory=psycopg.rows.dict_row,  # type: ignore[arg-type]
    )


class PostgresUsageStore:
    """Production ``UsageStore`` over PostgreSQL 16.

    Connection-per-operation keeps the service stateless; every statement is
    parameterised. Reads cover the metering tables owned by factory-agent
    and writes touch only this service's own tables
    (``tenant_registry`` / ``platform_principal`` / ``admin_audit`` /
    ``usage_export``).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def list_interaction_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[InteractionFact]:
        if not tenant_ids:
            return []
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT * FROM interaction_fact
                WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                """,
                (list(tenant_ids), start, end),
            )
            fetched = await rows.fetchall()
        return [_interaction_fact_from_row(row) for row in fetched]

    async def list_llm_call_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[LlmCallFact]:
        if not tenant_ids:
            return []
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT * FROM llm_call_fact
                WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                """,
                (list(tenant_ids), start, end),
            )
            fetched = await rows.fetchall()
        return [_llm_call_fact_from_row(row) for row in fetched]

    async def list_rollup_rows(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime, granularity: str
    ) -> list[RollupRow]:
        if not tenant_ids:
            return []
        async with await _connect(self._database_url) as connection:
            if granularity == "hour":
                result = await connection.execute(
                    """
                    SELECT tenant_id, bucket_start, metric, value, rollup_version, rolled_up_at
                    FROM tenant_usage_hourly
                    WHERE tenant_id = ANY(%s) AND bucket_start >= %s AND bucket_start < %s
                    """,
                    (list(tenant_ids), start, end),
                )
            else:
                result = await connection.execute(
                    """
                    SELECT tenant_id, bucket_start, metric, value, rollup_version, rolled_up_at
                    FROM tenant_usage_daily
                    WHERE tenant_id = ANY(%s) AND bucket_date >= %s AND bucket_date < %s
                    """,
                    (list(tenant_ids), start.date(), end.date()),
                )
            fetched = await result.fetchall()
        rows: list[RollupRow] = []
        for row in fetched:
            rows.append(
                RollupRow(
                    tenant_id=str(row["tenant_id"]),
                    bucket_start=row["bucket_start"],
                    metric=str(row["metric"]),
                    value=float(row["value"]),
                    rollup_version=str(row["rollup_version"]),
                    rolled_up_at=row["rolled_up_at"],
                    granularity=granularity,
                )
            )
        return rows

    async def list_tenants(self, start: datetime, end: datetime) -> list[str]:
        try:
            async with await _connect(self._database_url) as connection:
                rows = await connection.execute(
                    """
                    SELECT DISTINCT tenant_id FROM interaction_fact
                    WHERE occurred_at >= %s AND occurred_at < %s
                    UNION
                    SELECT DISTINCT tenant_id FROM llm_call_fact
                    WHERE occurred_at >= %s AND occurred_at < %s
                    UNION
                    SELECT DISTINCT tenant_id FROM mes_call_fact
                    WHERE occurred_at >= %s AND occurred_at < %s
                    """,
                    (start, end, start, end, start, end),
                )
                fetched = await rows.fetchall()
        except psycopg.errors.UndefinedTable:
            # A missing table (un-migrated database) simply yields no MES tenants.
            return []
        return sorted(str(row["tenant_id"]) for row in fetched)

    async def query_duration_percentiles(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> dict[str, dict[str, float | None]]:
        if not tenant_ids:
            return {}
        result: dict[str, dict[str, float | None]] = {}
        async with await _connect(self._database_url) as connection:
            for metric, column in _DURATION_COLUMN.items():
                rows = await connection.execute(
                    f"""
                    SELECT
                        percentile_cont(0.50) WITHIN GROUP (ORDER BY {column}) AS p50,
                        percentile_cont(0.95) WITHIN GROUP (ORDER BY {column}) AS p95,
                        percentile_cont(0.99) WITHIN GROUP (ORDER BY {column}) AS p99
                    FROM interaction_fact
                    WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                      AND {column} IS NOT NULL
                    """,  # nosec B608 - column names come from the fixed _DURATION_COLUMN map  # type: ignore[arg-type]  # trusted fixed column names
                    (list(tenant_ids), start, end),
                )
                row = await rows.fetchone()
                result[metric] = {
                    "50": _opt_float(row["p50"] if row else None),
                    "95": _opt_float(row["p95"] if row else None),
                    "99": _opt_float(row["p99"] if row else None),
                }
        return result

    async def query_user_activity(
        self,
        tenant_ids: frozenset[str],
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[str, int]], int]:
        if not tenant_ids:
            return [], 0
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT user_subject_id, COUNT(*) AS question_count
                FROM interaction_fact
                WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                  AND event_type = 'interaction_started'
                GROUP BY user_subject_id
                ORDER BY question_count DESC, user_subject_id ASC
                LIMIT %s OFFSET %s
                """,
                (list(tenant_ids), start, end, limit, offset),
            )
            fetched = await rows.fetchall()
            total_row = await connection.execute(
                """
                SELECT COUNT(DISTINCT user_subject_id) AS total
                FROM interaction_fact
                WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                  AND event_type = 'interaction_started'
                """,
                (list(tenant_ids), start, end),
            )
            total_row = await total_row.fetchone()
        pairs = [(str(row["user_subject_id"]), int(row["question_count"])) for row in fetched]
        return pairs, int(total_row["total"]) if total_row else 0

    async def query_freshness(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> datetime | None:
        if not tenant_ids:
            return None
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT MAX(received_at) AS latest
                FROM (
                    SELECT received_at FROM interaction_fact
                    WHERE tenant_id = ANY(%s) AND received_at >= %s AND received_at < %s
                    UNION ALL
                    SELECT received_at FROM llm_call_fact
                    WHERE tenant_id = ANY(%s) AND received_at >= %s AND received_at < %s
                ) AS seen
                """,
                (list(tenant_ids), start, end, list(tenant_ids), start, end),
            )
            row = await rows.fetchone()
        return row["latest"] if row else None

    async def query_distinct_counts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> dict[str, int]:
        if not tenant_ids:
            return {"users": 0, "llm_logical_calls": 0}
        async with await _connect(self._database_url) as connection:
            user_row = await connection.execute(
                """
                SELECT COUNT(DISTINCT user_subject_id) AS count
                FROM interaction_fact
                WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                """,
                (list(tenant_ids), start, end),
            )
            user_row = await user_row.fetchone()
            call_row = await connection.execute(
                """
                SELECT COUNT(DISTINCT logical_call_id) AS count
                FROM llm_call_fact
                WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                """,
                (list(tenant_ids), start, end),
            )
            call_row = await call_row.fetchone()
        return {
            "users": int(user_row["count"]) if user_row else 0,
            "llm_logical_calls": int(call_row["count"]) if call_row else 0,
        }

    async def record_audit(self, entry: AuditEntry) -> None:
        async with await _connect(self._database_url) as connection:
            await connection.execute(
                """
                INSERT INTO admin_audit (audit_id, principal_id, action, target, detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    entry.audit_id,
                    entry.principal_id,
                    entry.action,
                    entry.target,
                    json.dumps(entry.detail),
                    entry.created_at,
                ),
            )

    async def purge_audit_before(self, cutoff: datetime) -> int:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                "DELETE FROM admin_audit WHERE created_at < %s", (cutoff,)
            )
        return rows.rowcount

    async def create_export(self, export: ExportRecord) -> None:
        async with await _connect(self._database_url) as connection:
            await connection.execute(
                """
                INSERT INTO usage_export
                    (export_id, principal_id, format, tenant_filter, metric_version, status,
                     artifact_key, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    export.export_id,
                    export.principal_id,
                    export.format,
                    json.dumps(export.tenant_filter),
                    export.metric_version,
                    export.status,
                    export.artifact_key,
                    export.created_at,
                    export.expires_at,
                ),
            )

    async def get_export(self, export_id: str) -> ExportRecord | None:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                "SELECT * FROM usage_export WHERE export_id = %s", (export_id,)
            )
            row = await rows.fetchone()
        if row is None:
            return None
        return _export_from_row(row)

    async def mark_export_ready(self, export_id: str, artifact_key: str) -> None:
        async with await _connect(self._database_url) as connection:
            await connection.execute(
                """
                UPDATE usage_export SET status = 'ready', artifact_key = %s WHERE export_id = %s
                """,
                (artifact_key, export_id),
            )

    async def list_exports(self, principal_id: str, limit: int) -> list[ExportRecord]:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT * FROM usage_export WHERE principal_id = %s ORDER BY created_at DESC LIMIT %s
                """,
                (principal_id, limit),
            )
            fetched = await rows.fetchall()
        return [_export_from_row(row) for row in fetched]

    async def list_mes_call_facts(
        self, tenant_ids: frozenset[str], start: datetime, end: datetime
    ) -> list[MesCallFact]:
        if not tenant_ids:
            return []
        try:
            async with await _connect(self._database_url) as connection:
                rows = await connection.execute(
                    """
                    SELECT * FROM mes_call_fact
                    WHERE tenant_id = ANY(%s) AND occurred_at >= %s AND occurred_at < %s
                    """,
                    (list(tenant_ids), start, end),
                )
                fetched = await rows.fetchall()
        except psycopg.errors.UndefinedTable:
            # mes_call_fact is created and written by factory-agent; a missing table
            # yields an empty query result, never an error.
            return []
        return [_mes_call_fact_from_row(row) for row in fetched]

    async def list_mes_operation_categories(self) -> list[MesOperationCategory]:
        try:
            async with await _connect(self._database_url) as connection:
                rows = await connection.execute(
                    """
                    SELECT operation_id, category, version FROM mes_operation_category
                    ORDER BY operation_id
                    """
                )
                fetched = await rows.fetchall()
        except psycopg.errors.UndefinedTable:
            return []
        return [
            MesOperationCategory(
                operation_id=str(row["operation_id"]),
                category=str(row["category"]),
                version=_opt_str(row.get("version")),
            )
            for row in fetched
        ]

    async def list_tenant_registry(
        self, limit: int, offset: int
    ) -> tuple[list[TenantRegistryRecord], int]:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT * FROM tenant_registry ORDER BY created_at ASC LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            fetched = await rows.fetchall()
            total_row = await connection.execute("SELECT COUNT(*) AS total FROM tenant_registry")
            total_row = await total_row.fetchone()
        records = [_tenant_registry_from_row(row) for row in fetched]
        return records, int(total_row["total"]) if total_row else 0

    async def list_all_tenant_registry(self) -> list[TenantRegistryRecord]:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute("SELECT * FROM tenant_registry ORDER BY app_key ASC")
            fetched = await rows.fetchall()
        return [_tenant_registry_from_row(row) for row in fetched]

    async def get_tenant_registry(self, app_key: str) -> TenantRegistryRecord | None:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                "SELECT * FROM tenant_registry WHERE app_key = %s", (app_key,)
            )
            row = await rows.fetchone()
        if row is None:
            return None
        return _tenant_registry_from_row(row)

    async def create_tenant_registry(self, record: TenantRegistryRecord) -> bool:
        async with await _connect(self._database_url) as connection:
            try:
                await connection.execute(
                    """
                    INSERT INTO tenant_registry
                        (app_key, tenant_name, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        record.app_key,
                        record.tenant_name,
                        record.status,
                        record.created_at,
                        record.updated_at,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                return False
        return True

    async def update_tenant_registry(
        self,
        app_key: str,
        *,
        tenant_name: str | None,
        status: str | None,
        updated_at: datetime,
    ) -> TenantRegistryRecord | None:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                UPDATE tenant_registry
                SET tenant_name = COALESCE(%s, tenant_name),
                    status = COALESCE(%s, status),
                    updated_at = %s
                WHERE app_key = %s
                RETURNING *
                """,
                (tenant_name, status, updated_at, app_key),
            )
            row = await rows.fetchone()
        if row is None:
            return None
        return _tenant_registry_from_row(row)

    async def search_tenant_registry_names(self, fragment: str) -> list[str]:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                """
                SELECT app_key FROM tenant_registry
                WHERE tenant_name ILIKE %s
                ORDER BY app_key ASC
                """,
                (f"%{fragment}%",),
            )
            fetched = await rows.fetchall()
        return [str(row["app_key"]) for row in fetched]

    async def create_principal(self, record: PlatformPrincipalRecord) -> bool:
        async with await _connect(self._database_url) as connection:
            try:
                await connection.execute(
                    """
                    INSERT INTO platform_principal
                        (principal_id, username, password_hash, role, tenant_scope, status,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.principal_id,
                        record.username,
                        record.password_hash,
                        record.role,
                        json.dumps(list(record.tenant_scope)),
                        record.status,
                        record.created_at,
                        record.updated_at,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                return False
        return True

    async def get_principal(self, principal_id: str) -> PlatformPrincipalRecord | None:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                "SELECT * FROM platform_principal WHERE principal_id = %s", (principal_id,)
            )
            row = await rows.fetchone()
        if row is None:
            return None
        return _principal_from_row(row)

    async def get_principal_by_username(self, username: str) -> PlatformPrincipalRecord | None:
        async with await _connect(self._database_url) as connection:
            rows = await connection.execute(
                "SELECT * FROM platform_principal WHERE username = %s", (username,)
            )
            row = await rows.fetchone()
        if row is None:
            return None
        return _principal_from_row(row)


#: Duration metric name -> fact/column attribute (e2e lives in ``duration_ms``).
_DURATION_COLUMN: dict[str, str] = {
    "e2e_duration_ms": "duration_ms",
    "mes_duration_ms": "mes_duration_ms",
    "llm_duration_ms": "llm_duration_ms",
    "local_duration_ms": "local_duration_ms",
}


def _interaction_fact_from_row(row: dict[str, Any]) -> InteractionFact:
    return InteractionFact(
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        interaction_id=str(row["interaction_id"]),
        event_type=str(row["event_type"]),
        user_subject_id=str(row["user_subject_id"]),
        occurred_at=row["occurred_at"],
        capability_id=_opt_str(row.get("capability_id")),
        entrypoint=_opt_str(row.get("entrypoint")),
        role_category=_opt_str(row.get("role_category")),
        status=_opt_str(row.get("status")),
        duration_ms=_opt_int(row.get("duration_ms")),
        mes_duration_ms=_opt_int(row.get("mes_duration_ms")),
        llm_duration_ms=_opt_int(row.get("llm_duration_ms")),
        local_duration_ms=_opt_int(row.get("local_duration_ms")),
        result_rows_bucket=_opt_str(row.get("result_rows_bucket")),
        error_category=_opt_str(row.get("error_category")),
        received_at=row["received_at"],
    )


def _llm_call_fact_from_row(row: dict[str, Any]) -> LlmCallFact:
    return LlmCallFact(
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        interaction_id=str(row["interaction_id"]),
        occurred_at=row["occurred_at"],
        logical_call_id=str(row["logical_call_id"]),
        stage=str(row["stage"]),
        model_alias=str(row["model_alias"]),
        actual_model=str(row["actual_model"]),
        attempt=int(row["attempt"]),
        prompt_tokens=int(row["prompt_tokens"]),
        completion_tokens=int(row["completion_tokens"]),
        cached_tokens=int(row["cached_tokens"]),
        reasoning_tokens=int(row["reasoning_tokens"]),
        duration_ms=int(row["duration_ms"]),
        status=str(row["status"]),
        fallback_reason=_opt_str(row.get("fallback_reason")),
        error_category=_opt_str(row.get("error_category")),
        received_at=row["received_at"],
    )


def _export_from_row(row: dict[str, Any]) -> ExportRecord:
    tenant_filter = row["tenant_filter"]
    if isinstance(tenant_filter, str):
        tenant_filter = json.loads(tenant_filter)
    return ExportRecord(
        export_id=str(row["export_id"]),
        principal_id=str(row["principal_id"]),
        format=str(row["format"]),
        tenant_filter=dict(tenant_filter),
        metric_version=str(row["metric_version"]),
        status=str(row["status"]),
        artifact_key=_opt_str(row.get("artifact_key")),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _tenant_registry_from_row(row: dict[str, Any]) -> TenantRegistryRecord:
    return TenantRegistryRecord(
        app_key=str(row["app_key"]),
        tenant_name=str(row["tenant_name"]),
        status=str(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _principal_from_row(row: dict[str, Any]) -> PlatformPrincipalRecord:
    tenant_scope = row["tenant_scope"]
    if isinstance(tenant_scope, str):
        tenant_scope = json.loads(tenant_scope)
    return PlatformPrincipalRecord(
        principal_id=str(row["principal_id"]),
        username=str(row["username"]),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        tenant_scope=tuple(str(item) for item in tenant_scope),
        status=str(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _mes_call_fact_from_row(row: dict[str, Any]) -> MesCallFact:
    return MesCallFact(
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        interaction_id=str(row["interaction_id"]),
        occurred_at=row["occurred_at"],
        operation_id=str(row["operation_id"]),
        page_count=int(row["page_count"]),
        row_count_bucket=_opt_str(row.get("row_count_bucket")),
        duration_ms=int(row["duration_ms"]),
        status=str(row["status"]),
        error_category=_opt_str(row.get("error_category")),
        received_at=row["received_at"],
    )


def _opt_str(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _opt_int(value: object | None) -> int | None:
    return value if isinstance(value, int) else None


def _opt_float(value: object | None) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def hour_bucket(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = p / 100.0 * (len(sorted_values) - 1)
    lower = int(rank)
    upper = lower + 1
    fraction = rank - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


__all__ = [
    "AuditEntry",
    "ExportRecord",
    "InMemoryUsageStore",
    "MesOperationCategory",
    "PlatformPrincipalRecord",
    "PostgresUsageStore",
    "RollupRow",
    "TenantRegistryRecord",
    "UsageStore",
    "hour_bucket",
    "percentile",
]
