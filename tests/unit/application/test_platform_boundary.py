"""PlatformScope isolation guard tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory_agent.application.platform_boundary import (
    PlatformBoundaryGuard,
    PlatformScopeViolationError,
)
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    PlatformCapability,
    PlatformScope,
    PrincipalId,
    ScopeVersion,
    TenantId,
)

AS_OF = datetime(2026, 8, 21, tzinfo=timezone.utc)


def data_scope() -> DataScope:
    return DataScope(
        tenant_id=TenantId("tenant-a"),
        employee_ids=frozenset({EmployeeId("e1")}),
        dept_ids=frozenset({DeptId("g1")}),
        evaluated_at=AS_OF,
        scope_version=ScopeVersion("v"),
    )


def platform_scope() -> PlatformScope:
    return PlatformScope(
        principal_id=PrincipalId("ops-1"),
        tenant_ids=frozenset({TenantId("tenant-a"), TenantId("tenant-b")}),
        capabilities=frozenset({PlatformCapability.USAGE_AGGREGATE}),
    )


def test_platform_scope_entering_mes_path_raises() -> None:
    guard = PlatformBoundaryGuard()

    with pytest.raises(PlatformScopeViolationError):
        guard.assert_factory_context(platform_scope())  # type: ignore[arg-type]


def test_missing_data_scope_is_rejected() -> None:
    guard = PlatformBoundaryGuard()

    with pytest.raises(PlatformScopeViolationError):
        guard.assert_factory_context(None)


def test_factory_data_scope_passes_guard() -> None:
    guard = PlatformBoundaryGuard()

    guard.assert_factory_context(data_scope())


def test_data_scope_cannot_be_used_for_platform_operations() -> None:
    guard = PlatformBoundaryGuard()

    with pytest.raises(PlatformScopeViolationError):
        guard.assert_platform_context(data_scope())  # type: ignore[arg-type]


def test_platform_scope_passes_platform_guard() -> None:
    guard = PlatformBoundaryGuard()

    guard.assert_platform_context(platform_scope())
