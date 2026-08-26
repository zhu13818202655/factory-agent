"""Mock MES customer-shaped API tests: auth chain, envelope, filtering."""

from __future__ import annotations

from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.server import create_app


async def login(client: AsyncClient, app_key: str = "APPKEY-A") -> dict[str, Any]:
    response = await client.post("/api/system/token", json={"app_key": app_key})
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 1
    result = payload["result"]
    return cast_dict(result)


def cast_dict(value: object) -> dict[str, Any]:
    assert isinstance(value, dict)
    return dict(cast(dict[str, Any], value))


def common(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "app_key": result["appkey"],
        "timestamp": result["timestamp"],
        "sign": result["sign"],
    }


BOSS = {"Authorization": "Bearer MOCK-TOKEN-01009"}
MANAGER = {"Authorization": "Bearer MOCK-TOKEN-01008"}
WORKER = {"Authorization": "Bearer MOCK-TOKEN-01001"}
WORKER_B = {"Authorization": "Bearer MOCK-TOKEN-02001"}
WINDOW = {"dates": "2026-07-01", "datee": "2026-08-31"}


@pytest.mark.asyncio
async def test_token_returns_full_credential_bundle() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        result = await login(client)

    assert result["tokenType"] == "Bearer"
    assert result["expiresIn"] == 7200
    assert result["roles"] == [] and result["permissions"] == []
    assert result["user"] == "01009"
    assert set(result) >= {
        "accessToken",
        "expiresAt",
        "uname",
        "loginUserName",
        "appkey",
        "sign",
        "timestamp",
    }


@pytest.mark.asyncio
async def test_token_rejects_empty_app_key() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/system/token", json={"app_key": ""})

    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "app_key不能为空"
    assert body["result"] is None


@pytest.mark.asyncio
async def test_error_scenarios_cover_customer_messages() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_row_level_filtering_three_tiers() -> None:
    """boss sees all company rows; manager dept-filtered; worker own-only."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_company_isolation_between_app_keys() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_gongzi_mx_three_sources_merge_with_footer() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        result = await login(client)
        response = await client.post(
            "/api/NetYf/Sclzd/GongziMxQuery",
            json={
                **common(result),
                "page": 1,
                "size": 50,
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
    assert types == {"扫码产量", "吊挂产量", "手工账产量"}
    footer = body["result"]["footer"]
    assert set(footer) == {"bs_total", "fhsl_total", "sl_total", "je_total"}
    # Summary rows aggregate across prices, so je >= sl × min-price sanity:
    # the footer total must equal the sum of the summary rows (M9/M13).
    from decimal import Decimal

    je_sum = sum((Decimal(row["je"]) for row in body["result"]["list"]), Decimal())
    assert Decimal(footer["je_total"]) == je_sum


@pytest.mark.asyncio
async def test_worktype_progress_consistency_with_barcodes() -> None:
    """uid non-empty marks the worktype scanned (M6); others stay empty."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_ysk_and_wsk_return_footer_without_query_footer_param() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_all_27_endpoints_are_registered() -> None:
    app = create_app()
    paths = set(app.openapi()["paths"])
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
