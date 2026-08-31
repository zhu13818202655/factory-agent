"""Interactive tester for the mock-mes customer API (27 interfaces).

Usage (start the service first, then in another terminal):

    uv run --package mock-mes mock-mes                     # terminal 1: service
    uv run --package mock-mes python mock-mes/scripts/interactive_api.py   # terminal 2

Optionally override the base URL:

    MOCK_MES_URL=http://127.0.0.1:8010 uv run --package mock-mes python mock-mes/scripts/interactive_api.py

Flow:
    1) On startup it asks for a uid (identity), derives the app_key, and calls
       ``POST /api/system/token`` to grab the ``appkey/timestamp/sign`` bundle.
    2) It then shows a numbered menu of every endpoint. Type a number to fire
       that single endpoint; the envelope (code/message/total) and the first
       few rows are printed back.

Commands inside the REPL: ``l`` list, ``a`` run all, ``i`` switch identity,
``q`` quit. After picking an endpoint, press Enter to send the default params,
or paste a JSON dict (or ``k=v k=v`` pairs) to override them.

Stdlib-only (urllib), so it also runs with a plain ``python3``.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, cast

BASE_URL = os.environ.get("MOCK_MES_URL", "http://127.0.0.1:8010").rstrip("/")

WINDOW = {"dates": "2026-07-01", "datee": "2026-08-31"}

#: Common test identities (from mock_mes.identities).
UID_HINTS = {
    "01001": "普通员工（A厂）",
    "01008": "厂长/管理（A厂）",
    "01009": "老板（A厂）",
    "01012": "组长（A厂）",
    "02001": "普通员工（B厂，验证租户隔离）",
}

#: (path, required params, description). ``{uid}`` is substituted with the
#: current identity at request time.
ENDPOINTS: list[tuple[str, dict[str, object], str]] = [
    ("/api/NetYf/Baseinfo/UserInfoQuery", {"page": 1, "size": 50, "USERNAME": "Admin"}, "用户信息"),
    ("/api/NetYf/Baseinfo/MoveMenuQuery", {"page": 1, "size": 50}, "移动菜单"),
    ("/api/NetYf/Baseinfo/HuohaoQuery", {"page": 1, "size": 50}, "货号列表"),
    ("/api/NetYf/Baseinfo/HuohaoFormQuery", {"page": 1, "size": 50, "huohao": "HH001"}, "货号表单"),
    ("/api/NetYf/Baseinfo/ScTypeQuery", {"page": 1, "size": 50}, "色号类型"),
    ("/api/NetYf/Baseinfo/RfidWorktypeQuery", {"page": 1, "size": 50}, "工序定义"),
    ("/api/NetYf/Baseinfo/HuohaoWorktypeQuery", {"page": 1, "size": 50, "huohao": "HH001"}, "货号工序"),
    ("/api/NetYf/Baseinfo/EmployeeQuery", {"page": 1, "size": 50}, "员工列表"),
    ("/api/NetYf/Baseinfo/DeptQuery", {"page": 1, "size": 50}, "部门列表"),
    ("/api/NetYf/Plan/GridPageList", {"page": 1, "size": 50, **WINDOW}, "生产计划"),
    ("/api/NetYf/Sclzd/GridPageList", {"page": 1, "size": 50, **WINDOW}, "制单列表"),
    ("/api/NetYf/Sclzd/SclzdWorktypeQuery", {"page": 1, "size": 50, "dh": "ZD-2607-001"}, "制单工序"),
    (
        "/api/NetYf/Sclzd/SclzdBarcodeQuery",
        {"page": 1, "size": 50, "dh": "ZD-2607-001", "detailId": "1001", **WINDOW},
        "制单扫码",
    ),
    ("/api/NetYf/Sclzd/BarcodeClQuery", {"page": 1, "size": 50, "userid": "1001", **WINDOW}, "扫码产量明细"),
    (
        "/api/NetYf/Sclzd/HuohaoWtCLQuery",
        {"page": 1, "size": 50, "scheme": "货号工序", **WINDOW},
        "款号工序产量",
    ),
    ("/api/NetYf/PinFeng/GridPageList", {"page": 1, "size": 50, **WINDOW}, "手工账"),
    ("/api/NetYf/Sclzd/WorktypeProgressQuery", {"page": 1, "size": 50, "userid": "1001", "uid": ""}, "工序进度"),
    ("/api/NetYf/Sclzd/YskQuery", {"page": 1, "size": 50, **WINDOW}, "已扫产量"),
    ("/api/NetYf/Sclzd/WskQuery", {"page": 1, "size": 50, **WINDOW}, "未扫余量"),
    ("/api/NetYf/Sclzd/GongziMxQuery", {"page": 1, "size": 50, "Uid": "{uid}", "Type": "0,1,2", "scheme": "", **WINDOW}, "工资明细"),
    ("/api/NetYf/Sclzd/GongziJeOrderQuery", {"page": 1, "size": 50, "Uid": "{uid}", **WINDOW}, "工资排名"),
    ("/api/NetYf/Dg/GridPageList", {"page": 1, "size": 50}, "吊挂线"),
    ("/api/NetYf/Dg/DgZuGridPageList", {"page": 1, "size": 50}, "吊挂组"),
    ("/api/NetYf/Dg/DgClQuery", {"page": 1, "size": 50, "uid": "01001", **WINDOW}, "吊挂产量"),
]

#: /api/print/* authenticate differently (movepassword chain, no bearer).
#: ``test-permissions`` needs a live sign, fetched via ``query-sign`` on demand.
PRINT_ENDPOINTS: list[tuple[str, dict[str, object], str]] = [
    (
        "/api/print/query-sign",
        {"uid": "{uid}", "uname": "模拟员工甲", "t": "2026-08-21 08:00:00"},
        "打印签名",
    ),
    ("/api/print/test-permissions", {"uid": "{uid}"}, "打印权限"),
]

ALL_ENDPOINTS = ENDPOINTS + PRINT_ENDPOINTS

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def post_json(path: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> tuple[int, object]:
    """POST JSON to the running service; return (http_status, parsed_body)."""
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except (ValueError, UnicodeDecodeError):
            return exc.code, exc.reason
    except urllib.error.URLError as exc:
        raise ConnectionError(f"无法连接 {BASE_URL}，请先启动服务：uv run --package mock-mes mock-mes") from exc


def as_json_dict(value: object) -> dict[str, Any] | None:
    """Return ``value`` as a JSON object (str keys), or None if it isn't one.

    Needed because ``isinstance(x, dict)`` narrows to ``dict[Unknown, Unknown]``
    under strict type checking, which then pollutes everything downstream.
    """
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


def as_json_list(value: object) -> list[Any] | None:
    """Return ``value`` as a JSON array, or None if it isn't one."""
    if isinstance(value, list):
        return cast("list[Any]", value)
    return None


def authenticate(app_key: str) -> dict[str, object]:
    """Step 1: exchange app_key for the app_key/timestamp/sign bundle."""
    status, body = post_json("/api/system/token", {"app_key": app_key})
    data = as_json_dict(body)
    if data is None or data.get("code") != 1:
        raise SystemExit(f"获取 token 失败（HTTP {status}）：{json.dumps(body, ensure_ascii=False)}")
    result = as_json_dict(data.get("result"))
    if result is None:
        raise SystemExit(f"获取 token 失败（HTTP {status}）：result 缺失或非对象")
    print(f"鉴权 OK：appkey={result['appkey']}  timestamp={result['timestamp']}  sign={result['sign']}")
    #: The token endpoint returns ``appkey`` but requests must send ``app_key``.
    return {
        "app_key": result["appkey"],
        "timestamp": result["timestamp"],
        "sign": result["sign"],
    }


def print_sign(app_key: str, timestamp: int, uid: str) -> str:
    """Fetch the movepassword-style sign via /api/print/query-sign."""
    _, body = post_json("/api/print/query-sign", {"app_key": app_key, "timestamp": timestamp, "uid": uid})
    data = as_json_dict(body)
    if data is not None and data.get("code") == 1:
        return str(data["result"])
    raise RuntimeError(f"query-sign 失败：{json.dumps(body, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def summarize(path: str, label: str, status: int, body: object, max_rows: int = 3) -> bool:
    """Print one endpoint result; return True when the envelope says success."""
    data = as_json_dict(body)
    if data is None:
        print(f"  !! {label} {path} -> HTTP {status} 非法响应：{body!r}")
        return False
    ok = data.get("code") == 1
    result: object = data.get("result")
    if not ok:
        print(f"  !! {label} {path} -> HTTP {status} code={data.get('code')} {data.get('message')}")
        return False

    line = f"  OK {label} {path} -> HTTP {status}"
    rows: list[Any] = []
    result_obj = as_json_dict(result)
    result_list = as_json_list(result)
    if result_obj is not None:
        total = result_obj.get("total", result_obj.get("hh_total"))
        if total is not None:
            line += f"  total={total}"
        listed = as_json_list(result_obj.get("list"))
        rows = listed if listed is not None else []
        if not rows and total is None:
            line += f"  result={json.dumps(result_obj, ensure_ascii=False)[:300]}"
    elif result_list is not None:
        rows = result_list
        line += f"  {len(result_list)} 项"
    else:
        line += f"  result={json.dumps(result, ensure_ascii=False)[:300]}"
    print(line)
    for row in rows[:max_rows]:
        print("     " + json.dumps(row, ensure_ascii=False)[:400])
    if len(rows) > max_rows:
        print(f"     ...（其余 {len(rows) - max_rows} 行省略，可加大 size 参数查看）")
    return True


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------


def ask_uid() -> str:
    print("可用身份（uid）：")
    for uid, hint in UID_HINTS.items():
        print(f"  {uid}  {hint}")
    while True:
        uid = input("选择身份 uid [01009]: ").strip() or "01009"
        if uid.isdigit() and len(uid) == 5:
            return uid
        print("uid 应为 5 位数字，请重试。")


def parse_override(raw: str) -> dict[str, object]:
    """Accept a JSON dict or ``k=v k=v`` pairs as param overrides."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = as_json_dict(json.loads(raw))
        if parsed is None:
            raise ValueError("JSON 覆盖参数必须是对象")
        return parsed
    override: dict[str, object] = {}
    for chunk in raw.split():
        key, _, value = chunk.partition("=")
        if key:
            override[key] = value
    return override


def build_body(index: int, bundle: dict[str, object], uid: str) -> dict[str, object]:
    """Assemble the request body for endpoint ``index`` under identity ``uid``."""
    _path, params, _label = ALL_ENDPOINTS[index]
    substituted = {
        k: (str(v).replace("{uid}", uid) if isinstance(v, str) else v) for k, v in params.items()
    }
    if index < len(ENDPOINTS):
        return {**bundle, **substituted}
    # print/* chain: fresh timestamp + sign fetched via query-sign.
    timestamp = int(time.time())
    sign = print_sign(str(bundle["app_key"]), timestamp, uid)
    return {"app_key": bundle["app_key"], "timestamp": timestamp, "sign": sign, **substituted}


def run_endpoint(index: int, bundle: dict[str, object], uid: str) -> bool:
    path, params, label = ALL_ENDPOINTS[index]
    default_body = build_body(index, bundle, uid)
    raw = input(
        f"测试 [{index}] {label} {path}\n"
        f"默认参数: {json.dumps(params, ensure_ascii=False)}\n"
        "回车直接发送，或输入覆盖参数（JSON 或 k=v 形式）: "
    ).strip()
    try:
        override = parse_override(raw)
    except ValueError as exc:
        print(f"  !! 参数解析失败：{exc}")
        return False

    body = {**default_body, **override}
    is_print = index >= len(ENDPOINTS)
    headers = None if is_print else {"Authorization": f"Bearer MOCK-TOKEN-{uid}"}
    status, resp = post_json(path, body, headers)
    return summarize(path, label, status, resp)


def menu() -> None:
    print("\n命令：编号=测单个接口  l=列表  a=全部跑一遍  i=切换身份  q=退出")
    for i, (path, _, label) in enumerate(ALL_ENDPOINTS):
        tag = "print" if i >= len(ENDPOINTS) else "     "
        print(f"  [{i:2d}] {tag} {label}  {path}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    uid = ask_uid()
    app_key = "APPKEY-B" if uid.startswith("02") else "APPKEY-A"
    print(f"身份 uid={uid}  app_key={app_key}  token=MOCK-TOKEN-{uid}")
    bundle = authenticate(app_key)
    menu()

    while True:
        try:
            choice = input("\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice in ("q", "quit", "exit"):
            break
        if choice in ("", "l", "list"):
            menu()
        elif choice in ("a", "all"):
            failures = 0
            for i, (path, _params, label) in enumerate(ALL_ENDPOINTS):
                try:
                    body = build_body(i, bundle, uid)
                    headers = None if i >= len(ENDPOINTS) else {"Authorization": f"Bearer MOCK-TOKEN-{uid}"}
                    status, resp = post_json(path, body, headers)
                    if not summarize(path, label, status, resp, max_rows=0):
                        failures += 1
                except Exception as exc:  # noqa: BLE001 - report and continue
                    print(f"  !! {label} {path} -> EXC {exc}")
                    failures += 1
            total = len(ALL_ENDPOINTS)
            print(f"\n结果：{total - failures}/{total} 通过" + (f"，失败 {failures} 个" if failures else "，全部通过"))
        elif choice in ("i", "identity"):
            uid = ask_uid()
            app_key = "APPKEY-B" if uid.startswith("02") else "APPKEY-A"
            print(f"身份切换为 uid={uid}  app_key={app_key}")
            bundle = authenticate(app_key)
        elif choice.isdigit() and 0 <= int(choice) < len(ALL_ENDPOINTS):
            run_endpoint(int(choice), bundle, uid)
        else:
            print("无效输入。命令：编号 / l / a / i / q")
    print("再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
