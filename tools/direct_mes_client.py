"""直接访问客户 MES 的多接口测试脚本（不经 factory-agent）。

用法:
    set -a; source .env; set +a
    python tools/direct_mes_client.py 00     # 00/01/02/99 四选一

每个接口函数调用前都会自动先取 token（带 60s 缓存，过期自动重取），再带
app_key/timestamp/sign 三参 + Authorization: Bearer <accessToken> 访问。

- 只需改顶部常量即可换组合/窗口/关键业务键。
- 需要外部业务键的接口（dh/huohao/userid 等）默认用顶部占位常量，函数参数可覆盖。
- 请求体一律平铺：业务参数与 app_key/timestamp/sign 同层；queryFooter 涉及接口
  按客户实测必须以布尔发送。
- 凭据值不打印。
"""

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# 可改配置
# ---------------------------------------------------------------------------
BASE = os.environ.get("FACTORY_AGENT_CANONICAL_MES_BASE_URL", "http://hzlinkbiz.ywhzsoft.com:9002")
TIMEOUT = 90.0

# 默认时间窗（“7月”）
DATES = "2026-07-01"
DATEE = "2026-07-31"

PAGE = 1
SIZE = 200
QUERY_FOOTER = True  # queryFooter 必须为布尔
TYPE = "0,1,2"  # 工资/产量三来源
FLAG = "0"
SCHEME_DETAIL = ""  # 明细
SCHEME_SUMMARY = "hz"  # 汇总

# 需要外部业务键的接口在这里填默认值（也可调用函数时传参覆盖）
USERNAME = ""  # UserInfoQuery
HUOHAO = ""  # HuohaoQuery / HuohaoFormQuery / HuohaoWorktypeQuery
DH = ""  # SclzdWorktypeQuery / SclzdBarcodeQuery
DETAIL_ID = ""  # SclzdBarcodeQuery
USERID = ""  # WorktypeProgressQuery（物料编号）
BIND_STYLE = ""  # HuohaoWtCLQuery 的 scheme（如 "货号工序"）

TOKEN_TTL_SECONDS = 2400  # 客户 timestamp 窗口

ROLES = {"00", "01", "02", "99"}

# ---------------------------------------------------------------------------
# 请求体构造
# ---------------------------------------------------------------------------
_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _as_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _BOOL_TRUE:
            return True
        if v in _BOOL_FALSE:
            return False
    return value


def _normalize(business: dict[str, Any], bool_params: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: (_as_bool(value) if key in bool_params else value)
        for key, value in business.items()
        if value is not None and value != ""
    }


def _token_cache() -> dict[str, Any]:
    if not hasattr(_token_cache, "state"):
        _token_cache.state = {}
    return _token_cache.state  # type: ignore[attr-defined]


def get_token(role: str) -> dict[str, Any]:
    """POST /api/system/token，返回 result（含 appkey/sign/timestamp/accessToken/user）。"""
    cache = _token_cache()
    entry = cache.get(role)
    if entry and int(time.time()) - entry["fetched_at"] < TOKEN_TTL_SECONDS:
        return entry["result"]
    cred = os.environ.get(f"MES_USER_CREDENTIAL_{role}")
    if not cred:
        sys.exit(f"缺少环境变量 MES_USER_CREDENTIAL_{role}（先 set -a; source .env; set +a）")
    payload = httpx.post(f"{BASE}/api/system/token", json={"app_key": cred}, timeout=TIMEOUT).json()
    if payload.get("code") != 1:
        sys.exit(
            f"[{role}] token 交换失败: code={payload.get('code')} message={payload.get('message')}"
        )
    result = payload["result"]
    cache[role] = {"result": result, "fetched_at": int(time.time())}
    return result


def _call(
    path: str,
    token: dict[str, Any],
    business: dict[str, Any],
    *,
    bool_params: tuple[str, ...] = (),
) -> dict[str, Any]:
    """带三参 + Bearer 调用一个业务接口（业务参数平铺顶层），返回完整响应 JSON。"""
    biz = _normalize(business, bool_params)
    cred = {
        "app_key": token["appkey"],
        "timestamp": token["timestamp"],
        "sign": token["sign"],
    }
    body = {**cred, **biz}
    print(f"POST {path} BODY={json.dumps(body, ensure_ascii=False)}")
    resp = httpx.post(
        f"{BASE}{path}",
        json=body,
        headers={"Authorization": f"Bearer {token['accessToken']}"},
        timeout=TIMEOUT,
    )
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"http": resp.status_code, "raw": resp.text[:500]}


def _uid(token: dict[str, Any]) -> str:
    return str(token.get("user") or "")


def _pp(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _line(payload: dict[str, Any]) -> None:
    result = payload.get("result") or {}
    rows = result.get("list") or result.get("employeeList") or result.get("deptList") or []
    print(
        f"  http/… code={payload.get('code')} message={payload.get('message')!r} "
        f"total={result.get('total')} rows={len(rows)}"
    )


# ---------------------------------------------------------------------------
# 认证 / 基础数据（9）
# ---------------------------------------------------------------------------
def user_info_query(role: str, username: str = USERNAME) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/UserInfoQuery", token, {"USERNAME": username})


def move_menu_query(role: str) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/MoveMenuQuery", token, {})


def huohao_query(role: str, huohao: str = HUOHAO) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/HuohaoQuery", token, {} if not huohao else {"huohao": huohao})


def huohao_form_query(role: str, huohao: str = HUOHAO) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/HuohaoFormQuery", token, {"huohao": huohao})


def sc_type_query(role: str) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/ScTypeQuery", token, {})


def rfid_worktype_query(role: str) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/RfidWorktypeQuery", token, {})


def huohao_worktype_query(role: str, huohao: str = HUOHAO) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/HuohaoWorktypeQuery", token, {"huohao": huohao})


def employee_query(role: str, uid: str = "") -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/EmployeeQuery", token, {} if not uid else {"uid": uid})


def dept_query(role: str) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Baseinfo/DeptQuery", token, {})


# ---------------------------------------------------------------------------
# 生产计划与制单（4）
# ---------------------------------------------------------------------------
def plan_grid_page_list(role: str, dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Plan/GridPageList",
        token,
        {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee},
    )


def sclzd_grid_page_list(role: str, dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Sclzd/GridPageList",
        token,
        {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee},
    )


def sclzd_worktype_query(role: str, dh: str = DH) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Sclzd/SclzdWorktypeQuery", token, {"dh": dh})


def sclzd_barcode_query(role: str, dh: str = DH, detail_id: str = DETAIL_ID) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Sclzd/SclzdBarcodeQuery", token, {"dh": dh, "detailId": detail_id})


# ---------------------------------------------------------------------------
# 产量与进度（6）
# ---------------------------------------------------------------------------
def barcode_cl_query(role: str, dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Sclzd/BarcodeClQuery",
        token,
        {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee},
    )


def huohao_wt_cl_query(
    role: str, scheme: str = BIND_STYLE, dates: str = DATES, datee: str = DATEE
) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Sclzd/HuohaoWtCLQuery",
        token,
        {
            "page": PAGE,
            "size": SIZE,
            "queryFooter": QUERY_FOOTER,
            "scheme": scheme,
            "dates": dates,
            "datee": datee,
        },
        bool_params=("queryFooter",),
    )


def pin_feng_grid_page_list(role: str, dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/PinFeng/GridPageList",
        token,
        {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee},
    )


def worktype_progress_query(role: str, userid: str = USERID, uid: str = "") -> dict[str, Any]:
    token = get_token(role)
    business = {"page": PAGE, "size": SIZE, "userid": userid}
    if uid:
        business["uid"] = uid
    return _call("/api/NetYf/Sclzd/WorktypeProgressQuery", token, business)


def ysk_query(role: str, uid: str = "", dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    business = {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee}
    if uid:
        business["Uid"] = uid
    return _call("/api/NetYf/Sclzd/YskQuery", token, business)


def wsk_query(role: str, dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Sclzd/WskQuery",
        token,
        {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee},
    )


# ---------------------------------------------------------------------------
# 工资与排名（2）  GongziMxQuery
# ---------------------------------------------------------------------------
def gongzi_mx_query(
    role: str,
    uid: str | None = None,
    scheme: str = SCHEME_DETAIL,
    type_: str = TYPE,
    flag: str = FLAG,
    dates: str = DATES,
    datee: str = DATEE,
    query_footer: bool = QUERY_FOOTER,
) -> dict[str, Any]:
    token = get_token(role)
    business = {
        "page": PAGE,
        "size": SIZE,
        "queryFooter": query_footer,
        "Flag": flag,
        "Type": type_,
        "scheme": scheme,
        "dates": dates,
        "datee": datee,
    }
    if uid is None:
        uid = _uid(token)
    if uid:
        business["Uid"] = uid
    return _call(
        "/api/NetYf/Sclzd/GongziMxQuery",
        token,
        business,
        bool_params=("queryFooter",),
    )


def gongzi_je_order_query(
    role: str, dates: str = DATES, datee: str = DATEE, query_footer: bool = QUERY_FOOTER
) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Sclzd/GongziJeOrderQuery",
        token,
        {"page": PAGE, "size": SIZE, "queryFooter": query_footer, "dates": dates, "datee": datee},
        bool_params=("queryFooter",),
    )


# ---------------------------------------------------------------------------
# 吊挂（3）
# ---------------------------------------------------------------------------
def dg_grid_page_list(role: str) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Dg/GridPageList", token, {"page": PAGE, "size": SIZE})


def dg_zu_grid_page_list(role: str) -> dict[str, Any]:
    token = get_token(role)
    return _call("/api/NetYf/Dg/DgZuGridPageList", token, {"page": PAGE, "size": SIZE})


def dg_cl_query(role: str, dates: str = DATES, datee: str = DATEE) -> dict[str, Any]:
    token = get_token(role)
    return _call(
        "/api/NetYf/Dg/DgClQuery",
        token,
        {"page": PAGE, "size": SIZE, "dates": dates, "datee": datee},
    )


# ---------------------------------------------------------------------------
# 打印/便捷
# ---------------------------------------------------------------------------
def role_info(role: str) -> None:
    token = get_token(role)
    print(f"[{role}] user={_uid(token)} uname={token.get('uname')} dept={token.get('dept')}")


# ---------------------------------------------------------------------------
# 示例主流程（按角色跑一遍可直连的接口；需要业务键的接口留注释，填顶部常量后启用）
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="直连客户 MES 测试（按角色）")
    parser.add_argument("role", choices=sorted(ROLES), help="00=员工 / 01=组长 / 02=管理 / 99=老板")
    args = parser.parse_args()
    role = args.role

    role_info(role)
    print("\n== 基础数据：部门 DeptQuery ==")
    _line(dept_query(role))
    print("\n== 基础数据：员工 EmployeeQuery ==")
    _line(employee_query(role))

    print(f"\n== 工资明细 GongziMxQuery（本人 {DATES} ~ {DATEE} 明细）==")
    _pp(gongzi_mx_query(role, scheme=SCHEME_DETAIL))

    print(f"\n== 工资汇总 GongziMxQuery（本人 {DATES} ~ {DATEE} 汇总）==")
    _pp(gongzi_mx_query(role, scheme=SCHEME_SUMMARY))

    print("\n== 工资排名 GongziJeOrderQuery ==")
    _line(gongzi_je_order_query(role))

    # 需要业务键的接口示例（填顶部常量后取消注释即可）：
    # _line(huohao_query(role))
    # _line(plan_grid_page_list(role))
    # _line(barcode_cl_query(role))
    # _line(pin_feng_grid_page_list(role))
    # _line(ysk_query(role))
    # _line(wsk_query(role))
    # _line(dg_grid_page_list(role))
    # _line(dg_cl_query(role))
    # _line(sclzd_worktype_query(role))
    # _line(sclzd_barcode_query(role))
    # _line(worktype_progress_query(role))
    # _line(huohao_wt_cl_query(role))


if __name__ == "__main__":
    main()
