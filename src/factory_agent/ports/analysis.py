from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from sageql_core import RunControl

from factory_agent.domain import DataScope, IntentSlots, NarrowedFilters, TenantContext, TimeRange
from factory_agent.ports.session import CapabilityRunResult, UsageEvent


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    interaction_id: str
    question: str
    context: TenantContext
    scope: DataScope
    slots: IntentSlots
    time_range: TimeRange | None
    deadline: float
    cancelled: Callable[[], bool]
    validate_active: Callable[[], Awaitable[bool]]
    filters: NarrowedFilters | None = None
    validate_fact_scope: Callable[[str], Awaitable[bool]] | None = None
    run_control: RunControl | None = None
    self_only: bool = False
    factory_timezone: str = "Asia/Shanghai"
    emit_phase: Callable[[str], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    status: Literal[
        "completed", "knowledge", "clarify", "unavailable", "denied", "stopped", "failed"
    ]
    result: CapabilityRunResult | None = None
    answer: str | None = None
    reason: str | None = None
    usage_events: tuple[UsageEvent, ...] = ()


class AnalysisRunner(Protocol):
    async def analyze(self, request: AnalysisRequest) -> AnalysisOutcome: ...
