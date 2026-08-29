"""Report exports with short-lived, re-authorized download links.

Export rows are aggregates only; the generation writes a CSV or XLSX artifact
through an ``ExportFileStore``, records the export and a full admin audit
entry, and returns a signed, short-lived download token. Download re-validates
the signature and expiry — it never accepts an unscoped or expired link.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from usage_admin.masking import mask_app_key
from usage_admin.ops import MesCategoriesView, OpsLimits, OpsQueryError, OpsService
from usage_admin.platform import PlatformScope, PlatformScopeError
from usage_admin.store import AuditEntry, ExportRecord, UsageStore

ExportFormat = Literal["csv", "xlsx"]

#: MES billing-category metrics supported by exports (D1/D5).
_MES_EXPORT_METRICS: tuple[str, ...] = (
    "mes_output",
    "mes_payroll",
    "mes_order",
    "mes_other",
)


@dataclass(frozen=True, slots=True)
class ExportTable:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True, slots=True)
class ExportView:
    export_id: str
    format: ExportFormat
    status: str
    download_url: str | None
    expires_at: datetime | None
    created_at: datetime


class ExportFileStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> None: ...


class ExportGenerationError(ValueError):
    """Structured rejection when an export cannot be produced."""


def render_csv(table: ExportTable) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(table.columns))
    for row in table.rows:
        writer.writerow([_cell(value) for value in row])
    return buffer.getvalue().encode()


def render_xlsx(table: ExportTable) -> bytes:
    import xlsxwriter  # type: ignore[reportMissingTypeStubs]

    buffer = io.BytesIO()
    workbook: Any = xlsxwriter.Workbook(buffer, {"in_memory": True})
    worksheet: Any = workbook.add_worksheet("usage")
    for column_index, column in enumerate(table.columns):
        worksheet.write(0, column_index, column)
    for row_index, row in enumerate(table.rows, start=1):
        for column_index, value in enumerate(row):
            worksheet.write(row_index, column_index, _cell(value))
    workbook.close()
    return buffer.getvalue()


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _mes_metric_value(mes: MesCategoriesView | None, metric: str) -> int:
    if mes is None:
        return 0
    return int(mes.categories.get(metric.removeprefix("mes_"), 0))


def sign_download(secret: str, export_id: str, expires_at: datetime) -> str:
    message = f"{export_id}.{int(expires_at.timestamp())}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{export_id}.{int(expires_at.timestamp())}.{digest}"


def verify_download(secret: str, token: str, *, now: datetime) -> str | None:
    """Return the export id when the token is valid and unexpired, else None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    export_id, raw_expires, _digest = parts
    try:
        expires_at = datetime.fromtimestamp(int(raw_expires), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    if expires_at <= now:
        return None
    expected = sign_download(secret, export_id, expires_at)
    if not hmac.compare_digest(expected, token):
        return None
    return export_id


class ExportService:
    """Creates and serves platform report exports."""

    def __init__(
        self,
        store: UsageStore,
        ops: OpsService,
        files: ExportFileStore,
        *,
        clock: Callable[[], datetime],
        new_id: Callable[[], str],
        signing_secret: str,
        download_base_url: str,
        presign_expires_seconds: int = 900,
        max_rows: int = 100_000,
        limits: OpsLimits | None = None,
    ) -> None:
        self._store = store
        self._ops = ops
        self._files = files
        self._clock = clock
        self._new_id = new_id
        self._signing_secret = signing_secret
        self._download_base_url = download_base_url.rstrip("/")
        self._presign_expires_seconds = presign_expires_seconds
        self._max_rows = max_rows
        self._limits = limits or OpsLimits()

    async def create_export(
        self,
        scope: PlatformScope,
        *,
        start: datetime,
        end: datetime,
        format: ExportFormat,
        granularity: str | None = None,
        metrics: tuple[str, ...] = (),
    ) -> ExportView:
        if not scope.allows_export():
            raise PlatformScopeError("exports require the analyst role")
        table = await self._build_table(scope, start, end, granularity, metrics)
        if len(table.rows) > self._max_rows:
            raise ExportGenerationError(
                f"export exceeds the {self._max_rows}-row budget; narrow the range"
            )

        now = self._clock()
        export_id = self._new_id()
        key = f"exports/{export_id}.{format}"
        data = render_xlsx(table) if format == "xlsx" else render_csv(table)
        await self._files.put(key, data)
        expires_at = now + timedelta(seconds=self._presign_expires_seconds)
        masked_tenants = [
            masked for masked in (mask_app_key(t) for t in scope.tenant_ids) if masked
        ]
        await self._store.create_export(
            ExportRecord(
                export_id=export_id,
                principal_id=scope.principal_id,
                format=format,
                tenant_filter={"tenant_ids": sorted(masked_tenants)},
                metric_version=self._ops.metric_version(),
                status="ready",
                artifact_key=key,
                created_at=now,
                expires_at=expires_at,
            )
        )
        await self._audit(
            scope,
            "export.create",
            export_id,
            {
                "format": format,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "granularity": granularity,
                "metrics": list(metrics),
                "rows": len(table.rows),
            },
        )
        return ExportView(
            export_id=export_id,
            format=format,
            status="ready",
            download_url=self._build_url(export_id, expires_at),
            expires_at=expires_at,
            created_at=now,
        )

    async def get_export(self, scope: PlatformScope, export_id: str) -> ExportView:
        record = await self._store.get_export(export_id)
        if record is None or record.principal_id != scope.principal_id:
            raise OpsQueryError("export not found")
        return ExportView(
            export_id=record.export_id,
            format=record.format,  # type: ignore[arg-type]
            status=record.status,
            download_url=self._build_url(record.export_id, record.expires_at),
            expires_at=record.expires_at,
            created_at=record.created_at,
        )

    async def download(self, token: str) -> tuple[bytes, str] | None:
        """Resolve a signed token to file bytes, or ``None`` when invalid."""
        now = self._clock()
        export_id = verify_download(self._signing_secret, token, now=now)
        if export_id is None:
            return None
        record = await self._store.get_export(export_id)
        if record is None or record.artifact_key is None:
            return None
        data = await self._files.get(record.artifact_key)
        if data is None:
            return None
        return data, record.format

    async def _build_table(
        self,
        scope: PlatformScope,
        start: datetime,
        end: datetime,
        granularity: str | None,
        metrics: tuple[str, ...],
    ) -> ExportTable:
        if granularity in ("hour", "day"):
            view = await self._ops.timeseries(scope, start, end, granularity, metrics)
            columns = ("bucket",) + tuple(view.points[0].metrics) if view.points else ("bucket",)
            rows = tuple(
                (point.bucket.isoformat(),) + tuple(point.metrics.get(m, 0.0) for m in columns[1:])
                for point in view.points
            )
            return ExportTable(columns=columns, rows=rows)
        summary = await self._ops.summary(scope, start, end)
        mes_metrics = tuple(metric for metric in metrics if metric in _MES_EXPORT_METRICS)
        mes = await self._ops.mes_categories(scope, start, end) if mes_metrics else None
        columns = (
            "users",
            "questions",
            "valid_questions",
            "completed",
            "failed",
            "cancelled",
            "rejected",
            "llm_logical_calls",
            "llm_physical_attempts",
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "reasoning_tokens",
        ) + mes_metrics
        row = (
            summary.users,
            summary.questions,
            summary.valid_questions,
            summary.status.get("status.completed", 0),
            summary.status.get("status.failed", 0),
            summary.status.get("status.cancelled", 0),
            summary.status.get("status.rejected", 0),
            summary.llm_logical_calls,
            summary.llm_physical_attempts,
            summary.tokens.get("prompt_tokens", 0),
            summary.tokens.get("completion_tokens", 0),
            summary.tokens.get("cached_tokens", 0),
            summary.tokens.get("reasoning_tokens", 0),
        ) + tuple(
            _mes_metric_value(mes, metric) if mes is not None else 0 for metric in mes_metrics
        )
        return ExportTable(columns=columns, rows=(row,))

    def _build_url(self, export_id: str, expires_at: datetime) -> str:
        token = sign_download(self._signing_secret, export_id, expires_at)
        return f"{self._download_base_url}/admin/v1/exports/{export_id}/download?token={token}"

    async def _audit(
        self,
        scope: PlatformScope,
        action: str,
        target: str | None,
        detail: dict[str, object],
    ) -> None:
        await self._store.record_audit(
            AuditEntry(
                audit_id=self._new_id(),
                principal_id=scope.principal_id,
                action=action,
                target=target,
                detail=detail,
                created_at=self._clock(),
            )
        )


__all__ = [
    "ExportFileStore",
    "ExportFormat",
    "ExportGenerationError",
    "ExportService",
    "ExportTable",
    "ExportView",
    "render_csv",
    "render_xlsx",
    "sign_download",
    "verify_download",
]
