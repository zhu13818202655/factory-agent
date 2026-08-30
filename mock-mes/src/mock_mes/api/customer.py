"""Mock MES customer-shaped endpoints: all 27 interfaces (Story 5, PG-backed).

Implements the authentication chain, the ``{code, message, result, timestamp}``
envelope, ``footer`` totals, row-level filtering (M3/M19) and ``code=0`` error
scenarios. Since Story 10 every endpoint reads from PostgreSQL through
``MockMesStore``: row-level filtering (company → dept → ``move_admin_role="00"``
own-data), pagination and SQL COUNT/SUM happen in SQL; the three wage sources
are normalised in Python after SQL filtering. There is no in-memory dataset and
no fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mock_mes.identities import APP_KEY_TO_COMPANY, IDENTITIES, Record
from mock_mes.store import MockMesStore

router = APIRouter(tags=["customer"])

#: Deterministic placeholder secret; form simulation only, never a real key.
_MOCK_SECRET = "mock-secret-for-shape-simulation-only"  # nosec B105 - shape only

TOKEN_TTL_SECONDS = 7200  # M2: accessToken validity is 2 hours.


class MesError(Exception):
    """Customer-shaped failure: HTTP status plus envelope code/message."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message


def store_from(request: Request) -> MockMesStore:
    return cast(MockMesStore, request.app.state.store)


def _envelope(code: int, message: str, result: Any) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "result": result,
        "timestamp": int(time.time()),
    }


def ok(result: Any) -> dict[str, object]:
    return _envelope(1, "成功", result)


def fail(message: str) -> dict[str, object]:
    return _envelope(0, message, None)


def sign_of(app_key: str, timestamp: int) -> str:
    """Deterministic 32-char lowercase MD5 placeholder (M8 shape simulation)."""
    raw = f"{_MOCK_SECRET}app_key={app_key}timestamp={timestamp}"
    # MD5 is a shape-only placeholder, not a security primitive.
    return hashlib.md5(raw.encode()).hexdigest()  # nosec B324 # noqa: S324 - shape simulation only


# ---------------------------------------------------------------------------
# Authentication and common-parameter validation.
# ---------------------------------------------------------------------------


def bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise MesError(401, "签名无效")
    token = header.removeprefix(prefix)
    if not token.startswith("MOCK-TOKEN-") and token.count(".") != 2:
        raise MesError(401, "签名无效")
    return token


def identity_from(request: Request) -> Record:
    """Resolve the Bearer identity; unknown tokens are unauthenticated."""
    token = bearer_token(request)
    if token.startswith("MOCK-TOKEN-"):
        user = token.removeprefix("MOCK-TOKEN-")
    else:
        try:
            payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
            user = str(payload["user"])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise MesError(401, "签名无效") from None
    identity = next((item for item in IDENTITIES.values() if item["user"] == user), None)
    if identity is None:
        raise MesError(401, "签名无效")
    return identity


def check_common_params(body: dict[str, Any], *, need_sign: bool = True) -> tuple[str, int]:
    """Validate app_key/timestamp/sign; raise customer-shaped failures."""
    app_key = body.get("app_key")
    if not app_key:
        raise MesError(200, "app_key不能为空")
    if app_key not in APP_KEY_TO_COMPANY:
        raise MesError(200, "无效app_key")
    raw_timestamp = body.get("timestamp")
    try:
        if raw_timestamp is None:
            raise ValueError
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError):
        raise MesError(200, "加密信息解析失败,请检查参数是否正确") from None
    if not need_sign:
        return app_key, timestamp
    sign = body.get("sign")
    if not sign or sign != sign_of(app_key, timestamp):
        raise MesError(200, "签名无效")
    return app_key, timestamp


def require_same_tenant(identity: Record, app_key: str) -> None:
    """Company isolation: the token's company must match the AppKey's."""
    if APP_KEY_TO_COMPANY[app_key] != identity["company"]:
        raise MesError(200, "无效app_key")


# ---------------------------------------------------------------------------
# Pagination / footer assembly (data already SQL-filtered by the store).
# ---------------------------------------------------------------------------


def page_size(body: dict[str, Any]) -> tuple[int, int]:
    page = max(int(body.get("page", 1)), 1)
    size = max(int(body.get("size", 50)), 1)
    return page, size


def date_window(body: dict[str, Any]) -> tuple[date, date]:
    start = datetime.strptime(str(body.get("dates")), "%Y-%m-%d").date()
    end = datetime.strptime(str(body.get("datee")), "%Y-%m-%d").date()
    return start, end


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def sum_of(rows: list[Record], field: str) -> str:
    total = sum((_d(row.get(field, "0")) for row in rows), Decimal())
    return str(total)


# ---------------------------------------------------------------------------
# 认证与凭证（3）
# ---------------------------------------------------------------------------


@router.post("/api/system/token")
async def system_token(request: Request) -> JSONResponse:
    body = await _json_body(request)
    encrypted_app_key = body.get("app_key")
    if not encrypted_app_key:
        return JSONResponse(status_code=200, content=fail("app_key不能为空"))
    # The mock accepts any non-empty encrypted key and maps it to worker-a1's
    # tenant unless it names another seeded identity's AppKey.
    matched_app_key = next(
        (
            key
            for key, company in APP_KEY_TO_COMPANY.items()
            if str(encrypted_app_key).endswith(company.split("-")[-1])
            or str(encrypted_app_key) == key
        ),
        "APPKEY-A",
    )
    identity = next(item for item in IDENTITIES.values() if item["app_key"] == matched_app_key)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=TOKEN_TTL_SECONDS)
    iat = int(now.timestamp())
    payload = {
        "user": identity["user"],
        "uname": identity["uname"],
        "loginUserName": "",
        "loginRealName": None,
        "customId": matched_app_key,
        "userType": "小程序用户",
        "iat": iat,
        "nbf": iat,
        "exp": iat + TOKEN_TTL_SECONDS,
        "iss": "HzDuiJieServer",
        "aud": "HzDuiJieServer.ApiClients",
    }

    def b64(segment: dict[str, object]) -> str:
        raw = json.dumps(segment, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    access_token = f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(payload)}.mock-signature"
    result: dict[str, Any] = {
        "tokenType": "Bearer",
        "accessToken": access_token,
        "expiresIn": TOKEN_TTL_SECONDS,
        "expiresAt": expires_at.isoformat(),
        "user": identity["user"],
        "uname": identity["uname"],
        "loginUserName": "",
        "appkey": matched_app_key,
        "sign": sign_of(matched_app_key, iat),
        "timestamp": iat,
        "roles": [],
        "permissions": [],
    }
    return JSONResponse(content=ok(result))


@router.post("/api/print/query-sign")
async def query_sign(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, timestamp = check_common_params(body, need_sign=False)
    return JSONResponse(content=ok(sign_of(app_key, timestamp)))


@router.post("/api/print/test-permissions")
async def test_permissions(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    return JSONResponse(content=ok("调用成功"))


# ---------------------------------------------------------------------------
# 基础数据（9）
# ---------------------------------------------------------------------------


@router.post("/api/NetYf/Baseinfo/UserInfoQuery")
async def user_info_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    username = body.get("USERNAME")
    if not username:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    rows = await store_from(request).user_info(str(username))
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Baseinfo/MoveMenuQuery")
async def move_menu_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    identity = identity_from(request)
    rows = await store_from(request).list_rows("mock_move_menu", identity)
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Baseinfo/HuohaoQuery")
async def huohao_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    data = await store_from(request).huohao_all()
    return JSONResponse(
        content=ok(
            {
                "huohaoList": data,
                "hh_total": len(data),
                "huohaoTypeList": [
                    {
                        "id": "t1",
                        "bh": "T1",
                        "pbh": "0",
                        "name": "外套",
                        "name_pk": "WT",
                        "isdelete": False,
                    }
                ],
                "ht_total": 1,
            }
        )
    )


@router.post("/api/NetYf/Baseinfo/HuohaoFormQuery")
async def huohao_form_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    huohao = body.get("huohao")
    if not huohao:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    matched = await store_from(request).huohao_by_bh(str(huohao))
    return JSONResponse(
        content=ok(
            {
                "huohaoList": matched,
                "hh_total": len(matched),
                "huohaoColorList": [
                    {"id": "c1", "bh": huohao, "color": "黑色", "uploadguid": "guid-c1"}
                ],
                "hc_total": 1,
                "huohaoChimaList": [
                    {
                        "id": "s1",
                        "bh": huohao,
                        "chima": "M",
                        "banx": "标准版型",
                        "kez": "320",
                        "xs_price": "299.00",
                        "price": "120.00",
                    }
                ],
                "hs_total": 1,
            }
        )
    )


@router.post("/api/NetYf/Baseinfo/ScTypeQuery")
async def sc_type_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    rows = await store_from(request).sc_types()
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Baseinfo/RfidWorktypeQuery")
async def rfid_worktype_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    rows = await store_from(request).rfid_worktypes()
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Baseinfo/HuohaoWorktypeQuery")
async def huohao_worktype_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    check_common_params(body)
    huohao = body.get("huohao")
    if not huohao:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    rows = await store_from(request).huohao_worktypes(str(huohao))
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Baseinfo/EmployeeQuery")
async def employee_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    store = store_from(request)
    uid = body.get("uid")
    if uid:
        rows = await store.list_rows("mock_employee", identity, extra=[("uid", "=", str(uid))])
    else:
        rows = await store.list_rows("mock_employee", identity)
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Baseinfo/DeptQuery")
async def dept_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    rows = await store_from(request).list_rows("mock_dept", identity)
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


# ---------------------------------------------------------------------------
# 生产计划与制单（4）
# ---------------------------------------------------------------------------


@router.post("/api/NetYf/Plan/GridPageList")
async def plan_grid_page_list(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    page, size = page_size(body)
    result = await store_from(request).page(
        "mock_plan", identity, date_field="zhdate", start=start, end=end, page_num=page, size=size
    )
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


@router.post("/api/NetYf/Sclzd/GridPageList")
async def sclzd_grid_page_list(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    page, size = page_size(body)
    result = await store_from(request).page(
        "mock_sclzd", identity, date_field="zhdate", start=start, end=end, page_num=page, size=size
    )
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


@router.post("/api/NetYf/Sclzd/SclzdWorktypeQuery")
async def sclzd_worktype_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    dh = body.get("dh")
    if not dh:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    rows = await store_from(request).sclzd_worktypes_by_dh(identity, str(dh))
    return JSONResponse(content=ok({"list": rows, "total": len(rows)}))


@router.post("/api/NetYf/Sclzd/SclzdBarcodeQuery")
async def sclzd_barcode_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    dh = body.get("dh")
    detail_id = body.get("detailId")
    if not dh or detail_id is None:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    rows = await store_from(request).list_rows(
        "mock_barcode",
        identity,
        extra=[("dh", "=", str(dh)), ("detail_id", "=", str(detail_id))],
        order_by="id",
    )
    zb = [row for row in rows if row.get("sfzb")]
    normal = [row for row in rows if not row.get("sfzb")]
    return JSONResponse(
        content=ok(
            {
                "barcodeZb": zb,
                "totalZb": len(zb),
                "barcode": normal,
                "total": len(normal),
            }
        )
    )


# ---------------------------------------------------------------------------
# 产量与进度（6）
# ---------------------------------------------------------------------------


@router.post("/api/NetYf/Sclzd/BarcodeClQuery")
async def barcode_cl_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    page, size = page_size(body)
    result = await store_from(request).page(
        "mock_barcode_cl", identity, date_field="rq", start=start, end=end, page_num=page, size=size
    )
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


@router.post("/api/NetYf/Sclzd/HuohaoWtCLQuery")
async def huohao_wt_cl_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    scheme = body.get("scheme")
    if scheme not in ("货号工序", "工序"):
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    store = store_from(request)
    rows = await store.list_rows(
        "mock_barcode_cl", identity, date_field="rq", start=start, end=end, order_by="rq, id"
    )

    def footer(source: list[Record]) -> dict[str, str]:
        return {"sl_total": sum_of(source, "sssl")}

    grouped: dict[tuple[str, str], Record] = {}
    for row in rows:
        key = (str(row["huohao"]), str(row["worktype"]))
        entry = grouped.setdefault(
            key, {"huohao": row["huohao"], "worktype": row["worktype"], "sssl": "0"}
        )
        entry["sssl"] = str(_d(entry["sssl"]) + _d(row["sssl"]))
    items = sorted(grouped.values(), key=lambda item: (str(item["huohao"]), str(item["worktype"])))
    page, size = page_size(body)
    sliced = items[(page - 1) * size : page * size]
    result: dict[str, object] = {"list": sliced, "total": len(items)}
    if body.get("queryFooter"):
        result["footer"] = footer(items)
    return JSONResponse(content=ok(result))


@router.post("/api/NetYf/PinFeng/GridPageList")
async def pin_feng_grid_page_list(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    page, size = page_size(body)
    result = await store_from(request).page(
        "mock_pin_feng",
        identity,
        date_field="zhdate",
        start=start,
        end=end,
        page_num=page,
        size=size,
    )
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


@router.post("/api/NetYf/Sclzd/WorktypeProgressQuery")
async def worktype_progress_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    userid = body.get("userid")
    if userid is None:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    store = store_from(request)
    detail = await store.sclzd_by_id(identity, str(userid))
    if detail is None:
        return JSONResponse(content=ok({"list": [], "total": 0}))
    worktype_rows = await store.sclzd_worktypes_by_dh(identity, str(detail["dh"]))
    items: list[Record] = []
    for worktype_row in sorted(worktype_rows, key=lambda row: int(cast(int, row["sort"]))):
        scanned = await store.list_rows(
            "mock_barcode",
            identity,
            extra=[("detail_id", "=", str(userid)), ("worktype", "=", str(worktype_row["wt"]))],
            limit=1,
        )
        scan = scanned[0] if scanned else None
        items.append(
            {
                "userid": str(userid),
                "huohao": detail["huohao"],
                "color": detail["color"],
                "chima": detail["chima"],
                "baohao": detail["baohao"],
                "chuanghao": detail["chuanghao"],
                "fhsl": detail["fhsl"],
                "worktype": worktype_row["wt"],
                "name": worktype_row["wtname"],
                "uid": scan["uid"] if scan else "",
                "uname": scan["uname"] if scan else "",
                "dept": scan["dept"] if scan else "",
                "inputtime": scan["inputtime"] if scan else "",
                "cid": f"cid-{userid}-{worktype_row['wt']}" if scan else "",
                "zpsl": "0",
                "wsort": worktype_row["sort"],
            }
        )
    page, size = page_size(body)
    sliced = items[(page - 1) * size : page * size]
    return JSONResponse(content=ok({"list": sliced, "total": len(items)}))


@router.post("/api/NetYf/Sclzd/YskQuery")
async def ysk_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    page, size = page_size(body)
    store = store_from(request)
    extra: list[tuple[str, str, object]] = []
    target_uid = body.get("Uid")
    if target_uid:
        extra.append(("uid", "=", str(target_uid)))
    result = await store.page(
        "mock_ysk",
        identity,
        extra=extra,
        date_field="rq",
        start=start,
        end=end,
        page_num=page,
        size=size,
        order_by="rq, id",
    )
    sl_total = await store.sum_rows(
        "mock_ysk", identity, ("sl", "je"), extra=extra, date_field="rq", start=start, end=end
    )
    bs_total = await store.distinct_count(
        "mock_ysk", identity, "baohao", extra=extra, date_field="rq", start=start, end=end
    )
    # YskQuery has no queryFooter parameter but always returns a footer.
    return JSONResponse(
        content=ok(
            {
                "list": result.items,
                "total": result.total,
                "footer": {
                    "bs_total": bs_total,
                    "sl_total": sl_total["sl"],
                    "je_total": sl_total["je"],
                },
            }
        )
    )


@router.post("/api/NetYf/Sclzd/WskQuery")
async def wsk_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    page, size = page_size(body)
    store = store_from(request)
    result = await store.page("mock_wsk", identity, page_num=page, size=size, order_by="id")
    sl_total = await store.sum_rows("mock_wsk", identity, ("sl",))
    bs_total = await store.distinct_count("mock_wsk", identity, "baohao")
    return JSONResponse(
        content=ok(
            {
                "list": result.items,
                "total": result.total,
                "footer": {"bs_total": bs_total, "sl_total": sl_total["sl"]},
            }
        )
    )


# ---------------------------------------------------------------------------
# 工资与排名（2）
# ---------------------------------------------------------------------------


@router.post("/api/NetYf/Sclzd/GongziMxQuery")
async def gongzi_mx_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    flag = str(body.get("Flag", "0"))
    if flag not in ("0", "1"):
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    types = {part.strip() for part in str(body.get("Type", "")).split(",") if part.strip()}
    if not types <= {"0", "1", "2"}:
        raise MesError(200, "加密信息解析失败,请检查参数是否正确")
    scheme = str(body.get("scheme", ""))
    store = store_from(request)
    rows = await store.wage_rows(identity, types, start, end)
    # Story 7: honor the Uid filter for manager/boss identities (M19 row-level
    # filtering simulates the customer contract where Uid is a required param).
    target_uid = body.get("Uid")
    if target_uid:
        rows = [row for row in rows if str(row["uid"]) == str(target_uid)]
    summary_mode = scheme.lower() in ("汇总", "hz")

    def footer(source: list[Record]) -> dict[str, str]:
        return {
            "bs_total": str(len({row["baohao"] for row in source})) if source else "0",
            "fhsl_total": sum_of(source, "fhsl"),
            "sl_total": sum_of(source, "sl"),
            "je_total": sum_of(source, "je"),
        }

    if summary_mode:
        grouped: dict[tuple[str, str, str], Record] = {}
        for row in rows:
            key = (str(row["uid"]), str(row["worktype"]), str(row["type"]))
            entry = grouped.setdefault(
                key,
                {
                    "id": "",
                    "type": row["type"],
                    "rq": row["rq"],
                    "inputtime": "",
                    "uid": row["uid"],
                    "uname": row["uname"],
                    "dept": row["dept"],
                    "chuanghao": "",
                    "baohao": "",
                    "huohao": "",
                    "color": "",
                    "chima": "",
                    "worktype": row["worktype"],
                    "ischeck": 0,
                    "check_time": "",
                    "fhsl": "0",
                    "sl": "0",
                    "price": "0",
                    "je": "0",
                    "inputtime_raw": "",
                    "check_time_raw": "",
                },
            )
            entry["sl"] = str(_d(entry["sl"]) + _d(row["sl"]))
            entry["je"] = str(_d(entry["je"]) + _d(row["je"]))
        items = sorted(grouped.values(), key=lambda item: str(item["uid"]))
        page, size = page_size(body)
        sliced = items[(page - 1) * size : page * size]
        result: dict[str, object] = {"list": sliced, "total": len(items)}
        if body.get("queryFooter"):
            result["footer"] = footer(items)
        return JSONResponse(content=ok(result))

    page, size = page_size(body)
    sliced = rows[(page - 1) * size : page * size]
    for row in sliced:
        row.pop("_day", None)
    result = {"list": sliced, "total": len(rows)}
    if body.get("queryFooter"):
        result["footer"] = footer(rows)
    return JSONResponse(content=ok(result))


@router.post("/api/NetYf/Sclzd/GongziJeOrderQuery")
async def gongzi_je_order_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    store = store_from(request)
    rows = await store.wage_rows(identity, {"0", "1", "2"}, start, end)
    grouped: dict[str, Record] = {}
    for row in rows:
        uid = str(row["uid"])
        entry = grouped.setdefault(
            uid,
            {"uid": uid, "uname": row["uname"], "dept": row["dept"], "bs": "0", "je": "0"},
        )
        entry["je"] = str(_d(entry["je"]) + _d(row["je"]))
        entry["bs"] = str(int(str(entry["bs"])) + 1)

    items = sorted(grouped.values(), key=lambda item: _d(item["je"]), reverse=True)
    page, size = page_size(body)
    sliced = items[(page - 1) * size : page * size]
    result: dict[str, object] = {"list": sliced, "total": len(items)}
    if body.get("queryFooter"):
        result["footer"] = {"je_total": sum_of(items, "je")}
    return JSONResponse(content=ok(result))


# ---------------------------------------------------------------------------
# 吊挂（3）
# ---------------------------------------------------------------------------


@router.post("/api/NetYf/Dg/GridPageList")
async def dg_grid_page_list(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    page, size = page_size(body)
    result = await store_from(request).page("mock_dg", identity, page_num=page, size=size)
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


@router.post("/api/NetYf/Dg/DgZuGridPageList")
async def dg_zu_grid_page_list(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    page, size = page_size(body)
    result = await store_from(request).page("mock_dg_zu", identity, page_num=page, size=size)
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


@router.post("/api/NetYf/Dg/DgClQuery")
async def dg_cl_query(request: Request) -> JSONResponse:
    body = await _json_body(request)
    app_key, _ = check_common_params(body)
    identity = identity_from(request)
    require_same_tenant(identity, app_key)
    start, end = date_window(body)
    page, size = page_size(body)
    result = await store_from(request).page(
        "mock_dg_cl", identity, date_field="rq", start=start, end=end, page_num=page, size=size
    )
    return JSONResponse(content=ok({"list": result.items, "total": result.total}))


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON becomes a parse failure
        raise MesError(400, "加密信息解析失败,请检查参数是否正确") from None
    if not isinstance(body, dict):
        raise MesError(400, "加密信息解析失败,请检查参数是否正确")
    return cast(dict[str, Any], body)


__all__ = ["MesError", "router"]
