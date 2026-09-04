"""Pydantic boundary models validating external customer MES responses.

These models exist only at the external boundary: validated rows are converted
to plain dicts before entering the DuckDB sandbox. Raw customer payload shapes
never leak past ``data_api/``.

Models mirror the customer envelope ``{code, message, result,
timestamp}`` and the list shell ``result.{list, total}`` with optional
``result.footer`` (pagination walks to ``result.total``; the envelope ``code``
is judged only as 1/0 — see ``docs/product/AI问答对外接口-整理.md`` §1).
"""

# Customer field names intentionally preserve mixed casing from the upstream API.
# ruff: noqa: N815

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class MesEnvelope(BaseModel):
    """Customer response envelope; ``code`` is only 0 or 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int
    message: str
    result: Any = None
    timestamp: int


class ListResult(BaseModel):
    """List shell inside ``result``: list + total, optional footer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: list[dict[str, Any]] = Field(default_factory=lambda: [], alias="list")
    total: int
    footer: dict[str, str] | None = None


def _normalize_role_codes(value: object) -> object:
    """The customer returns ``roles`` as a single code string ("00".."99");
    tolerate a list form as well so both shapes validate."""
    if isinstance(value, str):
        return (value,) if value else ()
    return value


class CredentialBundleResponse(BaseModel):
    """Validated ``/api/system/token`` result.

    Contract source: ``docs/product/AI问答对外接口-整理.md`` §2.1. ``roles``
    is the authoritative role code (00 员工 / 01 组长 / 02 管理 / 99 老板);
    ``dept`` is the home department.

    Bound-department set (真实环境 2026-09-04 联调确认，差异台账 #1):
    the customer's live ``/api/system/token`` returns the multi-department
    binding as a **comma-separated string** named ``manageDept`` (e.g. role 02
    returns ``dept="001"`` with ``manageDept="001,005"``). The Mock era
    ``boundDepts`` array is not emitted by the live system but is still
    accepted so both shapes validate; ``manageDept`` wins when non-empty
    (split in ``token_gateway._bound_dept_codes``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokenType: str
    accessToken: str
    expiresIn: int
    expiresAt: str
    user: str
    uname: str
    dept: str = ""
    loginUserName: str = ""
    appkey: str
    sign: str
    timestamp: int
    roles: tuple[str, ...] = ()
    boundDepts: tuple[str, ...] = ()
    #: Live-environment multi-dept binding (comma-separated); see class docs.
    manageDept: str = ""
    permissions: tuple[str, ...] = ()

    @field_validator("roles", mode="before")
    @classmethod
    def _roles_from_code(cls, value: object) -> object:
        return _normalize_role_codes(value)


class _CustomerRow(BaseModel):
    """Base for validated customer rows.

    Unknown fields (e.g. a Mock-internal ``company`` used for row filtering) are
    tolerated and dropped; documented fields remain required and type-checked,
    so drift that changes the shape of consumed values still fails closed.

    Numeric tolerance (real-environment finding, 2026-09-04): the customer MES
    returns amounts/counts/ids as JSON numbers (``id``/``fhsl``/``sl``/
    ``price``/``je`` ...) and nullable timestamps (``inputtime_raw`` /
    ``check_time_raw`` can be ``null``), whereas the Mock serialised them as
    strings. Row models declare the consumed fields as ``str`` so downstream
    compute is stable; this coercer normalises number → ``str`` and ``null`` →
    ``""`` before validation so both shapes validate identically.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_scalars(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields.get(info.field_name)
        is_str_field = field is not None and field.annotation is str
        # str-declared fields are normalised: null -> "" (nullable upstream
        # timestamps like inputtime_raw) and numbers -> str (amounts/counts/ids).
        # Non-str fields pass through untouched so their own type checks apply.
        if not is_str_field:
            return value
        if value is None:
            return ""
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            # JSON numbers: keep a stable decimal-ish string (``1.0`` -> ``1``).
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
        return value


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
    # 真实环境实测（2026-09-04）：弘兆 MES EmployeeQuery 员工行不下发 mobile /
    # movepassword / employeeRule / move_admin_role（基础数据不含账号类字段），
    # Mock 才填充。声明默认空串使两种形态都通过校验；当前无下游消费这些字段。
    mobile: str = ""
    movepassword: str = ""
    move_Login: str
    dept: str
    deptname: str
    employeeRule: str = ""
    move_scan: int
    loginUserName: str
    zr_ck: str
    dy_gongzhong: str
    move_admin_role: str = ""


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

#: Base-data resources: the catalog's 基础数据 (9) group. These operations do
#: NOT filter by role and return the full roster/directory (customer-confirmed
#: rule 4, ``docs/product/需求及方案整理.md``「客户确认结论」), so their rows
#: carry the whole tenant's uid/dept values. The role-consistency safety net
#: (Story 2) must never treat those unfiltered directory rows as the result's
#: ownership signal; the kernel excludes them when observing ownership.
BASE_DATA_RESOURCES: frozenset[str] = frozenset(
    {
        "user_info",
        "move_menu",
        "huohao",
        "huohao_form",
        "sc_type",
        "rfid_worktype",
        "huohao_worktype",
        "employee",
        "dept",
    }
)


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
