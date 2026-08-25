"""Session, interaction, and message domain values with a pure state machine.

Factory semantics replace the report-agent flight vocabulary: ``FETCHING`` became
``EXECUTING``, ``ANALYZING`` became ``COMPOSING``, ``PREVIEWING`` became
``ANSWERED``, and report rendering is not part of this state machine. The tested
source behaviours that are preserved unchanged are: ``FAILED`` is reachable from
every non-terminal state, terminal states can never restart, and every applied
transition records ``from``, ``to``, and ``reason``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self

from factory_agent.domain.identifiers import InteractionId, TenantId
from factory_agent.domain.identity import CapabilityId, NonEmptyId, UserId


class SessionId(NonEmptyId):
    """Identifier of one multi-turn conversation inside a tenant."""


class MessageId(NonEmptyId):
    """Identifier of one persisted conversation message."""


class SessionState(StrEnum):
    """Lifecycle of a factory-agent conversation."""

    PARSING = "parsing"
    CLARIFYING = "clarifying"
    AUTHORIZING = "authorizing"
    EXECUTING = "executing"
    COMPOSING = "composing"
    ANSWERED = "answered"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.PARSING: frozenset(
        {
            SessionState.CLARIFYING,
            SessionState.AUTHORIZING,
            SessionState.CANCELLED,
            SessionState.FAILED,
        }
    ),
    SessionState.CLARIFYING: frozenset(
        {
            SessionState.PARSING,
            SessionState.AUTHORIZING,
            SessionState.CANCELLED,
            SessionState.FAILED,
        }
    ),
    SessionState.AUTHORIZING: frozenset(
        {SessionState.EXECUTING, SessionState.CANCELLED, SessionState.FAILED}
    ),
    SessionState.EXECUTING: frozenset(
        {SessionState.COMPOSING, SessionState.CANCELLED, SessionState.FAILED}
    ),
    SessionState.COMPOSING: frozenset(
        {SessionState.ANSWERED, SessionState.CANCELLED, SessionState.FAILED}
    ),
    SessionState.ANSWERED: frozenset(
        {
            SessionState.PARSING,
            SessionState.ARCHIVED,
            SessionState.CANCELLED,
            SessionState.FAILED,
        }
    ),
    SessionState.ARCHIVED: frozenset(),
    SessionState.CANCELLED: frozenset(),
    SessionState.FAILED: frozenset(),
}

TERMINAL_STATES: frozenset[SessionState] = frozenset(
    {SessionState.ARCHIVED, SessionState.CANCELLED, SessionState.FAILED}
)


class InvalidStateTransitionError(Exception):
    """Raised when a session is asked to make an illegal transition."""

    def __init__(self, source: SessionState, target: SessionState) -> None:
        super().__init__(f"illegal transition {source.value} -> {target.value}")
        self.source = source
        self.target = target


def is_terminal(state: SessionState) -> bool:
    return state in TERMINAL_STATES


def can_transition(source: SessionState, target: SessionState) -> bool:
    if target is SessionState.FAILED:
        return source not in TERMINAL_STATES
    return target in ALLOWED_TRANSITIONS[source]


def assert_transition(source: SessionState, target: SessionState) -> None:
    if not can_transition(source, target):
        raise InvalidStateTransitionError(source, target)


@dataclass(frozen=True, slots=True)
class StateTransition:
    """One applied transition; ``reason`` is a category label, never user text."""

    from_state: SessionState
    to_state: SessionState
    reason: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SessionStateMachine:
    """Immutable state holder that appends a history entry per transition."""

    state: SessionState = SessionState.PARSING
    history: tuple[StateTransition, ...] = ()

    def transition_to(self, target: SessionState, reason: str, occurred_at: datetime) -> Self:
        assert_transition(self.state, target)
        entry = StateTransition(
            from_state=self.state,
            to_state=target,
            reason=reason,
            occurred_at=occurred_at,
        )
        return type(self)(state=target, history=(*self.history, entry))


class InteractionStatus(StrEnum):
    """Lifecycle of a single request/answer turn."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_INTERACTION_STATUSES: frozenset[InteractionStatus] = frozenset(
    {
        InteractionStatus.COMPLETED,
        InteractionStatus.FAILED,
        InteractionStatus.CANCELLED,
    }
)


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageKind(StrEnum):
    PLAIN_TEXT = "plain_text"
    CLARIFICATION = "clarification"
    PHASE = "phase"
    RESULT_TABLE = "result_table"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    """Durable interaction row; ownership fields come from trusted context only."""

    interaction_id: InteractionId
    session_id: SessionId
    tenant_id: TenantId
    user_id: UserId
    status: InteractionStatus
    state: SessionState
    input_text: str
    capability_id: CapabilityId | None
    clarification_rounds: int
    last_event_sequence: int
    error_category: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """Durable message row, unique on ``(interaction_id, sequence)``."""

    message_id: MessageId
    interaction_id: InteractionId
    session_id: SessionId
    tenant_id: TenantId
    user_id: UserId
    role: MessageRole
    kind: MessageKind
    sequence: int
    text: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One SSE event with a durable, monotonically increasing sequence."""

    sequence: int
    name: str
    data: dict[str, object]


INTERACTION_STARTED = "interaction.started"
INTERACTION_PHASE = "interaction.phase"
INTERACTION_CLARIFICATION = "interaction.clarification"
INTERACTION_RESULT = "interaction.result"
INTERACTION_HEARTBEAT = "interaction.heartbeat"
INTERACTION_COMPLETED = "interaction.completed"
INTERACTION_FAILED = "interaction.failed"
INTERACTION_CANCELLED = "interaction.cancelled"

TERMINAL_EVENT_NAMES: frozenset[str] = frozenset(
    {INTERACTION_COMPLETED, INTERACTION_FAILED, INTERACTION_CANCELLED}
)

_TERMINAL_EVENT_BY_STATUS: dict[InteractionStatus, str] = {
    InteractionStatus.COMPLETED: INTERACTION_COMPLETED,
    InteractionStatus.FAILED: INTERACTION_FAILED,
    InteractionStatus.CANCELLED: INTERACTION_CANCELLED,
}


def terminal_event_name(status: InteractionStatus) -> str:
    """Map a terminal interaction status to its single terminal event name."""
    try:
        return _TERMINAL_EVENT_BY_STATUS[status]
    except KeyError as exc:
        raise ValueError(f"{status.value} is not a terminal interaction status") from exc


@dataclass(frozen=True, slots=True)
class IntentSlots:
    """Slots the intent parser may fill; every value is reviewed and typed.

    Employee and department identifiers are deliberately absent: they come from
    the trusted ``DataScope`` and can never be supplied by the user or a model.
    """

    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    time_expression: str | None = None
    order_codes: tuple[str, ...] = ()
    plan_codes: tuple[str, ...] = ()
    style_codes: tuple[str, ...] = ()
    dept_names: tuple[str, ...] = ()
    employee_names: tuple[str, ...] = ()

    def filled_names(self) -> frozenset[str]:
        filled: set[str] = set()
        if self.time_range_start is not None and self.time_range_end is not None:
            filled.add("time_range")
        for name in ("order_codes", "plan_codes", "style_codes", "dept_names", "employee_names"):
            if getattr(self, name):
                filled.add(name)
        return frozenset(filled)


@dataclass(frozen=True, slots=True)
class CapabilityIntent:
    """Typed parser output; never a raw model dictionary."""

    capability_id: CapabilityId | None
    confidence: float
    slots: IntentSlots = field(default_factory=IntentSlots)
    missing: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    @property
    def needs_clarification(self) -> bool:
        return self.capability_id is None or bool(self.missing) or bool(self.ambiguous)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "INTERACTION_CANCELLED",
    "INTERACTION_CLARIFICATION",
    "INTERACTION_COMPLETED",
    "INTERACTION_FAILED",
    "INTERACTION_HEARTBEAT",
    "INTERACTION_PHASE",
    "INTERACTION_RESULT",
    "INTERACTION_STARTED",
    "TERMINAL_EVENT_NAMES",
    "TERMINAL_INTERACTION_STATUSES",
    "TERMINAL_STATES",
    "CapabilityIntent",
    "IntentSlots",
    "InteractionRecord",
    "InteractionStatus",
    "InvalidStateTransitionError",
    "MessageId",
    "MessageKind",
    "MessageRecord",
    "MessageRole",
    "SessionEvent",
    "SessionId",
    "SessionState",
    "SessionStateMachine",
    "StateTransition",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "terminal_event_name",
]
