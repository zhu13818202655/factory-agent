"""Quick questions, history, favorites, and minimal user-mapping endpoints.

Identity never comes from the request body; the trusted gateway headers are the
only credential source (same as the session endpoints). History and favorites
are ownership-filtered by the trusted ``(tenant_id, user_id)`` pair.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from factory_agent.application.personal import (
    FavoriteNotFoundError,
    PersonalizationService,
)
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import CapabilityId, TenantId, UserId
from factory_agent.ports import InteractionOwner, TrustedCredential
from factory_agent.ports.personal import Favorite

personal_router = APIRouter(prefix="/v1", tags=["personal"])

TENANT_HEADER = "X-Factory-Tenant-Id"
USER_HEADER = "X-Factory-User-Id"


def _credential(tenant_id: str | None, user_id: str | None) -> TrustedCredential:
    if not tenant_id or not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted identity headers are missing"
        )
    try:
        return TrustedCredential(tenant_id=TenantId(tenant_id), user_id=UserId(user_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="trusted identity headers are invalid"
        ) from exc


def _owner(credential: TrustedCredential) -> InteractionOwner:
    return InteractionOwner(tenant_id=credential.tenant_id, user_id=credential.user_id)


class QuickQuestionView(BaseModel):
    id: str
    capability_id: str
    text: str
    slots: dict[str, object]


class HistoryEntryView(BaseModel):
    history_id: str
    capability_id: str
    intent: dict[str, object]
    status: str
    created_at: str


class HistoryPageView(BaseModel):
    items: list[HistoryEntryView]
    next_cursor: str | None = None


class FavoriteCreateRequest(BaseModel):
    capability_id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    #: Non-sensitive slots only; the service strips anything else.
    slots: dict[str, object] = Field(default_factory=dict)


class FavoriteView(BaseModel):
    favorite_id: str
    capability_id: str
    title: str
    slots: dict[str, object]
    created_at: str
    expires_at: str


class UserMappingRequest(BaseModel):
    """Minimal ``uid``/``uname``/``company`` mapping from the trusted frontend."""

    uname: str = Field(min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)


class UserMappingView(BaseModel):
    uid: str
    uname: str
    company: str | None = None


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


def _personal(request: Request) -> PersonalizationService:
    service = _container(request).personalization
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="personalization is not configured",
        )
    return service


def _not_found() -> HTTPException:
    """A missing record and one owned by another user look identical."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


@personal_router.get("/quick-questions", response_model=list[QuickQuestionView])
async def quick_questions(
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> list[QuickQuestionView]:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    questions = _personal(request).quick_questions(credential)
    return [
        QuickQuestionView(
            id=question.id,
            capability_id=question.capability_id,
            text=question.text,
            slots=question.slots,
        )
        for question in questions
    ]


@personal_router.get("/history", response_model=HistoryPageView)
async def list_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> HistoryPageView:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    page = await _personal(request).list_history(_owner(credential), limit, cursor)
    return HistoryPageView(
        items=[
            HistoryEntryView(
                history_id=entry.history_id,
                capability_id=str(entry.capability_id),
                intent=entry.intent,
                status=entry.status,
                created_at=entry.created_at.isoformat(),
            )
            for entry in page.items
        ],
        next_cursor=page.next_cursor,
    )


@personal_router.delete("/history/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_history(
    history_id: str,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> None:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    deleted = await _personal(request).delete_history(_owner(credential), history_id)
    if not deleted:
        raise _not_found()


@personal_router.post(
    "/favorites", response_model=FavoriteView, status_code=status.HTTP_201_CREATED
)
async def create_favorite(
    body: FavoriteCreateRequest,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> FavoriteView:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    now = _container(request).clock.now()
    try:
        favorite = await _personal(request).create_favorite(
            _owner(credential),
            capability_id=CapabilityId(body.capability_id),
            title=body.title,
            slots=body.slots,
            now=now,
        )
    except FavoriteNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _favorite_view(favorite)


@personal_router.get("/favorites", response_model=list[FavoriteView])
async def list_favorites(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> list[FavoriteView]:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    favorites = await _personal(request).list_favorites(_owner(credential), limit)
    return [_favorite_view(favorite) for favorite in favorites]


@personal_router.delete("/favorites/{favorite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    favorite_id: str,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> None:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    deleted = await _personal(request).delete_favorite(_owner(credential), favorite_id)
    if not deleted:
        raise _not_found()


@personal_router.post("/favorites/{favorite_id}/re-ask", response_model=FavoriteView)
async def reask_favorite(
    favorite_id: str,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> FavoriteView:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    try:
        favorite = await _personal(request).reask_favorite(_owner(credential), favorite_id)
    except FavoriteNotFoundError as exc:
        raise _not_found() from exc
    return _favorite_view(favorite)


@personal_router.post("/users/me/mapping", response_model=UserMappingView)
async def save_user_mapping(
    body: UserMappingRequest,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias="X-Factory-Tenant-Id"),
    x_factory_user_id: str | None = Header(default=None, alias="X-Factory-User-Id"),
) -> UserMappingView:
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    now = _container(request).clock.now()
    await _personal(request).save_mapping(
        uid=str(credential.user_id),
        tenant_id=credential.tenant_id,
        uname=body.uname,
        company=body.company,
        now=now,
    )
    return UserMappingView(uid=str(credential.user_id), uname=body.uname, company=body.company)


def _favorite_view(favorite: Favorite) -> FavoriteView:
    return FavoriteView(
        favorite_id=favorite.favorite_id,
        capability_id=str(favorite.capability_id),
        title=favorite.title,
        slots=favorite.slots,
        created_at=favorite.created_at.isoformat(),
        expires_at=favorite.expires_at.isoformat(),
    )


__all__ = ["personal_router"]
