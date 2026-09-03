"""Scope-deviation review task tests (Story 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from factory_agent.application.scope_review import ScopeReviewService
from factory_agent.domain import Role, TenantId, UserId
from factory_agent.ports.scope_violation import ScopeViolationRecord
from tests.support.scope_violation import InMemoryScopeViolationStore

NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
_SEQUENCE = iter(range(1, 1000))


def _record(
    *,
    tenant: str = "APPKEY-A",
    role: Role = Role.EMPLOYEE,
    capability: str = "FR-003",
    level: str = "exact_hit",
    code: str = "scope_mismatch_exact",
    created_at: datetime | None = None,
) -> ScopeViolationRecord:
    return ScopeViolationRecord(
        violation_id=f"v-{next(_SEQUENCE)}",
        tenant_id=TenantId(tenant),
        user_id=UserId("u-1"),
        role=role,
        capability_id=capability,
        level=level,
        mode="production",
        reason_code=code,
        interaction_id="it-1",
        expected_range="可查范围：本人的产量与工资数据",
        actual_summary="返回业务数据含 1 个范围外员工工号值",
        row_count=3,
        sample_count=1,
        sample_digests=("abc123",),
        created_at=created_at or NOW,
    )


@pytest.mark.asyncio
async def test_empty_period_is_a_normal_no_op_run() -> None:
    store = InMemoryScopeViolationStore()
    service = ScopeReviewService(store)

    report = await service.run_once(now=NOW)

    assert report.empty
    assert report.total_findings == 0
    assert report.groups == ()
    assert "no scope deviations" in service.render_text(report)


@pytest.mark.asyncio
async def test_aggregates_by_role_capability_level_and_reason() -> None:
    store = InMemoryScopeViolationStore()
    await store.record(_record(tenant="APPKEY-A", role=Role.EMPLOYEE, capability="FR-003"))
    await store.record(
        _record(
            tenant="APPKEY-A",
            role=Role.EMPLOYEE,
            capability="FR-003",
            created_at=NOW + timedelta(minutes=1),
        )
    )
    await store.record(
        _record(tenant="APPKEY-B", role=Role.MANAGER, capability="FR-008", level="heuristic_hit")
    )
    service = ScopeReviewService(store)

    report = await service.run_once(now=NOW + timedelta(days=1))

    assert report.total_findings == 3
    assert len(report.groups) == 2
    by_key = {(group.tenant_id, group.capability_id): group for group in report.groups}
    fr003 = by_key[("APPKEY-A", "FR-003")]
    assert fr003.count == 2
    assert fr003.role == Role.EMPLOYEE.value
    assert fr003.level == "exact_hit"
    fr008 = by_key[("APPKEY-B", "FR-008")]
    assert fr008.count == 1
    assert fr008.level == "heuristic_hit"


@pytest.mark.asyncio
async def test_review_window_excludes_old_findings() -> None:
    store = InMemoryScopeViolationStore()
    await store.record(_record(created_at=NOW - timedelta(days=30)))
    await store.record(_record(created_at=NOW - timedelta(hours=1)))
    service = ScopeReviewService(store, window_days=7)

    report = await service.run_once(now=NOW)

    assert report.total_findings == 1
