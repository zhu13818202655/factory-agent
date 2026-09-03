"""Mock MES customer-shaped API tests (PG-backed): auth, envelope,
filtering, pagination and the wages golden over the generated data base.

Identity comes from the generated employee master: ``MOCK-TOKEN-<uid>`` (or a
JWT from ``/api/system/token``) names a generated account, so the four role
tiers (00 员工 / 01 组长 / 02 管理 / 99 老板) are exercised against real data.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any, cast

import pytest
from httpx import AsyncClient

BOSS = {"Authorization": "Bearer MOCK-TOKEN-01009"}
MANAGER = {"Authorization": "Bearer MOCK-TOKEN-01008"}
#: Generated group leader of dept-a1 (position 11 of the generated roster).
LEADER = {"Authorization": "Bearer MOCK-TOKEN-01012"}
WORKER = {"Authorization": "Bearer MOCK-TOKEN-01001"}
WORKER_B = {"Authorization": "Bearer MOCK-TOKEN-02001"}
WINDOW = {"dates": "2026-07-01", "datee": "2026-08-31"}


async def login(
    client: AsyncClient, app_key: str = "APPKEY-A", uid: str | None = None
) -> dict[str, Any]:
    body: dict[str, object] = {"app_key": app_key}
    if uid:
        body["uid"] = uid
    response = await client.post("/api/system/token", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 1
    result = payload["result"]
    assert isinstance(result, dict)
    return dict(cast(dict[str, Any], result))


def common(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "app_key": result["appkey"],
        "timestamp": result["timestamp"],
        "sign": result["sign"],
    }


@pytest.mark.asyncio
async def test_token_returns_full_credential_bundle(client: AsyncClient) -> None:
    result = await login(client)

    assert result["tokenType"] == "Bearer"
    assert result["expiresIn"] == 7200
    # Customer contract: ``roles`` is a single code string, not an array, and
    # ``dept``/``boundDepts`` carry the bound organisation. With no ``uid`` the
    # mock logs in as the tenant boss (01009).
    assert result["roles"] == "99"
    assert result["permissions"] == []
    assert result["user"] == "01009"
    assert result["dept"] == "dept-a1"
    assert result["boundDepts"] == ["dept-a1"]
    assert set(result) >= {
        "accessToken",
        "expiresAt",
        "uname",
        "loginUserName",
        "appkey",
        "sign",
        "timestamp",
    }
    # The JWT payload also carries dept + roles (customer §2 sample).
    claims = json.loads(base64.urlsafe_b64decode(result["accessToken"].split(".")[1] + "=="))
    assert claims["dept"] == "dept-a1"
    assert claims["roles"] == "99"


@pytest.mark.asyncio
async def test_token_login_for_each_generated_role(client: AsyncClient) -> None:
    """Four-role login samples: 99 boss / 02 manager / 01 leader / 00 worker.

    Identities are generated employees (not a static fixture): every role tier
    can obtain a token bundle carrying its authoritative role + bound depts.
    """
    cases = [
        ("01009", "99", "dept-a1", ["dept-a1"]),  # 老板 全厂
        ("01008", "02", "dept-a1", ["dept-a1"]),  # 管理 绑定部门
        ("01012", "01", "dept-a1", ["dept-a1"]),  # 组长 绑定小组(dept 同部门)
        ("01001", "00", "dept-a1", ["dept-a1"]),  # 员工 本人
    ]
    for uid, role, dept, bound in cases:
        result = await login(client, app_key="APPKEY-A", uid=uid)
        assert result["user"] == uid
        assert result["roles"] == role
        assert result["dept"] == dept
        assert result["boundDepts"] == bound
        claims = json.loads(base64.urlsafe_b64decode(result["accessToken"].split(".")[1] + "=="))
        assert claims["dept"] == dept
        assert claims["roles"] == role


@pytest.mark.asyncio
async def test_cross_workshop_manager_token_carries_bound_depts(
    client: AsyncClient,
) -> None:
    """02 管理 may bind several 车间 (客户确认); the bundle carries the full set."""
    result = await login(client, app_key="APPKEY-A", uid="01101")
    assert result["roles"] == "02"
    assert result["boundDepts"] == ["dept-a2", "dept-a4"]


@pytest.mark.asyncio
async def test_token_rejects_empty_app_key(client: AsyncClient) -> None:
    response = await client.post("/api/system/token", json={"app_key": ""})

    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "app_key不能为空"
    assert body["result"] is None


@pytest.mark.asyncio
async def test_error_scenarios_cover_customer_messages(client: AsyncClient) -> None:
    result = await login(client)
    headers = {"Authorization": f"Bearer {result['accessToken']}"}
    cases = [
        ({"app_key": "", "timestamp": 1, "sign": "x"}, "app_key不能为空"),
        ({"app_key": "BAD", "timestamp": 1, "sign": "x"}, "无效app_key"),
        (
            {**common(result), "sign": "deadbeef"},
            "签名无效",
        ),
        (
            {"app_key": "APPKEY-A", "timestamp": "oops", "sign": "x"},
            "加密信息解析失败,请检查参数是否正确",
        ),
    ]
    for body, message in cases:
        response = await client.post("/api/NetYf/Sclzd/YskQuery", json=body, headers=headers)
        assert response.json()["code"] == 0, body
        assert response.json()["message"] == message, body

    missing = await client.post("/api/not/exist", json={}, headers=headers)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_row_level_filtering_three_tiers(client: AsyncClient) -> None:
    """boss sees all company rows; manager dept-filtered; worker own-only."""
    result = await login(client)
    params = {**common(result), "page": 1, "size": 50, "Uid": "01001", **WINDOW}

    boss = await client.post("/api/NetYf/Sclzd/YskQuery", json=params, headers=BOSS)
    manager = await client.post("/api/NetYf/Sclzd/YskQuery", json=params, headers=MANAGER)
    worker = await client.post("/api/NetYf/Sclzd/YskQuery", json=params, headers=WORKER)

    boss_rows = boss.json()["result"]["list"]
    manager_rows = manager.json()["result"]["list"]
    worker_rows = worker.json()["result"]["list"]

    assert len(boss_rows) >= len(manager_rows) >= len(worker_rows)
    assert all(row["dept"] == "dept-a1" for row in manager_rows)
    assert all(row["uid"] == "01001" for row in worker_rows)


@pytest.mark.asyncio
async def test_company_isolation_between_app_keys(client: AsyncClient) -> None:
    result = await login(client)
    # Company B token presenting company A's AppKey is rejected.
    response = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json={**common(result), "page": 1, "size": 50, "Uid": "02001", **WINDOW},
        headers=WORKER_B,
    )

    assert response.json()["code"] == 0
    assert response.json()["message"] == "无效app_key"


@pytest.mark.asyncio
async def test_gongzi_mx_three_sources_merge_with_footer(client: AsyncClient) -> None:
    result = await login(client)
    response = await client.post(
        "/api/NetYf/Sclzd/GongziMxQuery",
        json={
            **common(result),
            "page": 1,
            "size": 200,
            "queryFooter": True,
            "Uid": "01001",
            "Flag": "0",
            "Type": "0,1,2",
            "scheme": "hz",
            **WINDOW,
        },
        headers=BOSS,
    )

    body = response.json()
    assert body["code"] == 1
    types = {row["type"] for row in body["result"]["list"]}
    assert types <= {"扫码产量", "吊挂产量", "手工账产量"}
    footer = body["result"]["footer"]
    assert set(footer) == {"bs_total", "fhsl_total", "sl_total", "je_total"}
    # Summary rows aggregate across prices; footer equals the sum of rows.
    je_sum = sum((Decimal(row["je"]) for row in body["result"]["list"]), Decimal())
    assert Decimal(footer["je_total"]) == je_sum


@pytest.mark.asyncio
async def test_worktype_progress_consistency_with_barcodes(client: AsyncClient) -> None:
    """uid non-empty marks the worktype scanned (M6); others stay empty."""
    result = await login(client)
    response = await client.post(
        "/api/NetYf/Sclzd/WorktypeProgressQuery",
        json={**common(result), "page": 1, "size": 50, "userid": "1001", "uid": ""},
        headers=BOSS,
    )

    rows = response.json()["result"]["list"]
    assert len(rows) == 3  # total worktypes for the huohao
    scanned = [row for row in rows if row["uid"]]
    unswept = [row for row in rows if not row["uid"]]
    assert {row["worktype"] for row in scanned} == {"WT01", "WT03"}
    assert {row["worktype"] for row in unswept} == {"WT02"}


@pytest.mark.asyncio
async def test_ysk_and_wsk_return_footer_without_query_footer_param(client: AsyncClient) -> None:
    result = await login(client)
    ysk = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json={**common(result), "page": 1, "size": 50, "Uid": "01001", **WINDOW},
        headers=BOSS,
    )
    wsk = await client.post(
        "/api/NetYf/Sclzd/WskQuery",
        json={**common(result), "page": 1, "size": 50, **WINDOW},
        headers=BOSS,
    )

    assert "footer" in ysk.json()["result"]
    assert set(ysk.json()["result"]["footer"]) == {"bs_total", "sl_total", "je_total"}
    assert "footer" in wsk.json()["result"]
    assert set(wsk.json()["result"]["footer"]) == {"bs_total", "sl_total"}


@pytest.mark.asyncio
async def test_all_27_endpoints_are_registered(client: AsyncClient) -> None:
    paths = set(client._transport.app.openapi()["paths"])  # type: ignore[attr-defined]
    expected = {
        "/api/system/token",
        "/api/print/query-sign",
        "/api/print/test-permissions",
        "/api/NetYf/Baseinfo/UserInfoQuery",
        "/api/NetYf/Baseinfo/MoveMenuQuery",
        "/api/NetYf/Baseinfo/HuohaoQuery",
        "/api/NetYf/Baseinfo/HuohaoFormQuery",
        "/api/NetYf/Baseinfo/ScTypeQuery",
        "/api/NetYf/Baseinfo/RfidWorktypeQuery",
        "/api/NetYf/Baseinfo/HuohaoWorktypeQuery",
        "/api/NetYf/Baseinfo/EmployeeQuery",
        "/api/NetYf/Baseinfo/DeptQuery",
        "/api/NetYf/Plan/GridPageList",
        "/api/NetYf/Sclzd/GridPageList",
        "/api/NetYf/Sclzd/SclzdWorktypeQuery",
        "/api/NetYf/Sclzd/SclzdBarcodeQuery",
        "/api/NetYf/Sclzd/BarcodeClQuery",
        "/api/NetYf/Sclzd/HuohaoWtCLQuery",
        "/api/NetYf/PinFeng/GridPageList",
        "/api/NetYf/Sclzd/WorktypeProgressQuery",
        "/api/NetYf/Sclzd/YskQuery",
        "/api/NetYf/Sclzd/WskQuery",
        "/api/NetYf/Sclzd/GongziMxQuery",
        "/api/NetYf/Sclzd/GongziJeOrderQuery",
        "/api/NetYf/Dg/GridPageList",
        "/api/NetYf/Dg/DgZuGridPageList",
        "/api/NetYf/Dg/DgClQuery",
    }
    assert expected <= paths


@pytest.mark.asyncio
async def test_basic_data_endpoints_return_rows(client: AsyncClient) -> None:
    result = await login(client)
    params = {**common(result), "page": 1, "size": 50}
    huohao = await client.post("/api/NetYf/Baseinfo/HuohaoQuery", json=params, headers=BOSS)
    sc = await client.post("/api/NetYf/Baseinfo/ScTypeQuery", json=params, headers=BOSS)
    rfid = await client.post("/api/NetYf/Baseinfo/RfidWorktypeQuery", json=params, headers=BOSS)
    dept = await client.post("/api/NetYf/Baseinfo/DeptQuery", json=params, headers=BOSS)

    assert huohao.json()["result"]["hh_total"] >= 2
    assert sc.json()["result"]["total"] == 1
    assert rfid.json()["result"]["total"] == 3
    # COMPANY-A (APPKEY-A) has five workshops at the default scale; the
    # COMPANY-B workshop stays invisible under tenant isolation.
    assert dept.json()["result"]["total"] == 5


async def _employee_rows(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Full COMPANY-A employee roster (base data is role-neutral)."""
    result = await login(client)
    response = await client.post(
        "/api/NetYf/Baseinfo/EmployeeQuery",
        json={**common(result), "page": 1, "size": 1000},
        headers=headers,
    )
    assert response.json()["code"] == 1
    return [dict(cast(dict[str, Any], row)) for row in response.json()["result"]["list"]]


@pytest.mark.asyncio
async def test_base_data_is_not_role_filtered(client: AsyncClient) -> None:
    """客户确认：基础数据接口不按权限过滤，返回全部数据.

    A worker sees the full department dictionary and the full employee roster —
    not just their own row/department.
    """
    result = await login(client, uid="01001")
    params = {**common(result), "page": 1, "size": 50}
    dept = await client.post("/api/NetYf/Baseinfo/DeptQuery", json=params, headers=WORKER)
    roster = await _employee_rows(client, WORKER)

    assert dept.json()["result"]["total"] == 5
    assert len(roster) >= 490  # ~500-person factory, not own-row-only
    assert {row["dept"] for row in roster} >= {"dept-a1", "dept-a2", "dept-a5"}
    # A single-uid EmployeeQuery still narrows to that employee.
    one = await client.post(
        "/api/NetYf/Baseinfo/EmployeeQuery",
        json={**common(result), "uid": "01008", "page": 1, "size": 50},
        headers=WORKER,
    )
    rows = one.json()["result"]["list"]
    assert len(rows) == 1 and rows[0]["uid"] == "01008"


@pytest.mark.asyncio
async def test_group_leader_sees_only_own_group(client: AsyncClient) -> None:
    """01 组长查所属绑定组：工资排名只含本组成员，非本组不出现."""
    leader_result = await login(client, uid="01012")
    leader = {"Authorization": f"Bearer {leader_result['accessToken']}"}
    roster = await _employee_rows(client, leader)
    leader_row = next(row for row in roster if row["uid"] == "01012")
    group = leader_row["group"]
    assert group  # leaders belong to a group
    member_uids = {row["uid"] for row in roster if row["group"] == group}

    response = await client.post(
        "/api/NetYf/Sclzd/GongziJeOrderQuery",
        json={
            **common(leader_result),
            "page": 1,
            "size": 200,
            "queryFooter": True,
            **WINDOW,
        },
        headers=leader,
    )
    items = response.json()["result"]["list"]
    assert items, "the leader's group must have wage earners in the window"
    uids = {str(row["uid"]) for row in items}
    assert uids <= member_uids
    assert len(uids) > 1  # the group has peers, not just the leader
    # A peer outside the group (a worker of dept-a1 g01) never appears.
    assert "01001" not in uids


@pytest.mark.asyncio
async def test_worker_ranking_shows_group_peers(client: AsyncClient) -> None:
    """FR-004 员工在小组里排第几：排名接口返回本组可比对范围（组内总人数/排名）."""
    worker_result = await login(client, uid="01001")
    worker = {"Authorization": f"Bearer {worker_result['accessToken']}"}
    roster = await _employee_rows(client, worker)
    worker_row = next(row for row in roster if row["uid"] == "01001")
    member_uids = {row["uid"] for row in roster if row["group"] == worker_row["group"]}

    response = await client.post(
        "/api/NetYf/Sclzd/GongziJeOrderQuery",
        json={
            **common(worker_result),
            "page": 1,
            "size": 200,
            "queryFooter": True,
            **WINDOW,
        },
        headers=worker,
    )
    items = response.json()["result"]["list"]
    uids = {str(row["uid"]) for row in items}
    assert uids <= member_uids
    assert "01001" in uids
    assert len(uids) > 1  # 组内总人数 > 1 makes 组内排名 meaningful
    # 工资明细 stays own-data-only for the worker (only this one uid).
    detail = await client.post(
        "/api/NetYf/Sclzd/GongziMxQuery",
        json={
            **common(worker_result),
            "page": 1,
            "size": 200,
            "queryFooter": True,
            "Uid": "01001",
            "Flag": "0",
            "Type": "0,1,2",
            "scheme": "",
            **WINDOW,
        },
        headers=worker,
    )
    detail_uids = {str(row["uid"]) for row in detail.json()["result"]["list"]}
    assert detail_uids == {"01001"}


@pytest.mark.asyncio
async def test_manager_sees_bound_workshops_only(client: AsyncClient) -> None:
    """02 管理只查绑定部门/车间；跨车间绑定的管理可见多个车间."""
    single = await login(client, uid="01008")  # 一车间主任: dept-a1
    cross = await login(client, uid="01101")  # dept-a2 + dept-a4
    headers_single = {"Authorization": f"Bearer {single['accessToken']}"}
    headers_cross = {"Authorization": f"Bearer {cross['accessToken']}"}

    def call(headers: dict[str, str], bundle: dict[str, Any]) -> Any:
        return client.post(
            "/api/NetYf/Sclzd/BarcodeClQuery",
            json={**common(bundle), "page": 1, "size": 200, **WINDOW},
            headers=headers,
        )

    single_rows = (await call(headers_single, single)).json()["result"]["list"]
    cross_rows = (await call(headers_cross, cross)).json()["result"]["list"]
    assert single_rows
    assert {row["dept"] for row in single_rows} == {"dept-a1"}
    assert {row["dept"] for row in cross_rows} == {"dept-a2", "dept-a4"}


@pytest.mark.asyncio
async def test_generated_employee_login_at_scale(client: AsyncClient) -> None:
    """A plain generated worker (not an old static fixture) can log in."""
    result = await login(client, uid="01103")
    assert result["roles"] == "00"
    # Company-B still has no boss; its default login is the lowest worker.
    result_b = await login(client, app_key="APPKEY-B")
    assert result_b["user"] == "02001"
    assert result_b["roles"] == "00"
    # The token it mints authenticates business requests (tenant B).
    params = {**common(result_b), "page": 1, "size": 20, "Uid": "02001", **WINDOW}
    ysk = await client.post(
        "/api/NetYf/Sclzd/YskQuery",
        json=params,
        headers={"Authorization": f"Bearer {result_b['accessToken']}"},
    )
    assert ysk.json()["code"] == 1
