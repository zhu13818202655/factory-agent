"""Session and streaming endpoints.

Identity never comes from the request body. The caller presents the encrypted
``app_key`` in the configured credential header; the token gateway exchanges it
at ``/api/system/token`` and yields the authoritative role and bound
departments (customer contract §2). See ``factory_agent.api.identity`` for the
degraded header fallback used only when no gateway is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from factory_agent.api.identity import TENANT_HEADER, USER_HEADER, resolve_credential
from factory_agent.api.sse import encode_event, parse_last_event_id
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.application.session import (
    InteractionNotFoundError,
    SessionService,
    StartRequest,
)
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import InteractionId, SessionId
from factory_agent.ports import InteractionOwner

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
) -> InteractionView:
    service = _service(request)
    credential, _ = await resolve_credential(request)
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
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    service = _service(request)
    credential, _ = await resolve_credential(request)
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
) -> InteractionView:
    service = _service(request)
    credential, _ = await resolve_credential(request)
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
) -> MessagePageView:
    container = _container(request)
    store = container.interactions
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session store is not configured",
        )
    credential, _ = await resolve_credential(request)
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
