"""V2 结果集缓存（老板缓存）：取数层装饰器 + 缓存值编解码 + 一致性机制。

设计依据：``docs/design/V2接口取数重构与缓存加速方案.md`` §4 / 附录 F（2026-10-11）。

- **缓存单位** = 一次 V2 请求的完整响应体（行集 + footer + 行数 + 生成时点），
  列式紧凑 JSON（附录 F.1）；业务数据原样持久化，不脱敏；凭证 / accessToken /
  LLM 文本 / 卡片 / ResultTable 永不进缓存。
- **键** = ``fa:{contract}:{metric}:{data}:{tenant}:{scope_sig}:{api}:{params_digest}``；
  ``scope_sig`` 恒为全厂（99）签名——提问者的范围不进键，是读取时的过滤参数。
- **键归一化**（F.3，均已实测）：排序从不进键（排序一律本地做）、``groupby``
  集合化（键按排序后字段名集合）、枚举取值（``Type``）排序去重。
- **三条路径**（§4.5）：回溯范围内已结算窗口 → 读预取缓存、本地按时间锚截取、
  重聚合（0 次 MES 调用）；超出回溯范围 → 实时 + 回写 24h；未结算（含今天）→
  实时 + 只写短 TTL（60~120s）。
- **一致性**（§4.6）：模式开关（off/read/readwrite/shadow）、影子比对
  （命中后仍实时取一次，逐行同位比对，tie_order_only 单独标注）、回写门禁
  （footer 与本地聚合对账，整数严格相等、浮点容差）。
- **任何故障**（Redis 不可用 / 键不匹配 / 行损坏 / 派生列解析失败）一律回退
  实时，语义与无缓存完全一致。
"""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol, cast

from factory_agent.domain import NarrowedFilters, Role, local_calendar_dates
from factory_agent.ports.contracts import ResourceFetchResult

_LOGGER = logging.getLogger("factory_agent.data_api.result_cache")

#: 结果集缓存域（AuthAwareCache 的缓存域之一；键前缀 ``fa:…:v2_result:``）。
RESULT_CACHE_DOMAIN = "v2_result"

#: 参与结果集缓存的 V2 操作及其**可缓存粒度**（方案 §3.2② / 附录 C）：
#: 工资族预取粒度 = 明细模式（不传 groupbyFields）；进度预取粒度 = M7
#: （制单 × 部门）。其余粒度（FR-006/007/013 的分组取数、下钻粒度）不读不写缓存，
#: 始终实时——预取底料无法重聚合出它们的维度。
PAYROLL_OPERATION = "GongziClQuery"
PROGRESS_OPERATION = "ScjdQuery"
_PROGRESS_M7_GROUPBY: frozenset[str] = frozenset(
    {"dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"}
)

#: 缓存值结构版本（附录 F.1 ``v``）；派生逻辑或值结构变更时 bump。
RESULT_SET_VERSION = 1

#: 进度行的派生日期列（§4.4）：``dh`` 前 8 位解析成的标准日期。
DERIVED_DH_DATE = "dh_date"

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


def derive_dh_date(dh: object) -> str | None:
    """``dh`` 前 8 位 → ``YYYY-MM-DD``；解析失败返回 None（§4.4 格式校验）。

    制单日期恒等于制单号前 8 位（附录 A.5 实测零越界）；不符合 ``YYYYMMDD…``
    或不是合法日期的行不写缓存、回退实时。
    """
    text = str(dh or EMPTY)
    if len(text) < 8:
        return None
    head = text[:8]
    if not head.isdigit():
        return None
    year, month, day = int(head[:4]), int(head[4:6]), int(head[6:8])
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


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

    - ``groupbyFields`` 集合化（排序后的字段名列表）；
    - 枚举取值（``Type``）排序去重；
    - 排序参数不存在——排序从不下发 MES（§3.2⑤）。
    """
    normalized: dict[str, object] = {"dates": dates, "datee": datee}
    for key in sorted(extra_params):
        if key in {"dates", "datee", "orderbyFields", "orderByType"}:
            continue
        value: Any = extra_params[key]
        if key == "groupbyFields":
            items = cast("list[Any]", value if isinstance(value, (list, tuple)) else [value])
            normalized["groupby"] = sorted(str(field) for field in items)
        elif key == "Type" and isinstance(value, str):
            normalized[key] = ",".join(sorted({part for part in value.split(",") if part}))
        else:
            normalized[key] = value
    return normalized


def cacheable_grain(operation_id: str, extra_params: Mapping[str, Any]) -> bool:
    """该 (操作, groupbyFields) 粒度是否可读/写结果集缓存。"""
    group: Any = extra_params.get("groupbyFields")
    if operation_id == PAYROLL_OPERATION:
        return not group
    if operation_id == PROGRESS_OPERATION:
        if not isinstance(group, (list, tuple)):
            return False
        fields = cast("list[Any]", group)
        return frozenset(str(field) for field in fields) == _PROGRESS_M7_GROUPBY
    return False


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
    decoded: list[dict[str, str]] = []
    for raw_row in cast("list[Any]", rows):
        if not isinstance(raw_row, list):
            return None
        row = cast("list[Any]", raw_row)
        if len(row) != len(columns):
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


#: footer 里以「整体比率 / 均值」而非「行值之和」给出的聚合列：不参与对账。
#:
#: ``wcl`` = 包完工率（``configs/knowledge/field-dictionary.yaml``：缝制生产进度
#: 0~100 整数）。实测 2026-10-11：ScjdQuery 9001 行的 ``wcl`` 逐行求和 =
#: 898267.11，而服务端 ``footer.wcl_total`` = 99.77——那是**整体完工率**（≈ 均值
#: 99.8），不是和。比率列当可加量求和必然对不上；把「对不上」当成「数据坏」，
#: 会把 100% 的正常响应挡在缓存外。
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
    - 列在行集里但属 :data:`NON_ADDITIVE_FOOTER_COLUMNS`（比率/均值类）→ 跳过，
      逐行求和本身没有意义。

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
        if column not in present or column in NON_ADDITIVE_FOOTER_COLUMNS:
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
        if expected_decimal == expected_decimal.to_integral_value():
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
    """影子比对结论：``tie_order_only`` 区分真差异与并列顺序漂移（G.5）。"""

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
    for key in footer:
        if not key.endswith("_total"):
            continue
        column = key[: -len("_total")]
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
) -> tuple[Mapping[str, Any], ...]:
    """按提问者范围过滤缓存行（提问者的范围不进键，读取时过滤）。

    - ``99``（filters 无范围字段）：不过滤，全厂即其范围；
    - ``01`` / ``02``：``dept ∈ filters.dept_ids``（进度侧空 dept 组随之排除，
      附录 A.4：它在老板视角有意义，在任何部门视角都不该出现）;
    - ``00``：``uid == 本人``。
    """
    employee_ids = filters.employee_ids
    dept_ids = filters.dept_ids
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
    进度行集无日期列，用派生列 ``dh_date``。"""
    if operation_id == PROGRESS_OPERATION:
        return DERIVED_DH_DATE
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


def local_view_rows(
    rows: tuple[Mapping[str, Any], ...],
    operation_id: str,
    filters: NarrowedFilters,
    extra_params: Mapping[str, Any],
    window: tuple[str, str],
) -> tuple[Mapping[str, Any], ...]:
    """取数层交给下游的**本地视图**：范围过滤 → 时间锚截取。

    命中缓存时 serve 的就是它。影子比对也必须把实时行集过一遍**同一变换**再比：
    ``_inner`` 返回的是 MES 原始行集（服务端不做部门过滤、也不做本地截取），
    直接拿它比切片，等于「全厂整年」比「某部门某月」，行数永远不可能相等。
    """
    scoped = scope_filtered_rows(rows, operation_id, filters)
    return time_sliced_rows(scoped, time_anchor_column(operation_id, extra_params), window)


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
    ) -> None:
        self._cache = cache
        self._mode = mode
        self._policy = policy or ResultCachePolicy()
        self._clock = clock
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

        问题窗口 ⊆ 回溯范围时优先读**预取键**（预取窗口 + 同参，§4.5 路径 1）；
        预取键未命中再回退读问题窗口自身的键（路径 2/3 的回写键——交互路径的
        惰性回填对同窗口的重复提问同样生效）。否则只读问题窗口键。
        """
        today = self._today()
        prefetch_dates, prefetch_datee = self.prefetch_window(today)
        settled = question_window[0] >= prefetch_dates and question_window[1] <= prefetch_datee
        windows = (
            ((prefetch_dates, prefetch_datee), question_window) if settled else (question_window,)
        )
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
            if payload is None or payload.get("api") != operation_id:
                continue
            rows = tuple(payload.get("decoded_rows") or ())
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
                    "key": probed[-1],
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
                "keys": probed,
            },
        )
        return None

    def _describe_key(self, tenant_id: Any, params: Mapping[str, object]) -> str:
        """键串仅用于日志；构造失败不能影响读路径（Redis 是优化，不是闸门）。"""
        try:
            return str(
                self._cache.build_key(
                    RESULT_CACHE_DOMAIN, tenant_id, V2_RESULT_SCOPE_FINGERPRINT, params
                )
            )
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
    ) -> bool:
        """回写门禁 → 派生列 → F.1 编码 → 按窗口分类写 TTL（§4.6③ / F.2）。

        返回是否落盘；门禁不过 / 派生失败 / 存储故障都只记日志，绝不影响调用方。
        """
        if not rows:
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
            # §4.4：进度行派生 dh_date；解析失败的行数 ≠ 0 → 整批弃用（不写缓存）。
            enriched: list[dict[str, Any]] = []
            for row in rows:
                dh_date = derive_dh_date(row.get("dh"))
                if dh_date is None:
                    _LOGGER.warning(
                        "v2_result.dh_date_parse_failed",
                        extra={"operation": operation_id, "rows": len(rows)},
                    )
                    return False
                enriched.append({**dict(row), DERIVED_DH_DATE: dh_date})
            stored_rows = tuple(enriched)
            derived = (DERIVED_DH_DATE,)
        window_kind = window_category(
            window[0], window[1], self._today(), self._policy.backtrack_days
        )
        ttl = (
            self._policy.unsettled_ttl_seconds
            if window_kind == "unsettled"
            else self._policy.settled_ttl_seconds
        )
        params = self._key_params(operation_id, extra_params, window)
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
        try:
            await self._cache.put(
                RESULT_CACHE_DOMAIN,
                tenant_id,
                V2_RESULT_SCOPE_FINGERPRINT,
                params,
                payload,
                ttl_seconds=ttl,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("v2_result.store_unavailable", extra={"operation": operation_id})
            return False
        return True

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

    async def execute_full_step(
        self,
        filters: Any,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult: ...


class ResultCacheExecutor:
    """取数层装饰器：读 → 范围过滤 → 时间截取；未命中 → 实时 + 门禁回写。

    只接管**可缓存粒度**（``cacheable_grain``）的 V2 操作；其余步骤原样透传。
    回写只发生在 ``role == OWNER`` 且无范围下推的实时拉取上——V2 服务端按 JWT
    行级过滤，非 99 凭据返回的行集不是全厂数据，绝不能进全厂键。
    """

    def __init__(
        self,
        inner: FetchThrough,
        cache: V2ResultCache,
        *,
        alerts: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._alerts = alerts

    def _alert(self, name: str, detail: dict[str, Any]) -> None:
        if self._alerts is not None:
            try:
                self._alerts(name, detail)
            except Exception:  # noqa: BLE001 - 告警绝不影响结果
                _LOGGER.exception("v2_result.alert_failed")
        _LOGGER.warning("v2_result.%s", name, extra=detail)

    async def execute_full_step(
        self,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ResourceFetchResult:
        params = dict(extra_params or {})
        operation_id = request.operation_id
        window_start = cast("datetime | None", request.time_range[0])
        if (
            not self._cache.mode.reads
            or not cacheable_grain(operation_id, params)
            or window_start is None
        ):
            return await self._inner.execute_full_step(
                filters, request, active_scope=active_scope, extra_params=extra_params
            )
        # 键与切片窗口都必须折成**部署时区的含端日历日**。直接对 UTC 值取
        # ``.date()`` 会让 ``2026-09-01T00:00+08:00``（= 08-31T16:00Z）退到 08-31，
        # 于是缓存比实时多出整整一天（线上 9 月工资清单：277 行 / 281032.73，
        # 而实时是 269 行 / 265089.63）。换算与实时路径共用
        # ``domain.queries.local_calendar_dates``，杜绝两处再次各自漂移。
        window = local_calendar_dates(window_start, request.time_range[1], self._cache.timezone)
        hit = await self._cache.read(filters.tenant_id, operation_id, params, window)
        if hit is not None:
            served = self._serve_from_cache(hit, filters, operation_id, params, window)
            if self._cache.mode.verifies:
                return await self._shadow_verify(
                    served, hit, filters, request, active_scope, params, window
                )
            return served
        realtime = await self._inner.execute_full_step(
            filters, request, active_scope=active_scope, extra_params=extra_params
        )
        await self._maybe_write(realtime, filters, request, params, window)
        return realtime

    def _serve_from_cache(
        self,
        hit: _CacheHit,
        filters: NarrowedFilters,
        operation_id: str,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> ResourceFetchResult:
        rows = local_view_rows(hit.rows, operation_id, filters, params, window)
        return ResourceFetchResult(
            rows=tuple(dict(row) for row in rows),
            total=len(rows),
            pages_fetched=1,
            complete=True,
            footer=recomputed_footer(rows, hit.footer),
        )

    async def _shadow_verify(
        self,
        served: ResourceFetchResult,
        hit: _CacheHit,
        filters: NarrowedFilters,
        request: CacheableRequest,
        active_scope: Any | None,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> ResourceFetchResult:
        """影子比对：两侧都必须过**同一套本地变换**再比，否则比对无意义。

        两个不对称曾让这里恒判不一致（线上 9 月工资清单：命中也被判 mismatch、
        每次告警、还去驱逐一个从未命中的键）：

        1 ``hit.rows`` 是整段预取窗口（~17 万行），不是本次 serve 的切片；
        2 即便换成 ``served``，实时侧仍是 MES 的**原始行集**——服务端不做部门
          过滤（``hongzhao._resource_params`` 只为 GongziClQuery 下推单个
          ``uid``），而命中侧已经做过范围过滤 + 时间截取。全厂 vs 一个部门、
          整年 vs 一个月，行数照样不等。

        所以实时行集用 :func:`local_view_rows` 走一遍与命中侧相同的变换，footer
        也按同一口径重算（``recomputed_footer``），再逐行同位比对。这样比对真正
        回答的问题才是：「缓存里的那份底料，切片出来和现取的一不一样」——缓存
        陈旧、缺行、口径漂移都会被这个判定抓住。

        比对结果里 ``tie_order_only``（多重集全等、footer 全等，仅并列行先后不同）
        **按一致处理**：排序一律本地做、从不下发 MES，取数层行序不在契约内。
        """
        realtime = await self._inner.execute_full_step(
            filters, request, active_scope=active_scope, extra_params=params
        )
        rows = local_view_rows(realtime.rows, request.operation_id, filters, params, window)
        normalized = replace(
            realtime,
            rows=tuple(dict(row) for row in rows),
            total=len(rows),
            footer=recomputed_footer(rows, realtime.footer),
        )
        verdict = shadow_compare(served.rows, served.footer, normalized)
        if verdict.tie_order_only:
            # 并列项行序漂移**不是**差异：排序从不下发 MES、一律本地做（F.3），
            # 取数层的行序不在契约里。线上实测：命中的 9173 行与实时 9173 行
            # 多重集全等、footer 全等，仅并列行先后不同。若判为不一致，shadow
            # 会把每一个健康键都驱逐掉——留一行 info，然后照常 serve 缓存。
            _LOGGER.info(
                "v2_result.shadow.tie_order_only",
                extra={
                    "operation": request.operation_id,
                    "window": f"{hit.window[0]}..{hit.window[1]}",
                    "rows": len(served.rows),
                },
            )
        if verdict.consistent or verdict.tie_order_only:
            return served
        detail = {
            "operation": request.operation_id,
            "window": f"{hit.window[0]}..{hit.window[1]}",
            "source": hit.source,
            "tie_order_only": verdict.tie_order_only,
            "cached_rows": len(served.rows),
            "realtime_rows": len(normalized.rows),
        }
        self._alert("cache_shadow_mismatch", detail)
        # 驱逐**命中键**（通常是没有回溯范围的预取键），不是本问题窗口的键。
        await self._cache.evict(filters.tenant_id, request.operation_id, params, hit.window)
        # 比对不一致：以实时结果返回给用户，丢弃该缓存键。
        return realtime

    async def _maybe_write(
        self,
        realtime: ResourceFetchResult,
        filters: NarrowedFilters,
        request: CacheableRequest,
        params: dict[str, Any],
        window: tuple[str, str],
    ) -> None:
        if not self._cache.mode.writes or not realtime.complete:
            return
        # 只有 99 全厂凭据返回的行集才是全厂数据；范围下推（uid/dept）会让
        # 行集变窄，绝不能写进全厂键。
        if request.role is not Role.OWNER:
            return
        if params.get("uid") or params.get("Uid"):
            return
        await self._cache.write(
            filters.tenant_id,
            request.operation_id,
            realtime.rows,
            realtime.footer,
            params,
            window,
            source="query",
        )

    def __getattr__(self, name: str) -> Any:
        # execute_step 等其余端口方法原样透传（只拦截 execute_full_step）。
        return getattr(self._inner, name)


__all__ = [
    "DERIVED_DH_DATE",
    "NON_ADDITIVE_FOOTER_COLUMNS",
    "OWNER_SCOPE_SIGNATURE",
    "RESULT_CACHE_DOMAIN",
    "RESULT_SET_VERSION",
    "ResultCacheExecutor",
    "ResultCacheMode",
    "ResultCachePolicy",
    "ShadowVerdict",
    "V2ResultCache",
    "cacheable_grain",
    "decode_result_set",
    "derive_dh_date",
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
