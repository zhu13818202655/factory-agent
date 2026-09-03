"""Morning-report generation + local push channel tests (Story 3B)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.application.permission_matrix import Capability
from factory_agent.application.push_channel import LocalPushChannel
from factory_agent.application.reporting import (
    DirectReportRunner,
    ReportingService,
    SummaryDeniedError,
)
from factory_agent.domain import (
    CapabilityId,
    Role,
    TenantId,
    TimeRange,
    UserId,
)
from factory_agent.ports.contracts import TrustedCredential
from tests.support.authorization import (
    FakeMembershipSource,
    FakeOrganizationSource,
    membership,
)
from tests.support.push import InMemoryPushDeliveryStore
from tests.support.session import (
    FrozenClock,
    RecordingCapabilityRunner,
    SequentialIds,
)

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
WINDOW = TimeRange(
    start=datetime(2026, 9, 1, tzinfo=timezone.utc),
    end=datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc),
)


def _credential() -> TrustedCredential:
    return TrustedCredential(tenant_id=TenantId("APPKEY-A"), user_id=UserId("01001"))


def _authorization(role: Role = Role.EMPLOYEE) -> AuthorizationService:
    member = membership("01001", "APPKEY-A", "01001", role)
    return AuthorizationService(
        memberships=FakeMembershipSource(memberships_by_credential={("APPKEY-A", "01001"): member}),
        organizations=FakeOrganizationSource(depts_by_employee={"01001": ("dept-a1",)}),
        versions=FixedScopeVersionAssigner(),
    )


@pytest.mark.asyncio
async def test_morning_report_generates_scoped_sections_and_delivers() -> None:
    runner = RecordingCapabilityRunner()
    deliveries = InMemoryPushDeliveryStore()
    channel = LocalPushChannel(deliveries, new_id=SequentialIds(prefix="dlv"))
    service = ReportingService(
        DirectReportRunner(
            _authorization(),
            runner,
            clock=FrozenClock(NOW),
        ),
        channel,
        clock=FrozenClock(NOW),
    )

    report = await service.generate_morning_report(_credential())

    assert report is not None
    assert report.role is Role.EMPLOYEE
    # Employee report covers 本人产量 + 本人工资汇总.
    recipe_ids = {request.capability_id for request in runner.requests}
    assert recipe_ids == {
        CapabilityId("fr001_personal_output"),
        CapabilityId("fr002_personal_wage_summary"),
    }
    assert len(report.sections) == 2
    assert report.row_count_total == 2
    assert "【FR-002】" in report.body
    assert len(deliveries.deliveries) == 1
    assert deliveries.deliveries[0].kind == "morning_report"
    assert deliveries.deliveries[0].message_digest  # envelope only, no body stored


@pytest.mark.asyncio
async def test_morning_report_never_crosses_role_matrix() -> None:
    """FR-009 (owner-only) invoked for an employee is denied before any fetch."""
    runner = RecordingCapabilityRunner()
    direct = DirectReportRunner(_authorization(Role.EMPLOYEE), runner, clock=FrozenClock(NOW))

    with pytest.raises(SummaryDeniedError):
        await direct.run(_credential(), Capability.FACTORY_ORDER_OVERVIEW, WINDOW)
    assert runner.requests == []


@pytest.mark.asyncio
async def test_local_channel_records_envelope_only() -> None:
    deliveries = InMemoryPushDeliveryStore()
    channel = LocalPushChannel(deliveries, new_id=SequentialIds(prefix="dlv"))

    ok = await channel.deliver(
        tenant_id=TenantId("APPKEY-A"),
        user_id=UserId("01001"),
        kind="morning_report",
        content_item_id=None,
        message_digest="deadbeef",
        row_count=2,
        now=NOW,
    )

    assert ok is True
    assert deliveries.deliveries[0].status == "delivered"
    # No message content/amounts ever enter the delivery log.
    assert "工资" not in repr(deliveries.deliveries[0])
