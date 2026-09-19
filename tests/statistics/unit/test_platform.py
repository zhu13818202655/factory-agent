"""PlatformScope RBAC tests.

Identity never comes from headers any more (D-2), so the resolved-scope tests
that used to sit here are gone: the only wire format is a signed bearer token,
and its resolution is covered by ``test_auth.py``.
"""

import pytest

from factory_agent.statistics.platform import (
    PlatformRole,
    PlatformScope,
    PlatformScopeError,
)


def test_parse_roles() -> None:
    assert PlatformRole.parse("viewer") == PlatformRole.VIEWER
    assert PlatformRole.parse("analyst") == PlatformRole.ANALYST
    assert PlatformRole.parse("admin") == PlatformRole.ADMIN
    assert PlatformRole.parse("boss") is None
    assert PlatformRole.parse(None) is None


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


def test_admin_may_export_and_manage_tenants() -> None:
    admin = PlatformScope("ops-1", PlatformRole.ADMIN, frozenset())
    analyst = PlatformScope("ops-2", PlatformRole.ANALYST, frozenset())
    viewer = PlatformScope("ops-3", PlatformRole.VIEWER, frozenset())
    assert admin.allows_export()
    assert admin.allows_manage_tenants()
    assert not analyst.allows_manage_tenants()
    assert not viewer.allows_manage_tenants()
