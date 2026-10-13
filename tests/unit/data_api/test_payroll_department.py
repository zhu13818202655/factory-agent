import json
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import CURRENT_BUNDLE, MesCredentialBundle
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.data_api.result_cache import ResultCacheExecutor, ResultCacheMode, V2ResultCache
from factory_agent.domain import (
    DataScope,
    DeptId,
    EmployeeId,
    NarrowedFilters,
    Role,
    ScopeVersion,
    TenantId,
    UserId,
)
from factory_agent.domain.errors import ForbiddenError, InvalidRequestError
from factory_agent.execution.executor import ExecutionRequest, ScopedExecutor
from tests.unit.data_api.test_result_cache import make_cache

_TENANT = TenantId("SYNTHETIC-APP")
_WINDOW = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


def _bundle(role: str) -> MesCredentialBundle:
    return MesCredentialBundle(
        access_token="mock-access-token",
        app_key=str(_TENANT),
        sign="mock-sign",
        timestamp=1,
        expires_at=datetime.max.replace(tzinfo=UTC),
        user=UserId("synthetic-worker"),
        uname="合成员工",
        roles=(role,),
        dept="HOME-D",
        bound_depts=("HOME-D",),
    )


@pytest.mark.parametrize(
    ("role", "depts", "requested", "expected"),
    [
        ("99", ("HOME-D",), None, None),
        ("99", ("D2",), ("D2",), "D2"),
        ("02", ("D1",), None, "D1"),
        ("01", ("D1",), None, "D1"),
        ("02", ("D1", "D2"), None, None),
        ("99", ("D1", "D2"), ("D1", "D2"), None),
        ("00", ("HOME-D",), None, None),
        ("00", ("D2",), ("D2",), "D2"),
    ],
)
@pytest.mark.asyncio
async def test_payroll_pushes_only_one_effective_authorized_department(
    role: str, depts: tuple[str, ...], requested: tuple[str, ...] | None, expected: str | None
) -> None:
    recorded: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        recorded.append({"dept": body["dept"]} if "dept" in body else {})
        return httpx.Response(
            200,
            json={"code": 1, "message": "成功", "result": {"list": [], "total": 0}, "timestamp": 1},
        )

    filters = NarrowedFilters(
        tenant_id=_TENANT,
        employee_ids=None,
        dept_ids=frozenset(DeptId(value) for value in depts),
        requested_dept_ids=frozenset(DeptId(value) for value in requested) if requested else None,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle(role), load_catalog(), client=client
        )
        await adapter.fetch_resource("GongziClQuery", filters, _WINDOW, 50)
    assert recorded == ([{"dept": expected}] if expected is not None else [{}])


@pytest.mark.asyncio
async def test_context_bound_owner_keeps_factory_scope_with_a_manager_default_bundle() -> None:
    recorded: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        recorded.append({"dept": body["dept"]} if "dept" in body else {})
        return httpx.Response(
            200,
            json={"code": 1, "message": "成功", "result": {"list": [], "total": 0}, "timestamp": 1},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle("02"), load_catalog(), client=client
        )
        token = CURRENT_BUNDLE.set(_bundle("99"))
        try:
            await adapter.fetch_resource(
                "GongziClQuery",
                NarrowedFilters(_TENANT, None, frozenset({DeptId("HOME-D")})),
                _WINDOW,
                50,
            )
        finally:
            CURRENT_BUNDLE.reset(token)
    assert recorded == [{}]


@pytest.mark.parametrize("parameter", ["dept", "DEPT", "app_key", "sign"])
@pytest.mark.asyncio
async def test_extra_parameters_cannot_override_scope_or_credentials(parameter: str) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("untrusted scope input must be rejected before HTTP")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle("02"), load_catalog(), client=client
        )
        with pytest.raises(InvalidRequestError, match="scope and credential"):
            await adapter.fetch_resource(
                "GongziClQuery",
                NarrowedFilters(_TENANT, None, frozenset({DeptId("D1")})),
                _WINDOW,
                50,
                extra_params={parameter: "outside-scope-canary"},
            )
    assert calls == []


@pytest.mark.asyncio
async def test_denied_department_performs_zero_business_http_calls() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("denied scope must not reach the adapter")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle("02"), load_catalog(), client=client
        )
        executor = ScopedExecutor(adapter, load_catalog())
        scope = DataScope(
            _TENANT,
            frozenset({EmployeeId("synthetic-worker")}),
            frozenset({DeptId("D1")}),
            _WINDOW[0],
            ScopeVersion("synthetic-scope"),
        )
        with pytest.raises(ForbiddenError):
            await executor.execute_full_step(
                NarrowedFilters(_TENANT, None, frozenset({DeptId("outside-scope-canary")})),
                ExecutionRequest("GongziClQuery", _WINDOW, role=Role.MANAGER),
                active_scope=scope,
            )
    assert calls == []


@pytest.mark.asyncio
async def test_multiple_departments_keep_all_authorized_rows_after_realtime_fetch() -> None:
    source_rows = [
        {
            "id": f"row-{index}",
            "uid": "synthetic-worker",
            "dept": dept,
            "inputdate": "2026-09-10",
            "sl": "1",
            "je": "1",
        }
        for index, dept in enumerate(("D1", "D2", "outside-scope-canary"))
    ]
    recorded: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        recorded.append({"dept": body["dept"]} if "dept" in body else {})
        return httpx.Response(
            200,
            json={
                "code": 1,
                "message": "成功",
                "result": {"list": source_rows, "total": len(source_rows)},
                "timestamp": 1,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://mock.invalid"
    ) as client:
        adapter = HongzhaoMesAdapter(
            "http://mock.invalid", _bundle("02"), load_catalog(), client=client
        )
        cache = V2ResultCache(
            make_cache(),
            mode=ResultCacheMode.OFF,
            clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
            timezone=ZoneInfo("Asia/Shanghai"),
        )
        executor = ResultCacheExecutor(cast(Any, ScopedExecutor(adapter, load_catalog())), cache)
        result = await executor.execute_full_step(
            NarrowedFilters(_TENANT, None, frozenset({DeptId("D1"), DeptId("D2")})),
            ExecutionRequest("GongziClQuery", _WINDOW, role=Role.MANAGER),
        )
    assert result.complete
    assert {row["dept"] for row in result.rows} == {"D1", "D2"}
    assert "outside-scope-canary" not in repr(result.rows)
    assert recorded == [{}]
