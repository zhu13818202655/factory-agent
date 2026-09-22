"""Shared session definitions: limits, run state, terminal constants."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from factory_agent.application.business_filters import ResolvedBusinessFilters
from factory_agent.application.context import ConversationTurn
from factory_agent.application.session.thinking import ThinkingTranscript
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
    #: A ``pending`` interaction older than this never had a claiming stream;
    #: the recovery sweeps fail it durably with ``abandoned``.
    abandoned_pending_seconds: float = 600.0
    #: Reasoning-transcript narration (``interaction.thinking``). On by
    #: default: it exists to cover the windows the caller would otherwise
    #: watch in silence, and those windows are ordinary rather than
    #: exceptional. Disabling it removes the extra narration call entirely.
    thinking_enabled: bool = True
    #: Wall-clock ceiling on one narration call. A narration that outlives the
    #: work it describes is abandoned rather than awaited — it must never be
    #: the reason an answer is late.
    thinking_timeout_seconds: float = 20.0
    #: Poll interval of the interleaving loop: how often the pipeline looks at
    #: the transcript and at the fetch counters while work is in flight.
    thinking_tick_seconds: float = 0.25
    #: Interval of the temporal wait sentence. A fetch that never pages — one
    #: large request answered in a single round trip — publishes no counters, so
    #: without this the longest window in the run narrates nothing at all.
    #: ``0`` leaves the fetch narrated by its counters only.
    thinking_wait_seconds: float = 5.0
    #: Output cap for one narration call; short by design, since the transcript
    #: describes the current step rather than the answer.
    thinking_max_output_tokens: int = 160


EMPTY_BUSINESS_FILTERS = ResolvedBusinessFilters(
    employee_ids=None,
    dept_ids=None,
    order_codes=None,
    style_codes=None,
    plan_codes=None,
    material_ids=None,
)


@dataclass(frozen=True, slots=True)
class StartRequest:
    """Everything a turn needs; ownership never comes from the request body."""

    session_id: SessionId
    text: str
    history: tuple[ConversationTurn, ...] = ()
    clarification_rounds: int = 0
    #: Structured drill-down (D-3 拍板): capability + business narrowing slots
    #: supplied by the client from a card action. Validated server-side before
    #: any business call; absent = a normal text-parsed turn.
    drill: "DrillPayload | None" = None


@dataclass(frozen=True, slots=True)
class DrillPayload:
    """Structured drill request carried from ``start`` to the claiming executor.

    Scope identifiers are absent by design: the drill only carries business
    narrowing values (a department id set and/or one target employee uid) and
    the time window of the card the user clicked. Every value is re-validated
    server-side against the active DataScope before any business call — the
    drill can only narrow, never broaden.

    Time is carried as an absolute echo (D-7 拍板): the result payload states
    the window it actually resolved, the client sends ``time_range_start`` /
    ``time_range_end`` back verbatim, and ``time_expression`` rides along as
    the human label only. The absolute pair wins over the expression because
    a reviewed expression can name a single day or month and can never express
    the multi-day range the card actually answered; the expression is what the
    composed answer and the export filename display. Both fields may be absent
    (old client): the executor then inherits the newest window of the same
    session and only falls back to the 当月 default when the session has none.

    The payload lives in-process only (single-worker deployment): if the
    process restarts before the interaction is claimed, the pending row is
    swept to ``abandoned`` and the user retries — a lost drill never degrades
    into a silently text-parsed query.
    """

    capability_id: str
    dept_ids: tuple[str, ...] = ()
    employee_uid: str | None = None
    time_expression: str | None = None
    #: Absolute half-open window echoed from the card payload. Naive datetimes
    #: are read in the factory timezone; the same redline that judges a
    #: model-proposed range judges this one.
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None


@dataclass
class RunState:
    record: InteractionRecord
    sequence: int
    started_monotonic: float = 0.0
    last_intent: CapabilityIntent | None = None
    #: This round's reasoning transcript, opened by the pipeline and closed by
    #: whichever terminal path ends the round. It lives on the run state rather
    #: than being threaded through the outcome emitters so that *every* terminal
    #: closes it: a terminal that forgot to would ship the client a truncated
    #: block with no ``done`` frame (契约 §7-8).
    transcript: ThinkingTranscript | None = None

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
#: Recovery categories written by the sweeps.
CATEGORY_EXECUTOR_LOST = "executor_lost"
CATEGORY_ABANDONED = "abandoned"
#: User-facing notice persisted with an abandoned interaction's terminal event.
ABANDONED_NOTICE = "该提问未能开始执行，请重新发送。"
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

#: Stage labels carried on progress events, keyed by the reason that announces
#: them. Deliberately independent of ``STAGE_LABELS``: progress names work that
#: is still in flight, and every announced window runs while the state machine
#: is still ``PARSING``, so no state exists to label it from.
PROGRESS_LABELS: dict[str, str] = {
    "parse_started": "解析中",
    "authorize_started": "权限检查中",
    "scope_resolution_started": "核对数据范围",
}
