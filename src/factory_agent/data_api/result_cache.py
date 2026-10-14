"""Two validated tenant-owner detail snapshots serve bounded caller views.

Only complete default detail may enter the shared snapshot. Scope, business
filters, time windows and reviewed progress grains are applied when reading.
Credentials never enter values or logs. Shadow verification compares the final
recipe result, and every cache or derivation failure falls back to caller MES.
"""

import asyncio
import hashlib
import json
import logging
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol, cast

from factory_agent.data_api.progress_detail import (
    ProgressDerivationError,
    aggregate_progress_rows,
    derive_progress_rows,
    normalize_progress_date,
)
from factory_agent.domain import NarrowedFilters, Role, local_calendar_dates
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.ports.contracts import ResourceFetchResult

_LOGGER = logging.getLogger("factory_agent.data_api.result_cache")

#: 结果集缓存域（AuthAwareCache 的缓存域之一；键前缀 ``fa:…:v2_result:``）。
RESULT_CACHE_DOMAIN = "v2_result"

#: 两族均存经过完整性验证的默认明细；分组是读取视图，不是底料键维度。
PAYROLL_OPERATION = "GongziClQuery"
PROGRESS_OPERATION = "ScjdQuery"

#: 缓存值结构版本（附录 F.1 ``v``）；派生逻辑或值结构变更时 bump。
RESULT_SET_VERSION = 4
DETAIL_GRAIN = "default-detail-v4"
_FORBIDDEN_ROW_FIELDS = frozenset({"app_key", "accesstoken", "sign", "movepassword"})
_FORBIDDEN_REQUEST_PARAMS = _FORBIDDEN_ROW_FIELDS | frozenset(
    {"timestamp", "uid", "dept", "tenant_id", "employee_ids", "dept_ids"}
)


def _has_credential_fields(keys: Iterable[object]) -> bool:
    return any(str(key).lower().removesuffix("_total") in _FORBIDDEN_ROW_FIELDS for key in keys)


#: 进度时间锚来自接口返回的流程卡制单日期，规范化后仍使用原字段。
PROGRESS_DATE_COLUMN = "rq"

#: 全厂范围签名（键内 ``scope_sig`` 位）：缓存只有一份，范围过滤发生在读取时。
OWNER_SCOPE_SIGNATURE = "owner-99"

#: 进度侧空 dept 行是契约行为（未扫码工序行，附录 A.4）：老板视角保留；
#: 任何部门视角由范围过滤自然排除。
EMPTY = ""

#: ``v2_result`` 域的 scope 签名：缓存只有一份全厂（99 老板）行集，
#: 提问者的范围是读取时的过滤参数，永不进键（§4.2）。
V2_RESULT_SCOPE_FINGERPRINT = OWNER_SCOPE_SIGNATURE

#: ``_today()`` 的默认时区。仅为向后兼容保留（历史行为是 UTC）：**生产必须由
#: bootstrap 传入 ``factory_timezone``**。
#:
#: 为什么不能各算各的：预取窗口 ``[今天 − N, 昨天]`` 是键的一部分。写侧
#: （``prefetch.run_once``）用 ``factory_timezone`` 算「今天」，读侧（``read``）
#: 若用 UTC 算，则每天 UTC 与部署时区跨日的那个时段（GMT+8 下是 00:00–08:00）
#: 两侧差一天 → 读侧探测的键**写侧从未写过** → 预取全部落空、退回实时。
#: 两侧必须用同一个「今天」，且以部署时区为准（「止于昨天吃掉夜间补录」是本地语义）。
_DEFAULT_TIMEZONE: tzinfo = timezone.utc


class CacheableRequest(Protocol):
    """取数请求的最小只读视图（避免 ``data_api → execution`` 依赖）。

    ``ExecutionRequest`` 在运行时满足本协议；装饰器只读这三个字段。
    """

    @property
    def operation_id(self) -> str: ...

    @property
    def time_range(self) -> tuple[datetime, datetime]: ...

    @property
    def role(self) -> Role | None: ...


class ResultCacheMode(str, Enum):
    """§4.6① 模式开关：off 完全走实时（与无缓存一致）。"""

    OFF = "off"
    READ = "read"
    READWRITE = "readwrite"
    SHADOW = "shadow"

    @classmethod
    def parse(cls, value: str) -> "ResultCacheMode":
        try:
            return cls(value.strip().lower())
        except ValueError as error:
            raise ValueError(
                f"invalid result cache mode {value!r}: expected one of off/read/readwrite/shadow"
            ) from error

    @property
    def reads(self) -> bool:
        return self is not ResultCacheMode.OFF

    @property
    def writes(self) -> bool:
        return self in (ResultCacheMode.READWRITE, ResultCacheMode.SHADOW)

    @property
    def verifies(self) -> bool:
        """shadow 模式下命中缓存后仍实时取一次做逐行比对。"""
        return self is ResultCacheMode.SHADOW


@dataclass(frozen=True, slots=True)
class ResultCachePolicy:
    """F.2 TTL 分档与回溯窗口。"""

    backtrack_days: int = 365
    #: 未结算窗口（含今天）：只兜「同一天内连问」。
    unsettled_ttl_seconds: int = 90
    #: 超出回溯范围 / 预取未命中时的回填：惰性回填，靠 TTL 兜倒补。
    settled_ttl_seconds: int = 86400
    #: 影子比对 / footer 对账的浮点容差（相对偏差）。
    float_tolerance: float = 1e-6


def window_category(dates: str, datee: str, today: str, backtrack_days: int = 365) -> str:
    """§4.5 三条路径的窗口分类（dates/datee/today 均为 ISO 日期）。"""
    if datee >= today:
        return "unsettled"
    backtrack_start = _shift_iso(today, -backtrack_days)
    if dates < backtrack_start:
        return "out_of_range"
    return "settled"


def _shift_iso(iso_date: str, days: int) -> str:
    base = date.fromisoformat(iso_date)
    return (base + timedelta(days=days)).isoformat()


def normalize_params(
    operation_id: str,
    extra_params: Mapping[str, Any],
    dates: str,
    datee: str,
) -> dict[str, object]:
    """键归一化（F.3）：只含真正下发给 MES 的参数。

    - ``groupbyFields`` 是读取视图，不参与明细底料键；
    - 枚举取值（``Type``）排序去重；
    - 排序参数不存在——排序从不下发 MES（§3.2⑤）。
    """
    normalized: dict[str, object] = {"dates": dates, "datee": datee}
    for key in sorted(extra_params):
        if key in {"dates", "datee", "groupbyFields", "orderbyFields", "orderByType"}:
            continue
        value: Any = extra_params[key]
        if key == "Type" and isinstance(value, str):
            normalized[key] = ",".join(sorted({part for part in value.split(",") if part}))
        else:
            normalized[key] = value
    return normalized


def cacheable_grain(operation_id: str, extra_params: Mapping[str, Any]) -> bool:
    """Both reviewed V2 families derive requested views from default detail."""
    return operation_id in {PAYROLL_OPERATION, PROGRESS_OPERATION}


def detail_params(extra_params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in extra_params.items()
        if key not in {"groupbyFields", "orderbyFields", "orderByType"}
    }


# ---------------------------------------------------------------------------
# F.1 缓存值结构：列式紧凑 JSON


def encode_result_set(
    *,
    operation_id: str,
    rows: tuple[Mapping[str, Any], ...],
    footer: Mapping[str, str] | None,
    window: tuple[str, str],
    filters: Mapping[str, object],
    derived: tuple[str, ...],
    generated_at: str,
    source: str,
) -> bytes:
    columns = sorted({str(key) for row in rows for key in row})
    group_items = cast("Any", dict(filters).get("groupby", ()))
    payload = {
        "v": RESULT_SET_VERSION,
        "api": operation_id,
        "window": {"dates": window[0], "datee": window[1]},
        "filters": dict(filters),
        "groupby": sorted(str(key) for key in group_items),
        "derived": list(derived),
        "columns": columns,
        "rows": [[row.get(column, EMPTY) for column in columns] for row in rows],
        "footer": dict(footer) if footer else {},
        "generated_at": generated_at,
        "source": source,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_result_set(raw: bytes) -> dict[str, Any] | None:
    """任何结构问题返回 None → 调用方回退实时（键不匹配 / 行损坏同法）。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    payload = cast("dict[str, Any]", payload)
    if payload.get("v") != RESULT_SET_VERSION:
        return None
    api = payload.get("api")
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(api, str) or not isinstance(columns, list) or not isinstance(rows, list):
        return None
    if not all(isinstance(column, str) for column in cast("list[Any]", columns)):
        return None
    columns = cast("list[str]", columns)
    if len(set(columns)) != len(columns) or _has_credential_fields(columns):
        return None
    footer = payload.get("footer")
    derived = payload.get("derived")
    if not isinstance(footer, dict) or not isinstance(derived, list):
        return None
    footer_values = cast("dict[Any, Any]", footer)
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in footer_values.items()
    ):
        return None
    if _has_credential_fields(footer_values):
        return None
    if not all(isinstance(column, str) for column in cast("list[Any]", derived)):
        return None
    decoded: list[dict[str, str]] = []
    for raw_row in cast("list[Any]", rows):
        if not isinstance(raw_row, list):
            return None
        row = cast("list[Any]", raw_row)
        if len(row) != len(columns):
            return None
        if any(
            value is not None and not isinstance(value, (str, int, float, bool)) for value in row
        ):
            return None
        decoded.append(
            {
                column: (EMPTY if value is None else str(value))
                for column, value in zip(columns, row)
            }
        )
    payload["decoded_rows"] = decoded
    return payload


# ---------------------------------------------------------------------------
# 回写门禁（§4.6③）与影子比对（§4.6②）


#: Ratio footers cannot be summed from row percentages. ``wcl`` is checked by
#: recomputing received / issued totals when both contribution columns exist.
NON_ADDITIVE_FOOTER_COLUMNS: frozenset[str] = frozenset({"wcl"})


def footer_reconciles(
    rows: tuple[Mapping[str, Any], ...],
    footer: Mapping[str, str] | None,
    *,
    tolerance: float = 1e-6,
) -> bool:
    """footer 与本地聚合对账：整数列严格相等、浮点列用容差。

    footer 键约定 = ``<聚合列>_total``。**只对「本地能真正对账的键」下判断**——
    无法对账不等于对账失败，把无法对账的键当成 0 参与比较，会让门禁必然不通过：

    - 没有 footer（或没有可对账的键）→ 通过；
    - 键对应的列**不在行集里** → 本地无从求和，跳过。实测 GongziClQuery 的
      ``sssl_total`` 就是这个情形：footer 报 29305439，而 173572 行明细里根本没
      有 ``sssl`` 这一列（与 ``recomputed_footer`` 对不可复算键「直接丢弃」的
      策略一致）；
    - 比率列按其分子、分母重算；缺少这两列时跳过，不能求和行比率。

    剩下的键仍然严格判定：整数列零容差、浮点列相对容差。
    """
    if not footer:
        return True
    wanted = {key[: -len("_total")] for key in footer if key.endswith("_total")}
    present: set[str] = set()
    for row in rows:
        present.update(str(key) for key in row)
        if wanted.issubset(present):
            break
    for key, expected in footer.items():
        if not key.endswith("_total"):
            continue
        column = key[: -len("_total")]
        if not rows:
            try:
                if Decimal(str(expected)) != 0:
                    return False
            except InvalidOperation:
                return False
            continue
        if column in NON_ADDITIVE_FOOTER_COLUMNS:
            if not {"sssl", "fhsl"} <= present:
                continue
            ratio = recomputed_footer(rows, {key: expected})
            if ratio is None:
                return False
            try:
                if Decimal(ratio[key]) != Decimal(str(expected)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ):
                    return False
            except InvalidOperation:
                return False
            continue
        if column not in present:
            continue
        total = Decimal("0")
        for row in rows:
            value = row.get(column)
            if value is None or value == EMPTY:
                continue
            try:
                total += Decimal(str(value))
            except InvalidOperation:
                return False
        try:
            expected_decimal = Decimal(str(expected))
        except InvalidOperation:
            return False
        if not expected_decimal.is_finite() or not total.is_finite():
            return False
        if column != "je" and expected_decimal == expected_decimal.to_integral_value():
            # 整数列严格相等（容差为零）。
            if total != expected_decimal:
                return False
        else:
            scale = max(abs(float(expected_decimal)), 1.0)
            if abs(float(total) - float(expected_decimal)) > tolerance * scale:
                return False
    return True


@dataclass(frozen=True, slots=True)
class ShadowVerdict:
    """Raw-row diagnostic verdict, separate from final recipe shadow validation."""

    consistent: bool
    tie_order_only: bool


def recomputed_footer(
    rows: tuple[Mapping[str, Any], ...],
    footer: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """切片后本地重算 footer：``<col>_total`` = served 行集该列之和。

    预取键的 footer 对应**整预取窗口**；范围过滤 / 时间截取后的行集范围已经
    不同，沿用会让 FR-002/012 的 footer 对账误报。权威对账已在写入门禁完成
    （写入时 footer 与整窗行集对过账），serve 时的 footer 只需与本次返回的
    行集自洽。无法重算的键（列缺失 / 值不可解析）直接丢弃。
    """
    if not footer:
        return None
    out: dict[str, str] = {}
    present = {str(key) for row in rows for key in row}
    for key in footer:
        if not key.endswith("_total"):
            continue
        column = key[: -len("_total")]
        if rows and column not in present and column not in NON_ADDITIVE_FOOTER_COLUMNS:
            continue
        if column in NON_ADDITIVE_FOOTER_COLUMNS:
            if {"sssl", "fhsl"} <= present:
                try:
                    numerator = sum(
                        (Decimal(str(row.get("sssl") or "0")) for row in rows), Decimal(0)
                    )
                    denominator = sum(
                        (Decimal(str(row.get("fhsl") or "0")) for row in rows), Decimal(0)
                    )
                    ratio = numerator / denominator * 100 if denominator else Decimal(0)
                    out[key] = str(ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                except (InvalidOperation, ZeroDivisionError):
                    continue
            continue
        total = Decimal("0")
        parseable = True
        for row in rows:
            value = row.get(column)
            if value is None or value == EMPTY:
                continue
            try:
                total += Decimal(str(value))
            except InvalidOperation:
                parseable = False
                break
        if parseable:
            out[key] = str(total)
    return out or None


def shadow_compare(
    cached_rows: tuple[Mapping[str, Any], ...],
    cached_footer: Mapping[str, str] | None,
    realtime: ResourceFetchResult,
) -> ShadowVerdict:
    """逐行「同位」比对：行序也要一致，不只是集合相等。"""
    footer_ok = (cached_footer or {}) == (realtime.footer or {})
    realtime_rows = tuple(dict(row) for row in realtime.rows)
    cached_rows_normal = tuple(dict(row) for row in cached_rows)
    positional = _normalize_rows(cached_rows_normal) == _normalize_rows(realtime_rows)
    if positional and footer_ok:
        return ShadowVerdict(consistent=True, tie_order_only=False)
    as_multiset = _row_multiset(cached_rows_normal) == _row_multiset(realtime_rows)
    if as_multiset and footer_ok:
        # 数值全等、仅并列项行序不同：仍计不一致并告警，但标注 tie_order_only。
        return ShadowVerdict(consistent=False, tie_order_only=True)
    return ShadowVerdict(consistent=False, tie_order_only=False)


def _normalize_rows(rows: tuple[Mapping[str, Any], ...]) -> list[str]:
    return [json.dumps(dict(row), sort_keys=True, ensure_ascii=False) for row in rows]


def _row_multiset(rows: tuple[Mapping[str, Any], ...]) -> list[str]:
    return sorted(_normalize_rows(rows))


# ---------------------------------------------------------------------------
# 读取时的范围过滤与时间锚截取（§4.5）


def scope_filtered_rows(
    rows: tuple[Mapping[str, Any], ...],
    operation_id: str,
    filters: NarrowedFilters,
    role: Role | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """按提问者范围过滤缓存行（提问者的范围不进键，读取时过滤）。

    - ``99``（filters 无范围字段）：不过滤，全厂即其范围；
    - ``01`` / ``02``：``dept ∈ filters.dept_ids``（进度侧空 dept 组随之排除，
      附录 A.4：它在老板视角有意义，在任何部门视角都不该出现）;
    - ``00``：``uid == 本人``。
    """
    employee_ids = filters.employee_ids
    dept_ids = (
        filters.requested_dept_ids if role in (Role.OWNER, Role.EMPLOYEE) else filters.dept_ids
    )
    if employee_ids is None and dept_ids is None:
        return rows

    def keep(row: Mapping[str, Any]) -> bool:
        if dept_ids is not None:
            dept = str(row.get("dept") or EMPTY)
            # 空 dept 的行属于「未扫码工序组」，任何部门视角都不出现。
            if dept == EMPTY or dept not in {str(item) for item in dept_ids}:
                return False
        if employee_ids is not None:
            uid = str(row.get("uid") or EMPTY)
            if uid == EMPTY or uid not in {str(item) for item in employee_ids}:
                return False
        return True

    return tuple(row for row in rows if keep(row))


def time_anchor_column(operation_id: str, extra_params: Mapping[str, Any]) -> str:
    """时间锚列（附录 A.5）：工资 Flag=0 按 inputdate / Flag=1 按 check_time；
    进度使用返回的流程卡制单日期 ``rq``。"""
    if operation_id == PROGRESS_OPERATION:
        return PROGRESS_DATE_COLUMN
    if str(extra_params.get("Flag") or "0").strip() == "1":
        return "check_time"
    return "inputdate"


def time_sliced_rows(
    rows: tuple[Mapping[str, Any], ...],
    anchor_column: str,
    window: tuple[str, str],
) -> tuple[Mapping[str, Any], ...]:
    """本地按时间锚截取窗口：锚值前 10 位（ISO 日期前缀）落在 [dates, datee]。"""
    dates, datee = window
    sliced: list[Mapping[str, Any]] = []
    for row in rows:
        anchor = str(row.get(anchor_column) or EMPTY)[:10]
        if anchor and dates <= anchor <= datee:
            sliced.append(row)
    return tuple(sliced)


def _validate_time_anchors(
    rows: tuple[Mapping[str, Any], ...], operation_id: str, extra_params: Mapping[str, Any]
) -> None:
    column = time_anchor_column(operation_id, extra_params)
    for row in rows:
        anchor = str(row.get(column) or EMPTY)[:10]
        try:
            parsed = date.fromisoformat(anchor)
        except ValueError:
            raise InvalidRequestError("detail time anchor is invalid") from None
        if parsed.isoformat() != anchor:
            raise InvalidRequestError("detail time anchor is invalid")


def local_view_rows(
    rows: tuple[Mapping[str, Any], ...],
    operation_id: str,
    filters: NarrowedFilters,
    extra_params: Mapping[str, Any],
    window: tuple[str, str],
    role: Role | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Apply authorized scope, calendar window and business filters to detail.

    Grouping follows this projection so no outside-scope contribution enters a
    returned aggregate. Native MES shadow input runs through the same final recipe.
    """
    scoped = scope_filtered_rows(rows, operation_id, filters, role)
    sliced = time_sliced_rows(scoped, time_anchor_column(operation_id, extra_params), window)
    return _business_filtered_rows(sliced, operation_id, filters, extra_params)


def _business_filtered_rows(
    rows: tuple[Mapping[str, Any], ...],
    operation_id: str,
    filters: NarrowedFilters,
    extra_params: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    groupby = extra_params.get("groupbyFields") or ()
    order_column = "dh" if operation_id == PROGRESS_OPERATION and "baohao" in groupby else "jhdh"
    if filters.bed_codes is not None and any("chuanghao" not in row for row in rows):
        raise InvalidRequestError("detail does not contain the requested bed dimension")

    def matches(row: Mapping[str, Any]) -> bool:
        checks = (
            (filters.order_codes, order_column),
            (filters.plan_codes, "jhdh"),
            (filters.material_ids, "userid"),
            (filters.bed_codes, "chuanghao"),
        )
        if filters.style_codes is not None:
            style_fields = (
                ("huohao", "bbreed") if operation_id == PAYROLL_OPERATION else ("bbreed",)
            )
            if not any(
                str(row.get(column) or EMPTY) in filters.style_codes for column in style_fields
            ):
                return False
        return all(
            values is None or str(row.get(column) or EMPTY) in values for values, column in checks
        )

    return tuple(row for row in rows if matches(row))


# ---------------------------------------------------------------------------
# 缓存适配层


@dataclass(frozen=True, slots=True)
class _CacheHit:
    rows: tuple[Mapping[str, Any], ...]
    footer: dict[str, str] | None
    generated_at: str
    source: str
    #: **命中键**的窗口（可能是预取窗口，而非本问题窗口）。驱逐必须打在它上面：
    #: 打在问题窗口上等于空转——那个键根本没被读过。
    window: tuple[str, str] = ("", "")
    source_call_count: int = 0


class V2ResultCache:
    """结果集缓存的读写与一致性（复用 ``AuthAwareCache``，§4.7）。

    键 = ``AuthAwareCache.build_key("v2_result", tenant, owner-99, params)``，
    ``params`` = ``{"api": 操作, **归一化请求参数, dates, datee}``；
    ``scope_sig`` 恒为全厂签名，提问者的范围在读取时过滤、不进键。
    """

    def __init__(
        self,
        cache: Any,
        *,
        mode: ResultCacheMode = ResultCacheMode.OFF,
        policy: ResultCachePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        timezone: tzinfo | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._cache = cache
        self._mode = mode
        self._policy = policy or ResultCachePolicy()
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        # 必须与 ``prefetch.run_once`` 用同一个「今天」，否则预取窗口错位一天
        # （见 ``_DEFAULT_TIMEZONE``）。生产由 bootstrap 传 factory 时区。
        self._timezone: tzinfo = timezone or _DEFAULT_TIMEZONE

    @property
    def mode(self) -> ResultCacheMode:
        return self._mode

    @property
    def policy(self) -> ResultCachePolicy:
        return self._policy

    @property
    def timezone(self) -> tzinfo:
        """部署时区。切片窗口与预取窗口必须同在此时区下折日历日（见 ``_today``）。"""
        return self._timezone

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc)

    @property
    def current_timestamp(self) -> str:
        return self._now().isoformat()

    def _today(self) -> str:
        """部署时区下的「今天」——与预取侧 ``run_once`` 逐字对齐（见类注释）。"""
        return self._now().astimezone(self._timezone).date().isoformat()

    def prefetch_window(self, today: str) -> tuple[str, str]:
        """预取窗口：``[今天 − 365, 昨天]``（§4.3，止于昨天吃掉夜间补录）。"""
        return (
            _shift_iso(today, -self._policy.backtrack_days),
            _shift_iso(today, -1),
        )

    def _key_params(
        self, operation_id: str, extra_params: Mapping[str, Any], window: tuple[str, str]
    ) -> dict[str, object]:
        return {
            "api": operation_id,
            "grain": DETAIL_GRAIN,
            **normalize_params(operation_id, extra_params, window[0], window[1]),
        }

    async def read(
        self,
        tenant_id: Any,
        operation_id: str,
        extra_params: Mapping[str, Any],
        question_window: tuple[str, str],
    ) -> _CacheHit | None:
        """命中返回缓存行集；未命中 / 故障 / 键不匹配一律 None（回退实时）。

        回溯范围内先探测本日预取，再探测仍完整覆盖问题的前一日预取，最后
        探测问题窗口自身的回写键。每个候选必须独立完整覆盖，不拼接窗口。
        """
        today = self._today()
        prefetch_dates, prefetch_datee = self.prefetch_window(today)
        settled = question_window[0] >= prefetch_dates and question_window[1] <= prefetch_datee
        windows: list[tuple[str, str]] = []
        if settled:
            windows.append((prefetch_dates, prefetch_datee))
            previous_window = self.prefetch_window(_shift_iso(today, -1))
            if (
                previous_window[0] <= question_window[0]
                and question_window[1] <= previous_window[1]
            ):
                windows.append(previous_window)
        if question_window not in windows:
            windows.append(question_window)
        probed: list[str] = []
        for window in windows:
            params = self._key_params(operation_id, extra_params, window)
            probed.append(self._describe_key(tenant_id, params))
            try:
                lookup = await self._cache.get(
                    RESULT_CACHE_DOMAIN, tenant_id, V2_RESULT_SCOPE_FINGERPRINT, params
                )
            except Exception:  # noqa: BLE001 - Redis 是优化，不是闸门
                _LOGGER.warning("v2_result.store_unavailable", extra={"operation": operation_id})
                return None
            if lookup.value is None:
                continue
            payload = decode_result_set(lookup.value)
            if (
                payload is None
                or payload.get("api") != operation_id
                or payload.get("filters") != params
                or payload.get("groupby")
                or payload.get("window") != {"dates": window[0], "datee": window[1]}
                or (
                    operation_id == PROGRESS_OPERATION
                    and payload.get("decoded_rows")
                    and PROGRESS_DATE_COLUMN not in payload.get("columns", ())
                )
            ):
                continue
            rows = cast("tuple[Mapping[str, Any], ...]", tuple(payload.get("decoded_rows") or ()))
            if operation_id == PROGRESS_OPERATION:
                try:
                    if any(
                        normalize_progress_date(row.get(PROGRESS_DATE_COLUMN))
                        != row.get(PROGRESS_DATE_COLUMN)
                        for row in rows
                    ):
                        continue
                except ProgressDerivationError:
                    continue
            if any(
                _FORBIDDEN_ROW_FIELDS.intersection(str(key).lower() for key in row) for row in rows
            ):
                continue
            source = str(payload.get("source") or EMPTY)
            # 命中/未命中必须各留一行：否则 shadow 的「读 + 比对」在日志里
            # 与「纯实时」完全同形，运维无法判断缓存到底有没有生效。
            _LOGGER.info(
                "v2_result.hit",
                extra={
                    "operation": operation_id,
                    "rows": len(rows),
                    "window": f"{window[0]}..{window[1]}",
                    "probed": len(probed),
                    "key_digest": probed[-1],
                    "source": source,
                },
            )
            return _CacheHit(
                rows=rows,
                footer=payload.get("footer") or None,
                generated_at=str(payload.get("generated_at") or EMPTY),
                source=source,
                window=window,
            )
        _LOGGER.info(
            "v2_result.miss",
            extra={
                "operation": operation_id,
                "window": f"{question_window[0]}..{question_window[1]}",
                "settled": settled,
                "key_digests": probed,
            },
        )
        return None

    def _describe_key(self, tenant_id: Any, params: Mapping[str, object]) -> str:
        """键串仅用于日志；构造失败不能影响读路径（Redis 是优化，不是闸门）。"""
        try:
            key = self._cache.build_key(
                RESULT_CACHE_DOMAIN, tenant_id, V2_RESULT_SCOPE_FINGERPRINT, params
            )
            return hashlib.sha256(str(key).encode()).hexdigest()
        except Exception:  # noqa: BLE001 - 仅用于日志
            return "<key-unavailable>"

    async def write(
        self,
        tenant_id: Any,
        operation_id: str,
        rows: tuple[Mapping[str, Any], ...],
        footer: Mapping[str, str] | None,
        extra_params: Mapping[str, Any],
        window: tuple[str, str],
        *,
        source: str,
        deadline: float | None = None,
    ) -> bool:
        """回写门禁 → 派生列 → F.1 编码 → 按窗口分类写 TTL（§4.6③ / F.2）。

        返回是否落盘；门禁不过 / 派生失败 / 存储故障都只记日志，绝不影响调用方。
        """
        if extra_params.get("groupbyFields"):
            return False
        if (
            any(_has_credential_fields(row) for row in rows)
            or _has_credential_fields(footer or {})
            or _has_credential_fields(extra_params)
            or _FORBIDDEN_REQUEST_PARAMS.intersection(str(key).lower() for key in extra_params)
        ):
            _LOGGER.warning("v2_result.row_fields_rejected", extra={"operation": operation_id})
            return False
        # 回写门禁：footer 与本地聚合对账，坏数据不入缓存。
        if not footer_reconciles(rows, footer, tolerance=self._policy.float_tolerance):
            _LOGGER.warning(
                "v2_result.write_gate_rejected",
                extra={"operation": operation_id, "rows": len(rows)},
            )
            return False
        derived: tuple[str, ...] = ()
        stored_rows: tuple[Mapping[str, Any], ...] = rows
        if operation_id == PROGRESS_OPERATION:
            try:
                stored_rows = derive_progress_rows(rows)
            except ProgressDerivationError:
                _LOGGER.warning(
                    "v2_result.progress_date_invalid",
                    extra={"operation": operation_id, "rows": len(rows)},
                )
                return False
        try:
            _validate_time_anchors(stored_rows, operation_id, extra_params)
        except InvalidRequestError:
            _LOGGER.warning("v2_result.time_anchor_invalid", extra={"operation": operation_id})
            return False
        window_kind = window_category(
            window[0], window[1], self._today(), self._policy.backtrack_days
        )
        ttl = (
            self._policy.unsettled_ttl_seconds
            if window_kind == "unsettled"
            else self._policy.settled_ttl_seconds
        )
        params = self._key_params(operation_id, extra_params, window)
        try:
            payload = encode_result_set(
                operation_id=operation_id,
                rows=stored_rows,
                footer=footer,
                window=window,
                filters=params,
                derived=derived,
                generated_at=self._now().isoformat(),
                source=source,
            )
            if deadline is not None:
                publication_time = self._monotonic_clock or asyncio.get_running_loop().time
                if publication_time() >= deadline:
                    raise TimeoutError("cache publication deadline exceeded")
            written = await self._cache.put(
                RESULT_CACHE_DOMAIN,
                tenant_id,
                V2_RESULT_SCOPE_FINGERPRINT,
                params,
                payload,
                ttl_seconds=ttl,
            )
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.warning("v2_result.store_unavailable", extra={"operation": operation_id})
            return False
        return written is True

    async def evict(
        self,
        tenant_id: Any,
        operation_id: str,
        extra_params: Mapping[str, Any],
        window: tuple[str, str],
    ) -> None:
        params = self._key_params(operation_id, extra_params, window)
        try:
            key = self._cache.build_key(
                RESULT_CACHE_DOMAIN, tenant_id, V2_RESULT_SCOPE_FINGERPRINT, params
            )
        except Exception:  # noqa: BLE001
            return
        try:
            await self._cache.invalidate_key(key)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("v2_result.evict_failed", extra={"operation": operation_id})


class FetchThrough(Protocol):
    """内层取数端口（ScopedExecutor 或适配器）。"""

    def validate_step(
        self, filters: Any, request: CacheableRequest, active_scope: Any | None = None
    ) -> None: ...

    async def execute_full_step(
        self,
        filters: Any,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult: ...


class SnapshotProvider(Protocol):
    """Fetch validated, complete default detail using a trusted tenant owner."""

    async def __call__(
        self,
        tenant_id: Any,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult: ...


class ResultCacheExecutor:
    """Serve scoped views from two complete owner detail snapshots.

    Caller-filtered MES results never populate the shared owner snapshot. Shadow
    results are compared by the kernel after running the same reviewed recipe.
    """

    def __init__(
        self,
        inner: FetchThrough,
        cache: V2ResultCache,
        *,
        snapshot_provider: SnapshotProvider | None = None,
        alerts: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._snapshot_provider = snapshot_provider
        self._snapshot_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._alerts = alerts
        self._served_hits: dict[
            int,
            tuple[weakref.ReferenceType[ResourceFetchResult], Any, str, dict[str, Any], _CacheHit],
        ] = {}

    def _alert(self, name: str, detail: dict[str, Any]) -> None:
        if self._alerts is not None:
            try:
                self._alerts(name, detail)
            except Exception:  # noqa: BLE001 - alerts never affect business results
                _LOGGER.warning("v2_result.alert_failed")
        _LOGGER.warning("v2_result.%s", name, extra=detail)

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        self._inner.validate_step(filters, request, active_scope)
        params = dict(extra_params or {})
        if _FORBIDDEN_REQUEST_PARAMS.intersection(key.lower() for key in params):
            raise InvalidRequestError("scope and credential parameters cannot be supplied")
        operation_id = request.operation_id
        window_start = cast("datetime | None", request.time_range[0])
        if (
            not self._cache.mode.reads
            or not cacheable_grain(operation_id, params)
            or window_start is None
        ):
            return await self._fetch_realtime(filters, request, active_scope, params)
        window = local_calendar_dates(window_start, request.time_range[1], self._cache.timezone)
        snapshot_calls = 0
        hit = await self._cache.read(filters.tenant_id, operation_id, params, window)
        if (
            hit is None
            and self._snapshot_provider is not None
            and getattr(request, "allow_owner_snapshot", True)
        ):
            lock_key = hashlib.sha256(
                json.dumps(
                    [
                        str(filters.tenant_id),
                        operation_id,
                        normalize_params(operation_id, params, window[0], window[1]),
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            lock = self._snapshot_locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                hit = await self._cache.read(filters.tenant_id, operation_id, params, window)
                if hit is None:
                    hit, snapshot_calls = await self._fetch_snapshot(
                        filters, request, params, window
                    )
        if hit is not None:
            try:
                served = self._serve_from_cache(hit, filters, request, params, window)
                validator = getattr(request, "result_validator", None)
                if validator is not None and not validator(served):
                    raise InvalidRequestError("required analysis columns are unavailable")
            except (ProgressDerivationError, InvalidRequestError, InvalidOperation):
                await self._cache.evict(filters.tenant_id, operation_id, params, hit.window)
                _LOGGER.warning("v2_result.view_invalid", extra={"operation": operation_id})
            else:
                if self._cache.mode.verifies:
                    realtime = await self._fetch_realtime(filters, request, active_scope, params)
                    served = replace(
                        served,
                        shadow_result=realtime,
                        source_call_count=(served.source_call_count or 0)
                        + _source_call_count(realtime),
                    )
                    self._remember_hit(served, filters, request, params, hit)
                return served
        realtime = await self._fetch_realtime(filters, request, active_scope, params)
        await self._maybe_write(realtime, filters, request, params, window)
        if snapshot_calls:
            return replace(
                realtime, source_call_count=snapshot_calls + _source_call_count(realtime)
            )
        return realtime

    async def _fetch_realtime(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None,
        params: dict[str, Any],
        *,
        force_detail: bool = False,
    ) -> ResourceFetchResult:
        window_start = cast("datetime | None", request.time_range[0])
        if not cacheable_grain(request.operation_id, params) or window_start is None:
            return await self._inner.execute_full_step(
                filters, request, active_scope=active_scope, extra_params=params or None
            )
        native_progress = (
            not force_detail
            and request.operation_id == PROGRESS_OPERATION
            and self._native_progress_has_filters(filters, request, params)
        )
        fetch_params = params if native_progress else detail_params(params)
        fetch_filters = filters
        if request.operation_id == PROGRESS_OPERATION:
            # Material IDs are a returned detail dimension; the V2 contract
            # does not declare a material-ID request parameter.
            fetch_filters = replace(fetch_filters, material_ids=None)
            groupby = params.get("groupbyFields") or ()
            if not native_progress and "baohao" in groupby:
                # A package view's order slot carries sheet IDs. Default detail
                # must not reinterpret those values as plan IDs.
                fetch_filters = replace(fetch_filters, order_codes=None)
                if filters.order_codes is not None and len(filters.order_codes) == 1:
                    fetch_params["dh"] = next(iter(filters.order_codes))
        fetched = await self._inner.execute_full_step(
            fetch_filters,
            request,
            active_scope=active_scope,
            extra_params=fetch_params or None,
        )
        if native_progress and (
            (
                "dh" in (params.get("groupbyFields") or ())
                and any(not row.get(PROGRESS_DATE_COLUMN) for row in fetched.rows)
            )
            or (
                filters.bed_codes is not None
                and any("chuanghao" not in row for row in fetched.rows)
            )
        ):
            # A grouped response can omit the date even though default detail
            # supplies it. Fetch detail once rather than infer a date from IDs.
            detailed = await self._fetch_realtime(
                filters, request, active_scope, params, force_detail=True
            )
            return replace(
                detailed,
                source_call_count=_source_call_count(fetched) + _source_call_count(detailed),
            )
        if native_progress:
            # Preserve the customer's grouped calculation as an independent
            # baseline. Filter retained dimensions without recomputing its sums.
            rows = scope_filtered_rows(fetched.rows, request.operation_id, filters, request.role)
            rows = _business_filtered_rows(rows, request.operation_id, filters, params)
            if rows == fetched.rows:
                return fetched
            return replace(
                fetched,
                rows=tuple(dict(row) for row in rows),
                total=len(rows),
                footer=recomputed_footer(rows, fetched.footer),
            )
        window = local_calendar_dates(window_start, request.time_range[1], self._cache.timezone)
        try:
            rows = self._project_rows(fetched.rows, filters, request, params, window)
        except (ProgressDerivationError, InvalidRequestError, InvalidOperation):
            _LOGGER.warning(
                "v2_result.realtime_view_invalid", extra={"operation": request.operation_id}
            )
            return replace(
                fetched, rows=(), total=0, footer=None, complete=False, reason="upstream_invalid"
            )
        projected = replace(
            fetched,
            rows=tuple(dict(row) for row in rows),
            total=len(rows),
            footer=recomputed_footer(rows, fetched.footer),
        )
        # Validate the original source totals before a local projection changes
        # their grain; otherwise a bad source footer could become a valid cache.
        if fetched.complete and not footer_reconciles(
            fetched.rows, fetched.footer, tolerance=self._cache.policy.float_tolerance
        ):
            return replace(projected, complete=False, reason="upstream_invalid")
        if (
            projected.rows == fetched.rows
            and projected.total == fetched.total
            and projected.footer == fetched.footer
        ):
            return fetched
        return projected

    @staticmethod
    def _native_progress_has_filters(
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
    ) -> bool:
        fields = params.get("groupbyFields") or ()
        if not fields:
            return True
        needed: set[str] = set()
        dept_ids = (
            filters.requested_dept_ids
            if request.role in (Role.OWNER, Role.EMPLOYEE)
            else filters.dept_ids
        )
        if dept_ids is not None:
            needed.add("dept")
        if filters.employee_ids is not None:
            needed.add("uid")
        if filters.order_codes is not None:
            needed.add("dh" if "baohao" in fields else "jhdh")
        if filters.style_codes is not None:
            needed.add("bbreed")
        if filters.plan_codes is not None:
            needed.add("jhdh")
        if filters.material_ids is not None:
            needed.add("userid")
        if filters.bed_codes is not None:
            needed.add("chuanghao")
        return needed <= set(fields)

    async def _fetch_snapshot(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> tuple[_CacheHit | None, int]:
        provider = self._snapshot_provider
        if provider is None:
            return None, 0
        base_params = detail_params(params)
        source_calls = 0
        try:
            fetched = await provider(filters.tenant_id, request.operation_id, window, base_params)
            source_calls = _source_call_count(fetched)
            if not fetched.complete:
                return None, source_calls
            if self._cache.mode.writes:
                written = await self._cache.write(
                    filters.tenant_id,
                    request.operation_id,
                    fetched.rows,
                    fetched.footer,
                    base_params,
                    window,
                    source="query",
                )
                if not written:
                    self._alert(
                        "cache_write_failed",
                        {
                            "operation": request.operation_id,
                            "rows": len(fetched.rows),
                            "window": f"{window[0]}..{window[1]}",
                            "source": "owner_snapshot",
                        },
                    )
                    return None, source_calls
            elif not footer_reconciles(fetched.rows, fetched.footer):
                return None, source_calls
            rows = (
                derive_progress_rows(fetched.rows)
                if request.operation_id == PROGRESS_OPERATION
                else fetched.rows
            )
        except Exception:  # noqa: BLE001 - snapshot availability cannot block caller MES
            _LOGGER.warning(
                "v2_result.snapshot_unavailable", extra={"operation": request.operation_id}
            )
            return None, source_calls
        return _CacheHit(
            rows=rows,
            footer=fetched.footer,
            generated_at=self._cache.current_timestamp,
            source="query",
            window=window,
            source_call_count=source_calls,
        ), source_calls

    def _serve_from_cache(
        self,
        hit: _CacheHit,
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> ResourceFetchResult:
        rows = self._project_rows(hit.rows, filters, request, params, window)
        return ResourceFetchResult(
            rows=tuple(dict(row) for row in rows),
            total=len(rows),
            pages_fetched=0,
            complete=True,
            footer=recomputed_footer(rows, hit.footer),
            cache_source=hit.source,
            cache_generated_at=hit.generated_at,
            source_call_count=hit.source_call_count,
        )

    @staticmethod
    def _project_rows(
        source_rows: tuple[Mapping[str, Any], ...],
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> tuple[Mapping[str, Any], ...]:
        operation_id = request.operation_id
        detail = (
            derive_progress_rows(source_rows) if operation_id == PROGRESS_OPERATION else source_rows
        )
        _validate_time_anchors(detail, operation_id, params)
        rows = local_view_rows(detail, operation_id, filters, params, window, request.role)
        raw_groupby = params.get("groupbyFields") or ()
        if not isinstance(raw_groupby, (list, tuple)):
            raise InvalidRequestError("groupbyFields must be a field sequence")
        groupby_values = cast("list[Any] | tuple[Any, ...]", raw_groupby)
        if not all(isinstance(field, str) for field in groupby_values):
            raise InvalidRequestError("groupbyFields must contain field names")
        groupby = cast("tuple[str, ...]", tuple(groupby_values))
        if operation_id == PROGRESS_OPERATION:
            rows = aggregate_progress_rows(rows, groupby)
        elif groupby:
            rows = _aggregate_payroll_rows(rows, groupby)
        return rows

    def _remember_hit(
        self,
        result: ResourceFetchResult,
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
        hit: _CacheHit,
    ) -> None:
        handle = id(result)
        reference = weakref.ref(result, lambda _: self._served_hits.pop(handle, None))
        self._served_hits[handle] = reference, filters.tenant_id, request.operation_id, params, hit

    def release_cached_result(self, result: ResourceFetchResult) -> None:
        self._served_hits.pop(id(result), None)

    async def discard_cached_result(self, result: ResourceFetchResult) -> None:
        entry = self._served_hits.pop(id(result), None)
        if entry is None or entry[0]() is not result:
            return
        _, tenant_id, operation_id, params, hit = entry
        self._alert(
            "cache_shadow_mismatch",
            {
                "operation": operation_id,
                "window": f"{hit.window[0]}..{hit.window[1]}",
                "source": hit.source,
                "cached_rows": len(result.rows),
                "realtime_rows": len(result.shadow_result.rows) if result.shadow_result else 0,
            },
        )
        await self._cache.evict(tenant_id, operation_id, params, hit.window)

    async def _maybe_write(
        self,
        realtime: ResourceFetchResult,
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> None:
        if not self._cache.mode.writes or not realtime.complete or request.role is not Role.OWNER:
            return
        if params.get("groupbyFields") or params.get("uid") or params.get("Uid"):
            return
        if any(
            value is not None
            for value in (
                filters.employee_ids,
                filters.requested_dept_ids,
                filters.order_codes,
                filters.style_codes,
                filters.plan_codes,
                filters.material_ids,
                filters.bed_codes,
            )
        ):
            return
        written = await self._cache.write(
            filters.tenant_id,
            request.operation_id,
            realtime.rows,
            realtime.footer,
            params,
            window,
            source="query",
        )
        if not written:
            self._alert(
                "cache_write_failed",
                {
                    "operation": request.operation_id,
                    "rows": len(realtime.rows),
                    "window": f"{window[0]}..{window[1]}",
                    "source": "caller",
                },
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _source_call_count(result: ResourceFetchResult) -> int:
    if result.source_call_count is not None:
        return result.source_call_count
    return 1


def _aggregate_payroll_rows(
    rows: tuple[Mapping[str, Any], ...], fields: list[str] | tuple[str, ...]
) -> tuple[Mapping[str, Any], ...]:
    columns = tuple(dict.fromkeys(fields))
    measures = ("je", "sl", "sssl", "zpsl", "bs", "wgs", "wts", "fhsl")
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        if any(column not in row for column in columns):
            raise InvalidRequestError("payroll detail does not contain the requested dimensions")
        key = tuple(str(row.get(column) or EMPTY) for column in columns)
        out = grouped.setdefault(key, dict(zip(columns, key)))
        for measure in measures:
            if measure in row:
                out[measure] = Decimal(str(out.get(measure) or "0")) + Decimal(
                    str(row.get(measure) or "0")
                )
    return tuple(
        {key: str(value) for key, value in grouped[group].items()} for group in sorted(grouped)
    )


__all__ = [
    "PROGRESS_DATE_COLUMN",
    "NON_ADDITIVE_FOOTER_COLUMNS",
    "OWNER_SCOPE_SIGNATURE",
    "RESULT_CACHE_DOMAIN",
    "RESULT_SET_VERSION",
    "ResultCacheExecutor",
    "ResultCacheMode",
    "ResultCachePolicy",
    "SnapshotProvider",
    "ShadowVerdict",
    "V2ResultCache",
    "cacheable_grain",
    "decode_result_set",
    "detail_params",
    "encode_result_set",
    "footer_reconciles",
    "local_view_rows",
    "normalize_params",
    "recomputed_footer",
    "scope_filtered_rows",
    "shadow_compare",
    "time_anchor_column",
    "time_sliced_rows",
    "window_category",
]
