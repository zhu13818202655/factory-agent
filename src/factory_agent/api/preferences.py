"""Push preferences and morning-report endpoints (Story 3B).

The daily morning report is default-on and not configurable; the monthly/weekly
push preferences are user-scoped and their content items are filtered by the
caller's role data range (推送项按角色数据范围展示).
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from factory_agent.api.identity import resolve_credential
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.application.preferences import PreferenceValidationError
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.ports.push_preferences import PushPreferences

preferences_router = APIRouter(prefix="/v1/push", tags=["push"])


class PreferencesView(BaseModel):
    morning_report_enabled: bool = True
    weekly_enabled: bool = False
    weekly_day_of_week: int | None = None
    weekly_time: str | None = None
    monthly_enabled: bool = False
    monthly_day_of_month: int | None = None
    monthly_time: str | None = None
    content_items: list[str] = Field(default_factory=list)


class UpdatePreferencesRequest(BaseModel):
    weekly_enabled: bool | None = None
    weekly_day_of_week: int | None = None
    weekly_time: str | None = None
    monthly_enabled: bool | None = None
    monthly_day_of_month: int | None = None
    monthly_time: str | None = None
    content_items: list[str] | None = None


class ContentItemView(BaseModel):
    item_id: str
    title: str
    capability_id: str


class OptionsView(BaseModel):
    items: list[ContentItemView]


class SectionView(BaseModel):
    capability_id: str
    title: str
    row_count: int
    lines: list[str]


class MorningReportView(BaseModel):
    role: str
    date_label: str
    sections: list[SectionView]
    body: str
    delivered: bool = False


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


def _preferences_service(request: Request):
    container = _container(request)
    if container.preferences_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="push preferences are not configured",
        )
    return container.preferences_service


async def _authorize(request: Request):
    container = _container(request)
    credential, _ = await resolve_credential(request)
    try:
        return await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc


def _to_view(prefs: PushPreferences) -> PreferencesView:
    return PreferencesView(
        morning_report_enabled=True,
        weekly_enabled=prefs.weekly_enabled,
        weekly_day_of_week=prefs.weekly_day_of_week,
        weekly_time=prefs.weekly_time,
        monthly_enabled=prefs.monthly_enabled,
        monthly_day_of_month=prefs.monthly_day_of_month,
        monthly_time=prefs.monthly_time,
        content_items=list(prefs.content_items),
    )


@preferences_router.get("/preferences", response_model=PreferencesView)
async def get_preferences(request: Request) -> PreferencesView:
    service = _preferences_service(request)
    authorization = await _authorize(request)
    context = authorization.tenant_context
    prefs = await service.get(context.tenant_id, context.user_id)
    return _to_view(prefs)


@preferences_router.put("/preferences", response_model=PreferencesView)
async def update_preferences(
    payload: UpdatePreferencesRequest,
    request: Request,
) -> PreferencesView:
    service = _preferences_service(request)
    authorization = await _authorize(request)
    context = authorization.tenant_context
    try:
        prefs = await service.update(
            context.tenant_id,
            context.user_id,
            context.role,
            weekly_enabled=payload.weekly_enabled,
            weekly_day_of_week=payload.weekly_day_of_week,
            weekly_time=payload.weekly_time,
            monthly_enabled=payload.monthly_enabled,
            monthly_day_of_month=payload.monthly_day_of_month,
            monthly_time=payload.monthly_time,
            content_items=tuple(payload.content_items)
            if payload.content_items is not None
            else None,
        )
    except PreferenceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _to_view(prefs)


@preferences_router.get("/preferences/options", response_model=OptionsView)
async def list_options(request: Request) -> OptionsView:
    service = _preferences_service(request)
    authorization = await _authorize(request)
    raw = service.options_for_role(authorization.tenant_context.role)
    return OptionsView(
        items=[
            ContentItemView(
                item_id=item["item_id"],
                title=item["title"],
                capability_id=item["capability_id"],
            )
            for item in raw
        ]
    )


@preferences_router.get("/morning-report", response_model=MorningReportView)
async def morning_report(request: Request) -> MorningReportView:
    """昨日产量/工资摘要 (按角色数据范围), pull / on-demand 形态.

    Unattended 08:00 push needs the customer-confirmed task-credential
    mechanism (open decision); this endpoint generates with the caller's live
    credential and records delivery through the local push channel.
    """
    container = _container(request)
    if container.reporting is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="morning report is not configured",
        )
    credential, _ = await resolve_credential(request)
    try:
        report = await container.reporting.generate_morning_report(credential)
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="morning report is not configured",
        )
    return MorningReportView(
        role=report.role.value,
        date_label=report.date_label,
        sections=[
            SectionView(
                capability_id=section.capability_id,
                title=section.title,
                row_count=section.row_count,
                lines=list(section.lines),
            )
            for section in report.sections
        ],
        body=report.body,
        delivered=True,
    )


__all__ = ["preferences_router"]
