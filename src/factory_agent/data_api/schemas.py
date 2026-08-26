"""Pydantic boundary models validating external customer MES responses.

These models exist only at the external boundary: validated rows are converted
to plain dicts before entering the DuckDB sandbox. Raw customer payload shapes
never leak past ``data_api/``.

Story 5: models mirror the customer envelope ``{code, message, result,
timestamp}`` and the list shell ``result.{list, total}`` with optional
``result.footer`` (M13/M14).
"""

# Customer field names intentionally preserve mixed casing from the upstream API.
# ruff: noqa: N815

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field


class MesEnvelope(BaseModel):
    """Customer response envelope; ``code`` is only 0 or 1 (M14)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int
    message: str
    result: Any = None
    timestamp: int


class ListResult(BaseModel):
    """List shell inside ``result``: list + total, optional footer (M13)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: list[dict[str, Any]] = Field(default_factory=lambda: [], alias="list")
    total: int
    footer: dict[str, str] | None = None


class CredentialBundleResponse(BaseModel):
    """Validated ``/api/system/token`` result (M1/M2/M11/M15)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokenType: str
    accessToken: str
    expiresIn: int
    expiresAt: str
    user: str
    uname: str
    loginUserName: str
    appkey: str
    sign: str
    timestamp: int
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


class _CustomerRow(BaseModel):
    """Base for validated customer rows.

    Unknown fields (e.g. a Mock-internal ``company`` used for row filtering) are
    tolerated and dropped; documented fields remain required and type-checked,
    so drift that changes the shape of consumed values still fails closed.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)


# ---------------------------------------------------------------------------
# Resource row models. Field names are copied verbatim from the customer
# contract; case mixing (Uid/USERNAME/detailId) is intentional.
# ---------------------------------------------------------------------------


class UserInfoRow(_CustomerRow):
    code: str
    username: str
    realname: str
    companyName: str


class MoveMenuRow(_CustomerRow):
    uid: str
    uname: str
    dept: str
    menus: tuple[dict[str, Any], ...]


class HuohaoRow(_CustomerRow):
    bh: str
    bbreed: str
    name_pk: str
    description: str
    stype: str
    huohaotype: str
    dw: str
    lpinpai: str
    isdelete: bool
    jst_huohao: str


class ScTypeRow(_CustomerRow):
    bh: str
    name: str
    name_pk: str
    sfjcj: bool
    isdelete: bool
    sfcprk: bool


class RfidWorktypeRow(_CustomerRow):
    bh: str
    name: str
    name_pk: str
    gxtype: int
    isdelete: bool
    section: str
    jc: str
    sc_type: str
    worktype_group: str
    yfgs: int
    default_price: str
    gongzi_js_type: int
    wt_sort: int
    xz_price: str
    default_working_hours: str
    vehicle_type: str


class HuohaoWorktypeRow(_CustomerRow):
    id: str
    huohao: str
    huohaoname: str
    wt: str
    wtname: str
    sort: int
    sctype: str
    sctypename: str
    sfzb: int
    using_state: int
    zhgx: int
    sfxs: int
    theoretical_work_hours: str


class EmployeeRow(_CustomerRow):
    uid: str
    uname: str
    name_pk: str
    mobile: str
    movepassword: str
    move_Login: str
    dept: str
    deptname: str
    employeeRule: str
    move_scan: int
    loginUserName: str
    zr_ck: str
    dy_gongzhong: str
    move_admin_role: str


class DeptRow(_CustomerRow):
    id: str
    name: str
    remark: str
    name_pk: str
    isdelete: bool
    sysdept: str
    company: str
    companyName: str
    pid: str


class PlanRow(_CustomerRow):
    dh: str
    zhdate: str
    finish_date: str
    jhdh: str
    hth: str
    gdy: str
    zdr: str
    zsl: str
    zdr_sh: str
    state: int
    id: str
    khddh: str
    pinpai: str
    pinpainame: str
    khid: str
    khname: str
    khhh: str
    huohao: str
    huohaoname: str
    spname: str
    color: str
    chima: str
    dw: str
    ddsl: str
    paol: str
    sl: str
    remark: str
    dept: str = ""


class SclzdRow(_CustomerRow):
    dh: str
    zhdate: str
    dddh: str
    khid: str
    khname: str
    drdg_status: int
    huohao: str
    huohaoname: str
    description: str
    sctype: str
    sctypename: str
    chuanghao: str
    cjr: str
    zdr: str
    state: int
    id: str
    baohao: str
    ganghao: str
    color: str
    chima: str
    fhsl: str
    sssl: str
    remark: str


class SclzdWorktypeRow(_CustomerRow):
    id: str
    dh: str
    huohao: str
    huohaoname: str
    wt: str
    wtname: str
    sort: int
    zhgx: int
    sfzb: int
    sctype: str
    sctypename: str


class BarcodeClRow(_CustomerRow):
    inputtime: str
    uid: str
    uname: str
    dept: str
    deptname: str
    rq: str
    chuanghao: str
    sctype: str
    sctypename: str
    baohao: str
    id: str
    huohao: str
    bbreed: str
    description: str
    color: str
    chima: str
    worktype: str
    wtname: str
    fhsl: str
    sssl: str
    sl: str
    price: str
    je: str


class HuohaoWtCLRow(_CustomerRow):
    huohao: str
    sssl: str
    worktype: str


class PinFengRow(_CustomerRow):
    dh: str
    zhdate: str
    state: int
    zhuser: str
    zhuser_sh: str
    id: str
    dept: str
    deptname: str
    uid: str
    uname: str
    huohao: str
    huohaoname: str
    ddh: str
    worktype: str
    wtname: str
    dw: str
    js: str
    sl: str
    cp: str
    chuanghao: str
    color: str
    chima: str
    price: str
    je: str
    remark: str


class WorktypeProgressRow(_CustomerRow):
    userid: str
    huohao: str
    color: str
    chima: str
    baohao: str
    chuanghao: str
    fhsl: str
    worktype: str
    name: str
    uid: str
    uname: str
    dept: str
    inputtime: str
    cid: str
    zpsl: str
    wsort: int


class YskRow(_CustomerRow):
    inputtime: str
    inputtime_raw: str
    uname: str
    uid: str
    dept: str
    id: str
    chuanghao: str
    baohao: str
    huohao: str
    color: str
    chima: str
    worktype: str
    fhsl: str
    sl: str
    price: str
    je: str
    cid: str
    sffb: int
    fbid: str


class WskRow(_CustomerRow):
    id: str
    chuanghao: str
    huohao: str
    color: str
    chima: str
    worktype: str
    sl: str
    baohao: str


class GongziMxRow(_CustomerRow):
    id: str
    type: str
    rq: str
    inputtime: str
    uid: str
    uname: str
    dept: str
    chuanghao: str
    baohao: str
    huohao: str
    color: str
    chima: str
    worktype: str
    ischeck: int
    check_time: str
    fhsl: str
    sl: str
    price: str
    je: str
    inputtime_raw: str
    check_time_raw: str


class GongziJeOrderRow(_CustomerRow):
    uid: str
    uname: str
    dept: str
    bs: str
    je: str


class DgRow(_CustomerRow):
    id: str
    dg_type: str
    dg_name: str
    dg_Server: str
    dg_Database: str
    dg_Uid: str
    dg_Pwd: str


class DgZuRow(_CustomerRow):
    id: str
    dgname: str
    xianhao: str
    zuBieName: str


class DgClRow(_CustomerRow):
    id: str
    rq: str
    dddh: str
    chuanghao: str
    huohao: str
    bbreed: str
    color: str
    chima: str
    worktype: str
    wtname: str
    uid: str
    uname: str
    dguid: str
    dguname: str
    dept: str
    dgName: str
    dgStyleNo: str
    sl: str
    price: str
    je: str
    sfjz: int


#: Row model per catalog resource name.
ROW_MODEL_BY_RESOURCE: dict[str, type[_CustomerRow]] = {
    "user_info": UserInfoRow,
    "move_menu": MoveMenuRow,
    "huohao": HuohaoRow,
    "sc_type": ScTypeRow,
    "rfid_worktype": RfidWorktypeRow,
    "huohao_worktype": HuohaoWorktypeRow,
    "employee": EmployeeRow,
    "dept": DeptRow,
    "plan": PlanRow,
    "sclzd": SclzdRow,
    "sclzd_worktype": SclzdWorktypeRow,
    "barcode_cl": BarcodeClRow,
    "huohao_wt_cl": HuohaoWtCLRow,
    "pin_feng": PinFengRow,
    "worktype_progress": WorktypeProgressRow,
    "ysk": YskRow,
    "wsk": WskRow,
    "gongzi_mx": GongziMxRow,
    "gongzi_je_order": GongziJeOrderRow,
    "dg": DgRow,
    "dg_zu": DgZuRow,
    "dg_cl": DgClRow,
}


def row_to_plain_dict(row: BaseModel) -> dict[str, Any]:
    """Convert a validated row into a plain dict for the sandbox."""
    return {key: _plain(value) for key, value in row.model_dump().items()}


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        values: tuple[Any, ...] = cast(tuple[Any, ...], value)
        return [_plain(item) for item in values]
    if isinstance(value, BaseModel):
        return row_to_plain_dict(value)
    return value


__all__ = [
    "CredentialBundleResponse",
    "ListResult",
    "MesEnvelope",
    "ROW_MODEL_BY_RESOURCE",
    "row_to_plain_dict",
]
