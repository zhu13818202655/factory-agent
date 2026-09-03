"""Daily morning report & subscription summary generation (Story 3B).

Generation reuses the reviewed L1 read-only capability path. Because no
credential material may be stored (SECURITY), an unattended 08:00 run needs a
customer-confirmed task-credential mechanism (open decision); until then the
report is generated on demand for an authenticated user whose live credential
authorizes every fetch (pull 形态 + 站内记录), then delivered through the push
channel port (local fake channel in this story).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Protocol

from factory_agent.application.authorization import AuthorizationService, ResolvedAuthorization
from factory_agent.application.business_filters import BusinessFilterResolver
from factory_agent.application.capability_map import RECIPE_BY_FR
from factory_agent.application.filters import FilterNarrower
from factory_agent.application.permission_matrix import Capability, authorize_capability
from factory_agent.application.push_catalog import MORNING_REPORT_CAPABILITIES
from factory_agent.domain import CapabilityId, Role, TimeRange
from factory_agent.ports import CapabilityRunRequest, CapabilityRunResult, TrustedCredential
from factory_agent.ports.push import PushChannel
from factory_agent.ports.session import CapabilityRunner

_PERSONAL_CAPABILITIES = frozenset(
    {
        Capability.OWN_OUTPUT,
        Capability.OWN_PAYROLL_SUMMARY,
        Capability.OWN_PAYROLL_DETAIL,
        Capability.GROUP_INCOME_RANK,
    }
)


class SummaryDeniedError(Exception):
    """A capability is not available for the caller's role."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class DirectReportRunner:
    """Runs one reviewed capability under a live credential (read-only).

    Mirrors the session pipeline's authorization → narrowing → run steps
    without any LLM intent parsing. Scope identifiers reach the executor only
    through ``NarrowedFilters``; nothing here can broaden a scope.
    """

    def __init__(
        self,
        authorization: AuthorizationService,
        runner: CapabilityRunner,
        *,
        clock: Clock | None = None,
        business_filters: BusinessFilterResolver | None = None,
    ) -> None:
        self._authorization = authorization
        self._runner = runner
        self._clock = clock or _SystemClock()
        self._business_filters = business_filters
        self._narrower = FilterNarrower()

    async def authorize(self, credential: TrustedCredential) -> ResolvedAuthorization:
        return await self._authorization.authorize(credential, self._clock.now())

    async def run(
        self,
        credential: TrustedCredential,
        capability: Capability,
        time_range: TimeRange,
    ) -> CapabilityRunResult:
        authorization = await self.authorize(credential)
        context = authorization.tenant_context
        scope = authorization.data_scope
        decision = authorize_capability(capability, context, scope)
        if not decision.allowed:
            raise SummaryDeniedError(decision.reason or "capability denied")
        filters = self._narrower.narrow(
            scope,
            employee_ids=scope.employee_ids if capability in _PERSONAL_CAPABILITIES else None,
            restrict_to_scope_employees=capability not in _PERSONAL_CAPABILITIES,
        )
        recipe_id = RECIPE_BY_FR.get(str(capability))
        if recipe_id is None:
            raise SummaryDeniedError("capability has no reviewed recipe")
        return await self._runner.run(
            CapabilityRunRequest(
                capability_id=CapabilityId(recipe_id),
                filters=filters,
                time_range=time_range,
                role=context.role,
            )
        )


@dataclass(frozen=True, slots=True)
class SummarySection:
    capability_id: str
    title: str
    row_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MorningReport:
    role: Role
    date_label: str
    sections: tuple[SummarySection, ...]
    body: str

    @property
    def row_count_total(self) -> int:
        return sum(section.row_count for section in self.sections)


def _format_section(capability: Capability, result: CapabilityRunResult) -> SummarySection:
    columns = result.column_names
    lines: list[str] = []
    for row in result.rows[:5]:
        rendered = "，".join(f"{name}={value}" for name, value in zip(columns, row, strict=False))
        lines.append(rendered)
    return SummarySection(
        capability_id=str(capability),
        title=str(capability),
        row_count=len(result.rows),
        lines=tuple(lines),
    )


def _message_body(sections: tuple[SummarySection, ...]) -> str:
    chunks: list[str] = []
    for section in sections:
        chunks.append(f"【{section.title}】共 {section.row_count} 行")
        chunks.extend(section.lines)
    return "\n".join(chunks)


class ReportingService:
    """Generates and delivers scoped summaries for the calling user."""

    def __init__(
        self,
        runner: DirectReportRunner | None,
        channel: PushChannel | None,
        *,
        clock: Clock | None = None,
        default_time: str = "08:00",
    ) -> None:
        self._runner = runner
        self._channel = channel
        self._clock = clock or _SystemClock()
        self._default_time = default_time

    async def generate_morning_report(self, credential: TrustedCredential) -> MorningReport | None:
        """昨日产量/工资摘要, scoped to the caller's role (pull / on-demand)."""
        if self._runner is None:
            return None
        authorization = await self._runner.authorize(credential)
        role = authorization.tenant_context.role
        now = self._clock.now()
        # 最近一个自然日 (factory timezone is applied by the caller); use
        # server UTC day as the local-default window — the story default time
        # is configurable per deployment timezone.
        day = (now - timedelta(days=1)).date()
        window = TimeRange(
            start=datetime.combine(day, time.min, tzinfo=now.tzinfo),
            end=datetime.combine(day, time.max, tzinfo=now.tzinfo),
        )
        sections: list[SummarySection] = []
        for capability in MORNING_REPORT_CAPABILITIES[role]:
            result = await self._runner.run(credential, capability, window)
            sections.append(_format_section(capability, result))
        body = _message_body(tuple(sections))
        report = MorningReport(
            role=role,
            date_label=day.isoformat(),
            sections=tuple(sections),
            body=body,
        )
        if self._channel is not None:
            digest = hashlib.sha256(body.encode()).hexdigest()[:16]
            await self._channel.deliver(
                tenant_id=authorization.tenant_context.tenant_id,
                user_id=authorization.tenant_context.user_id,
                kind="morning_report",
                content_item_id=None,
                message_digest=digest,
                row_count=report.row_count_total,
                now=now,
            )
        return report


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


__all__ = [
    "DirectReportRunner",
    "MorningReport",
    "ReportingService",
    "SummaryDeniedError",
    "SummarySection",
]
