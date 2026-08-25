"""Session and streaming endpoints.

Identity never comes from the request body. Until the customer login contract is
confirmed (CQ-01/CQ-02) the deployment must place a trusted gateway in front of
this service that sets the configured tenant and user headers; the application
treats those headers as the only credential source.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from factory_agent.api.sse import encode_event, parse_last_event_id
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.application.session import (
    InteractionNotFoundError,
    SessionService,
    StartRequest,
)
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import InteractionId, SessionId, TenantId, UserId
from factory_agent.ports import InteractionOwner, TrustedCredential

TENANT_HEADER = "X-Factory-Tenant-Id"
USER_HEADER = "X-Factory-User-Id"

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

session_router = APIRouter(prefix="/v1", tags=["sessions"])


class StartInteractionRequest(BaseModel):
    """Request body deliberately has no tenant, user, or scope field."""

    text: str = Field(min_length=1, max_length=4000)


class InteractionView(BaseModel):
    interaction_id: str
    session_id: str
    status: str
    state: str


class MessageView(BaseModel):
    message_id: str
    role: str
    kind: str
    sequence: int
    text: str


class MessagePageView(BaseModel):
    items: list[MessageView]
    next_cursor: str | None = None


def _container(request: Request) -> ApplicationContainer:
    return cast(ApplicationContainer, request.app.state.container)


def _service(request: Request) -> SessionService:
    service = _container(request).sessions_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session service is not configured",
        )
    return service


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


def _not_found() -> HTTPException:
    """Unauthorized access is indistinguishable from a missing interaction."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="interaction not found")


@session_router.post(
    "/sessions/{session_id}/interactions",
    response_model=InteractionView,
    status_code=status.HTTP_201_CREATED,
)
async def start_interaction(
    session_id: str,
    body: StartInteractionRequest,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
    x_factory_user_id: str | None = Header(default=None, alias=USER_HEADER),
) -> InteractionView:
    service = _service(request)
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    try:
        record = await service.start(
            credential, StartRequest(session_id=SessionId(session_id), text=body.text)
        )
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="interaction text is not acceptable"
        ) from exc
    return InteractionView(
        interaction_id=str(record.interaction_id),
        session_id=str(record.session_id),
        status=record.status.value,
        state=record.state.value,
    )


@session_router.get("/interactions/{interaction_id}/stream")
async def stream_interaction(
    interaction_id: str,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
    x_factory_user_id: str | None = Header(default=None, alias=USER_HEADER),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    service = _service(request)
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    after_sequence = parse_last_event_id(last_event_id)

    async def body() -> AsyncIterator[str]:
        try:
            async for event in service.stream(
                credential, InteractionId(interaction_id), after_sequence=after_sequence
            ):
                yield encode_event(event)
        except (InteractionNotFoundError, IdentityRejectionError):
            return

    return StreamingResponse(body(), media_type="text/event-stream", headers=_SSE_HEADERS)


@session_router.post("/interactions/{interaction_id}/cancel", response_model=InteractionView)
async def cancel_interaction(
    interaction_id: str,
    request: Request,
    x_factory_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
    x_factory_user_id: str | None = Header(default=None, alias=USER_HEADER),
) -> InteractionView:
    service = _service(request)
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    try:
        record = await service.cancel(credential, InteractionId(interaction_id))
    except (InteractionNotFoundError, IdentityRejectionError) as exc:
        raise _not_found() from exc
    return InteractionView(
        interaction_id=str(record.interaction_id),
        session_id=str(record.session_id),
        status=record.status.value,
        state=record.state.value,
    )


@session_router.get("/sessions/{session_id}/messages", response_model=MessagePageView)
async def list_messages(
    session_id: str,
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
    x_factory_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
    x_factory_user_id: str | None = Header(default=None, alias=USER_HEADER),
) -> MessagePageView:
    container = _container(request)
    store = container.interactions
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session store is not configured",
        )
    credential = _credential(x_factory_tenant_id, x_factory_user_id)
    try:
        authorization = await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc
    owner = InteractionOwner(
        tenant_id=authorization.tenant_context.tenant_id,
        user_id=authorization.tenant_context.user_id,
    )
    page = await store.list_messages(owner, SessionId(session_id), min(max(1, limit), 200), cursor)
    return MessagePageView(
        items=[
            MessageView(
                message_id=str(message.message_id),
                role=message.role.value,
                kind=message.kind.value,
                sequence=message.sequence,
                text=message.text,
            )
            for message in page.items
        ],
        next_cursor=page.next_cursor,
    )


__all__ = ["TENANT_HEADER", "USER_HEADER", "session_router"]
