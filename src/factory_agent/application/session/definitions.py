"""Shared session definitions: limits, run state, terminal constants."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from factory_agent.application.business_filters import ResolvedBusinessFilters
from factory_agent.application.context import ConversationTurn
from factory_agent.domain import (
    CapabilityIntent,
    InteractionRecord,
    InteractionStatus,
    SessionId,
    SessionState,
    terminal_event_name,
)
from factory_agent.observability.logging_adapter import get_logger

IdFactory = Callable[[], str]

session_logger = get_logger("session.consistency")

#: Customer-confirmed time-range ceiling: at most the past year. Requests
#: beyond it are terminated with a friendly notice before any MES call.
DEFAULT_TIME_RANGE_MAX_DAYS = 366


class InteractionNotFoundError(LookupError):
    """Raised for both a missing interaction and one owned by another user."""


@dataclass(frozen=True, slots=True)
class SessionLimits:
    max_input_chars: int = 2000
    max_clarification_rounds: int = 3
    heartbeat_seconds: float = 15.0
    follow_timeout_seconds: float = 600.0
    stale_running_seconds: float = 600.0
    #: Whole-run wall-clock budget enforced by the background executor at its
    #: cooperative stop points; must stay below
    #: ``stale_running_seconds`` (see ``config.session_run_timeout_seconds``).
    run_timeout_seconds: float = 300.0


EMPTY_BUSINESS_FILTERS = ResolvedBusinessFilters(
    employee_ids=None,
    dept_ids=None,
    order_codes=None,
    style_codes=None,
    plan_codes=None,
)


@dataclass(frozen=True, slots=True)
class StartRequest:
    """Everything a turn needs; ownership never comes from the request body."""

    session_id: SessionId
    text: str
    history: tuple[ConversationTurn, ...] = ()
    clarification_rounds: int = 0


@dataclass
class RunState:
    record: InteractionRecord
    sequence: int
    started_monotonic: float = 0.0
    last_intent: CapabilityIntent | None = None

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)


TERMINAL_STATUSES = frozenset(
    {InteractionStatus.COMPLETED, InteractionStatus.FAILED, InteractionStatus.CANCELLED}
)

#: Cooperative stop-point verdicts.
STOP_CANCELLED = "cancelled"
STOP_RUN_TIMEOUT = "run_timeout"
TERMINAL_NAMES = frozenset(
    {
        terminal_event_name(InteractionStatus.COMPLETED),
        terminal_event_name(InteractionStatus.FAILED),
        terminal_event_name(InteractionStatus.CANCELLED),
    }
)

#: Human-readable stage labels carried on phase events.
STAGE_LABELS: dict[SessionState, str] = {
    SessionState.PARSING: "解析",
    SessionState.AUTHORIZING: "鉴权",
    SessionState.EXECUTING: "取数",
    SessionState.COMPOSING: "计算",
    SessionState.ANSWERED: "完成",
    SessionState.CLARIFYING: "追问",
    SessionState.FAILED: "失败",
    SessionState.CANCELLED: "取消",
}
