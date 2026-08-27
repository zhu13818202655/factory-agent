"""PlatformScope RBAC tests."""

from __future__ import annotations

import pytest
from usage_admin.platform import (
    PlatformRole,
    PlatformScope,
    PlatformScopeError,
    resolve_platform_scope,
)


def test_parse_roles() -> None:
    assert PlatformRole.parse("viewer") == PlatformRole.VIEWER
    assert PlatformRole.parse("analyst") == PlatformRole.ANALYST
    assert PlatformRole.parse("boss") is None
    assert PlatformRole.parse(None) is None


def test_resolve_scope_from_trusted_headers() -> None:
    scope = resolve_platform_scope("ops-1", "analyst", "tenant-a,tenant-b")
    assert scope.principal_id == "ops-1"
    assert scope.role == PlatformRole.ANALYST
    assert scope.tenant_ids == frozenset({"tenant-a", "tenant-b"})


def test_missing_principal_is_rejected() -> None:
    with pytest.raises(PlatformScopeError, match="principal"):
        resolve_platform_scope(None, "analyst", None)


def test_unsupported_role_is_rejected() -> None:
    with pytest.raises(PlatformScopeError, match="role"):
        resolve_platform_scope("ops-1", "employee", None)


def test_platform_wide_scope_covers_every_tenant() -> None:
    scope = PlatformScope("ops-1", PlatformRole.VIEWER, frozenset())
    assert scope.covers_tenant("any")
    assert scope.effective_tenants(frozenset({"a", "b"})) == frozenset({"a", "b"})


def test_scoped_tenants_are_intersected_never_widened() -> None:
    scope = PlatformScope("ops-1", PlatformRole.VIEWER, frozenset({"tenant-a"}))
    assert scope.effective_tenants(frozenset({"tenant-a", "tenant-b"})) == frozenset({"tenant-a"})


def test_require_covers_rejects_out_of_scope_request() -> None:
    scope = PlatformScope("ops-1", PlatformRole.VIEWER, frozenset({"tenant-a"}))
    with pytest.raises(PlatformScopeError):
        scope.require_covers(frozenset({"tenant-a", "tenant-b"}))


def test_only_analyst_may_export() -> None:
    viewer = PlatformScope("ops-1", PlatformRole.VIEWER, frozenset())
    analyst = PlatformScope("ops-2", PlatformRole.ANALYST, frozenset())
    assert not viewer.allows_export()
    assert analyst.allows_export()
