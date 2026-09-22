"""Session and streaming endpoints.

Identity never comes from the request body. The caller presents the encrypted
``app_key`` in the configured credential header; the token gateway exchanges it
at ``/api/system/token`` and yields the authoritative role and bound
departments (customer contract §2). See ``factory_agent.api.identity`` for the
degraded header fallback used only when no gateway is configured.

The conversation endpoints (list / create / detail) resolve ownership the same
way and never accept a tenant, user, or scope field: a conversation belongs to
the ``(tenant_id, user_id)`` pair the credential resolves to.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Self, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from factory_agent.api.identity import (
    TENANT_HEADER,
    USER_HEADER,
    require_tenant_enabled,
    resolve_credential,
)
from factory_agent.api.sse import encode_event, parse_last_event_id
from factory_agent.application.authorization import IdentityRejectionError
from factory_agent.application.session import (
    DrillPayload,
    InteractionNotFoundError,
    SessionService,
    StartRequest,
)
from factory_agent.bootstrap import ApplicationContainer
from factory_agent.domain import (
    InteractionId,
    InteractionRecord,
    MessageKind,
    MessageRecord,
    SessionId,
)
from factory_agent.ports import (
    ConversationSummary,
    InteractionOwner,
    InteractionStore,
    MessagePage,
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

#: Conversation title budget, counted in Unicode code points so a truncation can
#: never split a Chinese character in half.
_TITLE_MAX_CHARS = 30
#: Per-user conversation cap. A guard against an unbounded row count from a
#: client that re-creates a conversation on every click, not a real product
#: limit: 500 is far above any plausible history a worker accumulates.
_MAX_CONVERSATIONS_PER_USER = 500
#: Turn pages collected for one conversation detail. Turns are never paginated
#: in the response (a session holds a handful of them), but the walk is bounded
#: so a pathological session cannot spin the request forever.
_MAX_TURN_PAGES = 50
_TURN_PAGE_SIZE = 200

session_router = APIRouter(
    prefix="/v1", tags=["sessions"], dependencies=[Depends(require_tenant_enabled)]
)


class StartInteractionRequest(BaseModel):
    """Request body deliberately has no tenant, user, or scope field."""

    text: str = Field(min_length=1, max_length=4000)
    # Optional structured drill-down (D-3 拍板): a card action's follow-up
    # query. Business narrowing slots only; the server re-validates them
    # against the active DataScope before any business call.
    drill: "DrillSpec | None" = None


class DrillSpec(BaseModel):
    """Structured drill payload: target capability + business slots."""

    model_config = {"extra": "forbid"}

    capability_id: str = Field(min_length=1, max_length=128)
    dept_ids: list[str] = Field(default_factory=list, max_length=20)
    employee_uid: str | None = Field(default=None, max_length=64)
    time_expression: str | None = Field(default=None, max_length=64)
    #: Absolute window echoed from ``card.time_range`` (D-7 拍板). Both halves
    #: are required together: a half-supplied window is a client bug and is
    #: rejected instead of being silently completed with a guessed boundary.
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None

    @model_validator(mode="after")
    def _window_is_complete(self) -> Self:
        start_set = self.time_range_start is not None
        end_set = self.time_range_end is not None
        if start_set != end_set:
            raise ValueError("time_range_start and time_range_end must be sent together")
        return self

    def to_payload(self) -> DrillPayload:
        return DrillPayload(
            capability_id=self.capability_id,
            dept_ids=tuple(self.dept_ids),
            employee_uid=self.employee_uid,
            time_expression=self.time_expression,
            time_range_start=self.time_range_start,
            time_range_end=self.time_range_end,
        )


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
    #: The turn this message belongs to. Without it a client can only
    #: concatenate messages linearly and cannot recover each turn's terminal
    #: status, which is what renders a failed or cancelled turn.
    interaction_id: str | None = None
    created_at: str | None = None
    # Result-card metadata for kind=result_table (columns/row_count/artifact_id/
    # incomplete...), so clients can re-render the card after a page reload.
    # Absent for every other kind; omitted when the stored message has none.
    payload: dict[str, object] | None = None


class MessagePageView(BaseModel):
    items: list[MessageView]
    next_cursor: str | None = None


class ConversationMessagePreviewView(BaseModel):
    """The newest readable message, used as the list's one-line preview."""

    role: str
    kind: str
    text: str
    created_at: str


class ConversationSummaryView(BaseModel):
    session_id: str
    #: Derived from the conversation's earliest user question; ``null`` until
    #: the first question exists. Never user-editable.
    title: str | None = None
    created_at: str
    updated_at: str
    message_count: int
    interaction_count: int
    last_message: ConversationMessagePreviewView | None = None
    last_status: str | None = None


class ConversationPageView(BaseModel):
    items: list[ConversationSummaryView]
    next_cursor: str | None = None


class ConversationTurnView(BaseModel):
    """One question-and-answer turn inside a conversation."""

    interaction_id: str
    status: str
    state: str
    capability_id: str | None = None
    created_at: str
    completed_at: str | None = None
    error_category: str | None = None


class ConversationDetailView(BaseModel):
    conversation: ConversationSummaryView
    interactions: list[ConversationTurnView]
    messages: list[MessageView]
    next_cursor: str | None = None


class CreateConversationRequest(BaseModel):
    """Optional client-supplied identifier; nothing else is accepted."""

    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")


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


def _store(request: Request) -> InteractionStore:
    store = _container(request).interactions
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session store is not configured",
        )
    return store


async def _owner(request: Request) -> InteractionOwner:
    """The trusted ownership pair for this request, from the credential only."""
    container = _container(request)
    credential, _ = await resolve_credential(request)
    try:
        authorization = await container.authorization.authorize(credential, container.clock.now())
    except IdentityRejectionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.code.value) from exc
    context = authorization.tenant_context
    return InteractionOwner(tenant_id=context.tenant_id, user_id=context.user_id)


def _conversation_not_found() -> HTTPException:
    """A missing conversation and one owned by another identity are the same."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")


def _not_found() -> HTTPException:
    """Unauthorized access is indistinguishable from a missing interaction."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="interaction not found")


def _title(source: str | None) -> str | None:
    """Derive a conversation title without touching a model.

    Titles are cosmetic metadata, and the project's red line is that sensitive
    business fields never reach an LLM prompt — so the title is the caller's own
    first question, collapsed and truncated. The text is already persisted
    verbatim as that user's own ``plain_text`` message, so deriving it exposes
    nothing new.
    """
    if source is None:
        return None
    collapsed = " ".join(source.split())
    if not collapsed:
        return None
    if len(collapsed) <= _TITLE_MAX_CHARS:
        return collapsed
    return f"{collapsed[:_TITLE_MAX_CHARS]}…"


def _message_view(message: MessageRecord) -> MessageView:
    return MessageView(
        message_id=str(message.message_id),
        role=message.role.value,
        kind=message.kind.value,
        sequence=message.sequence,
        text=message.text,
        interaction_id=str(message.interaction_id),
        created_at=message.created_at.isoformat(),
        payload=message.payload or None,
    )


def _turn_view(record: InteractionRecord) -> ConversationTurnView:
    capability = record.capability_id
    return ConversationTurnView(
        interaction_id=str(record.interaction_id),
        status=record.status.value,
        state=record.state.value,
        capability_id=str(capability) if capability is not None else None,
        created_at=record.created_at.isoformat(),
        completed_at=record.completed_at.isoformat() if record.completed_at is not None else None,
        error_category=record.error_category,
    )


def _summary_view(summary: ConversationSummary) -> ConversationSummaryView:
    record = summary.conversation
    preview = summary.last_message
    return ConversationSummaryView(
        session_id=str(record.session_id),
        title=_title(summary.title_source),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        message_count=summary.message_count,
        interaction_count=summary.interaction_count,
        last_message=(
            ConversationMessagePreviewView(
                role=preview.role.value,
                kind=preview.kind.value,
                # The preview is a one-line list affordance; the full text is
                # always available from the conversation detail.
                text=preview.text if len(preview.text) <= 200 else f"{preview.text[:200]}…",
                created_at=preview.created_at.isoformat(),
            )
            if preview is not None
            else None
        ),
        last_status=summary.last_status.value if summary.last_status is not None else None,
    )


def _parse_exclude_kinds(raw: str | None) -> frozenset[MessageKind]:
    """Turn a comma-separated exclusion list into a validated kind set.

    An unknown kind is a client bug and is rejected rather than ignored: a typo
    would otherwise silently return the rows the caller asked to hide.
    """
    if raw is None:
        return frozenset()
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    known = {kind.value for kind in MessageKind}
    unknown = sorted(requested - known)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown message kind: {', '.join(unknown)}",
        )
    return frozenset(MessageKind(value) for value in requested)


async def _conversation_turns(
    store: InteractionStore, owner: InteractionOwner, session_id: SessionId
) -> list[InteractionRecord]:
    """Every turn of one conversation, oldest first.

    The detail response carries turns unpaginated (a session holds a handful),
    so this walks the paginated store read to completion under a hard bound.
    """
    turns: list[InteractionRecord] = []
    cursor: str | None = None
    for _ in range(_MAX_TURN_PAGES):
        page = await store.list_interactions(owner, session_id, _TURN_PAGE_SIZE, cursor)
        turns.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    return turns


async def _message_page(
    store: InteractionStore,
    owner: InteractionOwner,
    session_id: SessionId,
    *,
    limit: int,
    cursor: str | None,
    exclude_kinds: frozenset[MessageKind] = frozenset(),
) -> MessagePage:
    """One message page; a malformed or outdated cursor is a client error.

    Cursors are opaque server-signed strings: a client can only replay what it
    was given, so an unreadable cursor can never be repaired client-side — the
    correct response is a retryable ``400`` that tells the caller to re-read
    from the first page.
    """
    try:
        return await store.list_messages(owner, session_id, limit, cursor, exclude_kinds)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pagination cursor is malformed",
        ) from exc


async def _conversation_detail(
    store: InteractionStore,
    owner: InteractionOwner,
    session_id: SessionId,
    *,
    limit: int,
    cursor: str | None,
    exclude_kinds: frozenset[MessageKind],
) -> ConversationDetailView:
    summary = await store.get_conversation(owner, session_id)
    if summary is None:
        raise _conversation_not_found()
    messages = await _message_page(
        store, owner, session_id, limit=limit, cursor=cursor, exclude_kinds=exclude_kinds
    )
    turns = await _conversation_turns(store, owner, session_id)
    return ConversationDetailView(
        conversation=_summary_view(summary),
        interactions=[_turn_view(turn) for turn in turns],
        messages=[_message_view(message) for message in messages.items],
        next_cursor=messages.next_cursor,
    )


def _clamp(value: int, lower: int, upper: int) -> int:
    """Server-side clamp: the client's limit is a preference, never a bound."""
    return min(max(value, lower), upper)


def _new_session_id() -> str:
    """Server-generated conversation identifier (the client may supply its own)."""
    return f"sess_{uuid4().hex}"


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
            credential,
            StartRequest(
                session_id=SessionId(session_id),
                text=body.text,
                drill=body.drill.to_payload() if body.drill is not None else None,
            ),
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


@session_router.get(
    "/sessions/{session_id}/messages",
    response_model=MessagePageView,
    response_model_exclude_none=True,
)
async def list_messages(
    session_id: str,
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
) -> MessagePageView:
    store = _store(request)
    owner = await _owner(request)
    page = await _message_page(
        store, owner, SessionId(session_id), limit=_clamp(limit, 1, 200), cursor=cursor
    )
    return MessagePageView(
        items=[_message_view(message) for message in page.items],
        next_cursor=page.next_cursor,
    )


@session_router.get("/conversations", response_model=ConversationPageView)
async def list_conversations(
    request: Request,
    limit: int = Query(default=20),
    cursor: str | None = Query(default=None),
) -> ConversationPageView:
    """One recency-ordered page of the caller's own conversations.

    Ownership comes from the credential alone; there is deliberately no
    parameter for another user's conversations, and none for a role filter
    (a conversation belongs to a person, not to a role).
    """
    store = _store(request)
    owner = await _owner(request)
    page = await store.list_conversations(owner, _clamp(limit, 1, 100), cursor)
    return ConversationPageView(
        items=[_summary_view(summary) for summary in page.items],
        next_cursor=page.next_cursor,
    )


@session_router.post(
    "/conversations",
    response_model=ConversationDetailView,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    request: Request,
    response: Response,
    body: CreateConversationRequest | None = None,
) -> ConversationDetailView:
    """Create a conversation, or replay an existing one.

    Idempotent on purpose: a repeated click or a retried request returns the
    conversation as it actually is (``200``) instead of erroring or, worse,
    starting an empty duplicate that hides the existing history. The response
    already carries the whole conversation flow, so a client needs no follow-up
    read after creating one.
    """
    store = _store(request)
    owner = await _owner(request)
    requested = body.session_id if body is not None else None
    session_id = SessionId(requested) if requested is not None else SessionId(_new_session_id())
    existing = await store.get_conversation(owner, session_id)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return await _conversation_detail(
            store, owner, session_id, limit=50, cursor=None, exclude_kinds=frozenset()
        )
    if await store.count_conversations(owner) >= _MAX_CONVERSATIONS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="conversation limit reached",
        )
    created = await store.create_conversation(owner, session_id, _container(request).clock.now())
    if not created.created:
        # Lost a race with a concurrent create: the winner's conversation is the
        # real one, so report it as an existing conversation.
        response.status_code = status.HTTP_200_OK
    return await _conversation_detail(
        store, owner, session_id, limit=50, cursor=None, exclude_kinds=frozenset()
    )


@session_router.get(
    "/conversations/{session_id}",
    response_model=ConversationDetailView,
)
async def get_conversation(
    session_id: str,
    request: Request,
    limit: int = Query(default=50),
    cursor: str | None = Query(default=None),
    exclude_kinds: str | None = Query(default=None),
) -> ConversationDetailView:
    """The whole conversation flow: metadata, every turn, and its messages.

    Unlike the message-only route, nulls are sent explicitly (``title: null``,
    ``last_message: null``, ``payload: null``) so the response has one stable
    shape a client can type directly.
    """
    store = _store(request)
    owner = await _owner(request)
    return await _conversation_detail(
        store,
        owner,
        SessionId(session_id),
        limit=_clamp(limit, 1, 200),
        cursor=cursor,
        exclude_kinds=_parse_exclude_kinds(exclude_kinds),
    )


__all__ = ["TENANT_HEADER", "USER_HEADER", "session_router"]
