"""Platform operations API with PlatformScope RBAC and export download."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from usage_admin.container import AdminContainer
from usage_admin.exports import ExportService, ExportView
from usage_admin.ops import OpsQueryError, OpsService
from usage_admin.platform import (
    PRINCIPAL_HEADER,
    ROLE_HEADER,
    TENANT_HEADER,
    PlatformScope,
    PlatformScopeError,
    resolve_platform_scope,
)

admin_router = APIRouter(prefix="/admin/v1", tags=["admin"])


def _parse_datetime(raw: str | None, name: str) -> datetime:
    if raw is None:
        raise HTTPException(status_code=422, detail=f"{name} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} is not a valid datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _scope(request: Request) -> PlatformScope:
    try:
        return resolve_platform_scope(
            request.headers.get(PRINCIPAL_HEADER),
            request.headers.get(ROLE_HEADER),
            request.headers.get(TENANT_HEADER),
        )
    except PlatformScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _container(request: Request) -> AdminContainer:
    return cast(AdminContainer, request.app.state.container)


def _ops(request: Request) -> OpsService:
    return _container(request).ops


def _export(request: Request) -> ExportService:
    return _container(request).exports


class DurationStatsView(BaseModel):
    count: int
    mean_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None


class SummaryView(BaseModel):
    tenant_ids: list[str]
    start: datetime
    end: datetime
    users: int
    questions: int
    valid_questions: int
    status: dict[str, int]
    llm_logical_calls: int
    llm_physical_attempts: int
    tokens: dict[str, int]
    durations: dict[str, DurationStatsView]
    metric_version: str
    timezone: str
    freshness: datetime | None = None
    incomplete: bool


class TimeseriesPointView(BaseModel):
    bucket: datetime
    metrics: dict[str, float]


class TimeseriesView(BaseModel):
    tenant_ids: list[str]
    start: datetime
    end: datetime
    granularity: Literal["hour", "day"]
    points: list[TimeseriesPointView]
    metric_version: str
    timezone: str
    incomplete: bool


class DimensionsView(BaseModel):
    tenant_ids: list[str]
    start: datetime
    end: datetime
    dimension: str
    values: dict[str, float]
    truncated: bool
    metric_version: str
    timezone: str


class UserActivityView(BaseModel):
    user_subject_id: str
    question_count: int


class UsersPageView(BaseModel):
    tenant_ids: list[str]
    start: datetime
    end: datetime
    items: list[UserActivityView]
    total: int
    next_cursor: int | None = None
    metric_version: str
    timezone: str


class ExportCreateRequest(BaseModel):
    start: datetime
    end: datetime
    format: Literal["csv", "xlsx"] = "csv"
    granularity: Literal["hour", "day"] | None = None
    metrics: list[str] = Field(default_factory=list)


class ExportViewOut(BaseModel):
    export_id: str
    format: str
    status: str
    download_url: str | None = None
    expires_at: datetime | None = None
    created_at: datetime


@admin_router.get("/tenants", response_model=list[str])
async def list_tenants(
    request: Request,
    start: str = Query(description="ISO datetime"),
    end: str = Query(description="ISO datetime"),
) -> list[str]:
    scope = _scope(request)
    parsed_start = _parse_datetime(start, "start")
    parsed_end = _parse_datetime(end, "end")
    try:
        return await _ops(request).list_tenants(scope, parsed_start, parsed_end)
    except OpsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@admin_router.get("/usage/summary", response_model=SummaryView)
async def usage_summary(
    request: Request,
    start: str = Query(description="ISO datetime"),
    end: str = Query(description="ISO datetime"),
) -> SummaryView:
    scope = _scope(request)
    parsed_start = _parse_datetime(start, "start")
    parsed_end = _parse_datetime(end, "end")
    try:
        view = await _ops(request).summary(scope, parsed_start, parsed_end)
    except OpsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SummaryView(
        tenant_ids=list(view.tenant_ids),
        start=view.start,
        end=view.end,
        users=view.users,
        questions=view.questions,
        valid_questions=view.valid_questions,
        status=view.status,
        llm_logical_calls=view.llm_logical_calls,
        llm_physical_attempts=view.llm_physical_attempts,
        tokens=view.tokens,
        durations={
            metric: DurationStatsView(
                count=stats.count,
                mean_ms=stats.mean_ms,
                p50_ms=stats.p50_ms,
                p95_ms=stats.p95_ms,
                p99_ms=stats.p99_ms,
            )
            for metric, stats in view.durations.items()
        },
        metric_version=view.metric_version,
        timezone=view.timezone,
        freshness=view.freshness,
        incomplete=view.incomplete,
    )


@admin_router.get("/usage/timeseries", response_model=TimeseriesView)
async def usage_timeseries(
    request: Request,
    start: str = Query(description="ISO datetime"),
    end: str = Query(description="ISO datetime"),
    granularity: Literal["hour", "day"] = "day",
    metrics: str = Query(default="users,questions,valid_questions"),
) -> TimeseriesView:
    scope = _scope(request)
    parsed_start = _parse_datetime(start, "start")
    parsed_end = _parse_datetime(end, "end")
    requested = tuple(part for part in metrics.split(",") if part)
    try:
        view = await _ops(request).timeseries(
            scope, parsed_start, parsed_end, granularity, requested
        )
    except OpsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TimeseriesView(
        tenant_ids=list(view.tenant_ids),
        start=view.start,
        end=view.end,
        granularity=view.granularity,
        points=[
            TimeseriesPointView(bucket=point.bucket, metrics=point.metrics) for point in view.points
        ],
        metric_version=view.metric_version,
        timezone=view.timezone,
        incomplete=view.incomplete,
    )


@admin_router.get("/usage/dimensions", response_model=DimensionsView)
async def usage_dimensions(
    request: Request,
    start: str = Query(description="ISO datetime"),
    end: str = Query(description="ISO datetime"),
    dimension: str = Query(description="capability|status|model_alias|actual_model|..."),
) -> DimensionsView:
    scope = _scope(request)
    parsed_start = _parse_datetime(start, "start")
    parsed_end = _parse_datetime(end, "end")
    try:
        view = await _ops(request).dimensions(scope, parsed_start, parsed_end, dimension)
    except OpsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DimensionsView(
        tenant_ids=list(view.tenant_ids),
        start=view.start,
        end=view.end,
        dimension=view.dimension,
        values=view.values,
        truncated=view.truncated,
        metric_version=view.metric_version,
        timezone=view.timezone,
    )


@admin_router.get("/usage/users", response_model=UsersPageView)
async def usage_users(
    request: Request,
    start: str = Query(description="ISO datetime"),
    end: str = Query(description="ISO datetime"),
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UsersPageView:
    scope = _scope(request)
    parsed_start = _parse_datetime(start, "start")
    parsed_end = _parse_datetime(end, "end")
    try:
        view = await _ops(request).users(scope, parsed_start, parsed_end, limit, offset)
    except OpsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UsersPageView(
        tenant_ids=list(view.tenant_ids),
        start=view.start,
        end=view.end,
        items=[
            UserActivityView(
                user_subject_id=item.user_subject_id,
                question_count=item.question_count,
            )
            for item in view.items
        ],
        total=view.total,
        next_cursor=view.next_cursor,
        metric_version=view.metric_version,
        timezone=view.timezone,
    )


@admin_router.post("/exports", response_model=ExportViewOut, status_code=201)
async def create_export(request: Request, body: ExportCreateRequest) -> ExportViewOut:
    scope = _scope(request)
    try:
        view = await _export(request).create_export(
            scope,
            start=body.start,
            end=body.end,
            format=body.format,
            granularity=body.granularity,
            metrics=tuple(body.metrics),
        )
    except (PlatformScopeError, OpsQueryError) as exc:
        status = 403 if isinstance(exc, PlatformScopeError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _to_view(view)


@admin_router.get("/exports/{export_id}", response_model=ExportViewOut)
async def get_export(request: Request, export_id: str) -> ExportViewOut:
    scope = _scope(request)
    try:
        view = await _export(request).get_export(scope, export_id)
    except OpsQueryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_view(view)


@admin_router.get("/exports/{export_id}/download")
async def download_export(
    request: Request,
    export_id: str,
    token: str = Query(description="signed short-lived download token"),
) -> Response:
    result = await _export(request).download(token)
    if result is None:
        raise HTTPException(status_code=403, detail="download link is invalid or expired")
    data, format = result
    media_type = (
        "text/csv"
        if format == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    filename = f"usage-{export_id}.{format}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _to_view(view: ExportView) -> ExportViewOut:
    return ExportViewOut(
        export_id=view.export_id,
        format=view.format,
        status=view.status,
        download_url=view.download_url,
        expires_at=view.expires_at,
        created_at=view.created_at,
    )
