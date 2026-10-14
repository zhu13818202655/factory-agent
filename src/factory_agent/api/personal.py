"""Current identity, quick questions, history, favorites, and user mapping.

Identity never comes from the request body; it is resolved by the token
exchange (see ``factory_agent.api.identity``). History and favorites are
ownership-filtered by the trusted ``(tenant_id, user_id)`` pair. Quick
questions are role-aware: the role is the authoritative token role, and
``GET /v1/me`` exposes that same role (plus its data scope) so the frontend
can render role-aware UI from the credential alone without ever passing a
role of its own.
"""

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from factory_agent.api.identity import require_tenant_enabled, resolve_credential
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.application.personal import (
    FavoriteNotFoundError,
    PersonalizationService,
)
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import CapabilityId
from factory_agent.ports import InteractionOwner, ResolvedPrincipal, TrustedCredential
from factory_agent.ports.personal import Favorite

personal_router = APIRouter(
    prefix="/v1", tags=["personal"], dependencies=[Depends(require_tenant_enabled)]
)


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


class DataScopeView(BaseModel):
    """The caller's authorized range, in locally provable terms.

    ``employee_ids`` is the caller's own work number; ``dept_ids`` is the
    token-returned bound department/workshop set. ``mes_filtered`` records
    that any wider row visibility (99 老板 = whole factory) is performed by
    the customer MES, not claimed here (see ``factory_agent.domain.DataScope``).
    """

    employee_ids: list[str]
    dept_ids: list[str]
    mes_filtered: bool
    scope_version: str


class IdentityView(BaseModel):
    """Who the caller is, derived only from the trusted credential."""

    uid: str
    uname: str
    #: Domain role: ``employee`` / ``group_leader`` / ``manager`` / ``owner``.
    role: str
    #: Customer role code ``00`` / ``01`` / ``02`` / ``99``; the authoritative
    #: token value the frontend already switches on.
    role_code: str
    #: Bound department/workshop set (one element for 00/01, the bound set for
    #: 02; empty only when the token carries no binding).
    dept_ids: list[str]
    data_scope: DataScopeView


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


async def _display_name(
    request: Request,
    credential: TrustedCredential,
    principal: ResolvedPrincipal | None,
) -> str:
    """The token ``uname``, falling back to a saved display mapping.

    The token is the only name source in production; the mapping fallback only
    covers a deployment whose identity is wired without a token gateway.
    """
    if principal is not None:
        return principal.display_name
    service = _container(request).personalization
    if service is None:
        return ""
    mapping = await service.get_mapping(credential.tenant_id, str(credential.user_id))
    return mapping.uname if mapping is not None else ""


@personal_router.get("/me", response_model=IdentityView)
async def current_identity(request: Request) -> IdentityView:
    """The caller's role, name, and authorized range.

    The frontend presents only the encrypted ``app_key``; role, name, and
    departments come from the token exchange and the range from authorization,
    so a caller can never state its own role. This is the endpoint a
    role-aware client calls once at startup — quick questions remain the
    authoritative *capability* list, this is the authoritative *identity*.
    """
    container = _container(request)
    credential, principal = await resolve_credential(request)
    try:
        authorization = await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc
    context = authorization.tenant_context
    scope = authorization.data_scope
    dept_ids = sorted(str(dept) for dept in scope.dept_ids)
    return IdentityView(
        uid=str(context.user_id),
        uname=await _display_name(request, credential, principal),
        role=context.role.value,
        role_code=context.role.mes_code,
        dept_ids=dept_ids,
        data_scope=DataScopeView(
            employee_ids=sorted(str(item) for item in scope.employee_ids),
            dept_ids=dept_ids,
            mes_filtered=scope.mes_filtered,
            scope_version=str(scope.scope_version),
        ),
    )


@personal_router.get("/quick-questions", response_model=list[QuickQuestionView])
async def quick_questions(request: Request) -> list[QuickQuestionView]:
    container = _container(request)
    credential, principal = await resolve_credential(request)
    # The role is authoritative: prefer the token principal, and fall back to
    # resolving authorization (degraded header mode) when no principal exists.
    role = principal.role if principal is not None else None
    if role is None:
        try:
            authorization = await container.authorization.authorize(
                credential, container.clock.now()
            )
            role = authorization.tenant_context.role
        except IdentityRejectionError:
            role = None
    questions = _personal(request).quick_questions(credential, role)
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
) -> HistoryPageView:
    credential, _ = await resolve_credential(request)
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
) -> None:
    credential, _ = await resolve_credential(request)
    deleted = await _personal(request).delete_history(_owner(credential), history_id)
    if not deleted:
        raise _not_found()


@personal_router.post(
    "/favorites", response_model=FavoriteView, status_code=status.HTTP_201_CREATED
)
async def create_favorite(
    body: FavoriteCreateRequest,
    request: Request,
) -> FavoriteView:
    credential, _ = await resolve_credential(request)
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
) -> list[FavoriteView]:
    credential, _ = await resolve_credential(request)
    favorites = await _personal(request).list_favorites(_owner(credential), limit)
    return [_favorite_view(favorite) for favorite in favorites]


@personal_router.delete("/favorites/{favorite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    favorite_id: str,
    request: Request,
) -> None:
    credential, _ = await resolve_credential(request)
    deleted = await _personal(request).delete_favorite(_owner(credential), favorite_id)
    if not deleted:
        raise _not_found()


@personal_router.post("/favorites/{favorite_id}/re-ask", response_model=FavoriteView)
async def reask_favorite(
    favorite_id: str,
    request: Request,
) -> FavoriteView:
    credential, _ = await resolve_credential(request)
    try:
        favorite = await _personal(request).reask_favorite(_owner(credential), favorite_id)
    except FavoriteNotFoundError as exc:
        raise _not_found() from exc
    return _favorite_view(favorite)


@personal_router.post("/users/me/mapping", response_model=UserMappingView)
async def save_user_mapping(
    body: UserMappingRequest,
    request: Request,
) -> UserMappingView:
    credential, _ = await resolve_credential(request)
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
