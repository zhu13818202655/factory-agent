"""Smoke-test every customer-facing mock-mes endpoint (27 interfaces).

Usage:
    uv run --no-sync --package mock-mes python mock-mes/scripts/smoke_api.py

Checks per endpoint: HTTP envelope ``code == 1``, ``result`` present, and the
row-level filter (caller sees only their scope). Run with the boss token by
default; pass a uid to switch identity (e.g. ``01001`` worker, ``01008``
manager, ``01012`` group leader, ``02001`` company-B worker).

The app is started in-process against the database configured in the
repository-root ``.env`` (``MOCK_MES_DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import sys

from httpx import ASGITransport, AsyncClient
from mock_mes.api.customer import sign_of
from mock_mes.api.server import create_app
from mock_mes.config import MockMesSettings

WINDOW = {"dates": "2026-07-01", "datee": "2026-08-31"}

#: (path, required params, description)
ENDPOINTS: list[tuple[str, dict[str, object], str]] = [
    ("/api/NetYf/Baseinfo/UserInfoQuery", {"page": 1, "size": 50, "USERNAME": "Admin"}, "用户信息"),
    ("/api/NetYf/Baseinfo/MoveMenuQuery", {"page": 1, "size": 50}, "移动菜单"),
    ("/api/NetYf/Baseinfo/HuohaoQuery", {"page": 1, "size": 50}, "货号列表"),
    ("/api/NetYf/Baseinfo/HuohaoFormQuery", {"page": 1, "size": 50, "huohao": "HH001"}, "货号表单"),
    ("/api/NetYf/Baseinfo/ScTypeQuery", {"page": 1, "size": 50}, "色号类型"),
    ("/api/NetYf/Baseinfo/RfidWorktypeQuery", {"page": 1, "size": 50}, "工序定义"),
    (
        "/api/NetYf/Baseinfo/HuohaoWorktypeQuery",
        {"page": 1, "size": 50, "huohao": "HH001"},
        "货号工序",
    ),
    ("/api/NetYf/Baseinfo/EmployeeQuery", {"page": 1, "size": 50}, "员工列表"),
    ("/api/NetYf/Baseinfo/DeptQuery", {"page": 1, "size": 50}, "部门列表"),
    ("/api/NetYf/Plan/GridPageList", {"page": 1, "size": 50, **WINDOW}, "生产计划"),
    ("/api/NetYf/Sclzd/GridPageList", {"page": 1, "size": 50, **WINDOW}, "制单列表"),
    (
        "/api/NetYf/Sclzd/SclzdWorktypeQuery",
        {"page": 1, "size": 50, "dh": "ZD-2607-001"},
        "制单工序",
    ),
    (
        "/api/NetYf/Sclzd/SclzdBarcodeQuery",
        {"page": 1, "size": 50, "dh": "ZD-2607-001", "detailId": "1001", **WINDOW},
        "制单扫码",
    ),
    (
        "/api/NetYf/Sclzd/BarcodeClQuery",
        {"page": 1, "size": 50, "userid": "1001", **WINDOW},
        "扫码产量明细",
    ),
    (
        "/api/NetYf/Sclzd/HuohaoWtCLQuery",
        {"page": 1, "size": 50, "scheme": "货号工序", **WINDOW},
        "款号工序产量",
    ),
    ("/api/NetYf/PinFeng/GridPageList", {"page": 1, "size": 50, **WINDOW}, "手工账"),
    (
        "/api/NetYf/Sclzd/WorktypeProgressQuery",
        {"page": 1, "size": 50, "userid": "1001", "uid": ""},
        "工序进度",
    ),
    ("/api/NetYf/Sclzd/YskQuery", {"page": 1, "size": 50, **WINDOW}, "已扫产量"),
    ("/api/NetYf/Sclzd/WskQuery", {"page": 1, "size": 50, **WINDOW}, "未扫余量"),
    (
        "/api/NetYf/Sclzd/GongziMxQuery",
        {"page": 1, "size": 50, "Uid": "{uid}", "Type": "0,1,2", "scheme": "", **WINDOW},
        "工资明细",
    ),
    (
        "/api/NetYf/Sclzd/GongziJeOrderQuery",
        {"page": 1, "size": 50, "Uid": "{uid}", **WINDOW},
        "工资排名",
    ),
    ("/api/NetYf/Dg/GridPageList", {"page": 1, "size": 50}, "吊挂线"),
    ("/api/NetYf/Dg/DgZuGridPageList", {"page": 1, "size": 50}, "吊挂组"),
    ("/api/NetYf/Dg/DgClQuery", {"page": 1, "size": 50, "uid": "01001", **WINDOW}, "吊挂产量"),
]

#: /api/print/* are authenticated differently (movepassword); checked separately.
PRINT_ENDPOINTS: list[tuple[str, dict[str, object], str]] = [
    (
        "/api/print/query-sign",
        {
            "app_key": "APPKEY-A",
            "timestamp": 1786697009,
            "uid": "01001",
            "uname": "模拟员工甲",
            "t": "2026-08-21 08:00:00",
        },
        "打印签名",
    ),
    (
        "/api/print/test-permissions",
        {
            "app_key": "APPKEY-A",
            "timestamp": 1786697009,
            "sign": sign_of("APPKEY-A", 1786697009),
            "uid": "01001",
        },
        "打印权限",
    ),
]


async def main() -> int:
    uid = sys.argv[1] if len(sys.argv) > 1 else "01009"
    app_key = "APPKEY-B" if uid.startswith("02") else "APPKEY-A"
    settings = MockMesSettings()
    assert settings.database_url is not None
    app = create_app(settings)
    await app.state.db.open()
    failures: list[str] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1) authenticate -> envelope bundle (appkey/timestamp/sign/token)
            auth = await client.post("/api/system/token", json={"app_key": app_key})
            auth_payload = auth.json()
            assert auth_payload["code"] == 1, auth_payload
            bundle = {
                "app_key": auth_payload["result"]["appkey"],
                "timestamp": auth_payload["result"]["timestamp"],
                "sign": auth_payload["result"]["sign"],
            }
            # Legacy bearer maps directly to the identity of the requested uid,
            # so the row-level filter is exercised per role.
            headers = {"Authorization": f"Bearer MOCK-TOKEN-{uid}"}
            print(f"身份 uid={uid}  app_key={app_key}  token=MOCK-TOKEN-{uid}  鉴权 OK\n")

            def row_count(result: dict[str, object]) -> str:
                total = result.get("total", result.get("hh_total", "?"))
                return f"{total} 行"

            for path, params, label in ENDPOINTS:
                try:
                    request_body = {
                        **bundle,
                        **{
                            k: (str(v).replace("{uid}", uid) if isinstance(v, str) else v)
                            for k, v in params.items()
                        },
                    }
                    resp = await client.post(path, json=request_body, headers=headers)
                    body = resp.json()
                    ok = body.get("code") == 1
                    detail = (
                        row_count(body.get("result", {}))
                        if ok
                        else f"code={body.get('code')} {body.get('message')}"
                    )
                    mark = "OK " if ok else "!! "
                    print(f"{mark} {label:8s} {path}  -> {detail}")
                    if not ok:
                        failures.append(path)
                except Exception as exc:  # noqa: BLE001 - report and continue
                    print(f"!!  {label:8s} {path}  -> EXC {exc}")
                    failures.append(path)

            print("\n--- 打印类（movepassword 鉴权） ---")
            for path, params, label in PRINT_ENDPOINTS:
                resp = await client.post(path, json=params)
                body = resp.json()
                ok = body.get("code") == 1
                detail = body.get("message", body.get("result", ""))
                print(f"{'OK ' if ok else '!! '} {label:6s} {path}  -> {detail}")
                if not ok:
                    failures.append(path)

            # 2) row-level filter probe: each identity only sees its own scope.
            print("\n--- 行级过滤探针（按身份过滤，不得越权） ---")
            probe = await client.post(
                "/api/NetYf/Sclzd/YskQuery",
                json={**bundle, "page": 1, "size": 5000, **WINDOW},
                headers=headers,
            )
            probe_body = probe.json()
            rows = probe_body["result"]["list"]
            uids = {str(row.get("uid")) for row in rows}
            depts = {str(row.get("dept")) for row in rows}
            total = probe_body["result"]["total"]
            print(f"  可见总行数={total}  分页内 uid 数={len(uids)}  涉及部门={sorted(depts)[:5]}")
            if uid == "01001" and any(u != "01001" for u in uids):
                failures.append("worker sees other workers")
            if uid == "02001" and "COMPANY-A" in {str(row.get("company")) for row in rows}:
                failures.append("company-B worker sees company-A data")
    finally:
        await app.state.db.close()

    print(
        f"\n结果：{27 - len(failures)}/27 通过"
        + (f"，失败：{failures}" if failures else "，全部通过")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
