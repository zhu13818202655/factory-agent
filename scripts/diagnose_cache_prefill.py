"""以 99（老板）视角诊断 V2 结果集缓存为什么一条键都没落盘。

诊断脚本，不属于生产链路。它刻意复用 ``bootstrap`` 的构建函数
（``_build_cache`` / ``_build_result_cache`` / ``_Owner99PrefetchFetcher``），
因此产出的 Redis 键与运行中的应用**逐字节一致**——不会出现「脚本写进去了、
应用却读不到」的假象。落盘走的是真实的 ``V2PrefetchWorker.run_once``。

每一步都打点，失败会停在具体那一环：

  1  载入 .env → FactoryAgentSettings，打印关键开关
  2  Redis 可达性探测（``AuthAwareCache.ping``）
  3  结果集缓存构建：模式 / reads / writes / verifies
  4  99 凭据换取：角色 / 租户 / 绑定部门（换取失败就在这里断掉）
  5  逐族取数：行数 / 完整性 / 耗时 / footer 键（不完整就在这里断掉）
  6  回写门禁预演 + 真实落盘：TTL 分档 / 门禁是否放行
  7  Redis 键空间快照

用法::

    uv run --no-sync python scripts/diagnose_cache_prefill.py              # 取数并写缓存
    uv run --no-sync python scripts/diagnose_cache_prefill.py --dry-run    # 只取数、不写
    uv run --no-sync python scripts/diagnose_cache_prefill.py --only ScjdQuery
    uv run --no-sync python scripts/diagnose_cache_prefill.py --reuse-fetch  # 复用上次取数

取数成功会把行集落到 ``$TMPDIR/fa_prefetch_fetch_<op>.json``；``--reuse-fetch``
从那里读回并校验窗口/参数一致，反复诊断门禁时不再重复压客户 MES。

退出码：0 = 至少一个键落盘；1 = 一个键都没落盘（并已打印断点）。
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_agent import bootstrap as boot
from factory_agent.application import prefetch as prefetch_module
from factory_agent.application.prefetch import V2PrefetchWorker
from factory_agent.config import FactoryAgentSettings
from factory_agent.data_api.result_cache import (
    PAYROLL_OPERATION,
    PROGRESS_OPERATION,
    ResultCachePolicy,
    derive_dh_date,
    footer_reconciles,
    window_category,
)
from factory_agent.data_api.token_gateway import TokenCredentialExchange
from factory_agent.ports.contracts import ResourceFetchResult

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

#: 只打印这些关键字相关的应用日志，避免把启动噪声全捞出来。
_LOG_KEYWORDS: tuple[str, ...] = (
    "v2_result",
    "prefetch",
    "cache.store",
    "cache_shadow",
)


# --------------------------------------------------------------------------- io


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}", flush=True)


def ok(message: str) -> None:
    print(f"   [ OK ] {message}", flush=True)


def bad(message: str) -> None:
    print(f"   [FAIL] {message}", flush=True)


def info(message: str) -> None:
    print(f"   [ .. ] {message}", flush=True)


def mask(value: str | None) -> str:
    """只暴露「有没有配」和长度，绝不打印凭据本身。"""
    if not value:
        return "<空>"
    return f"<已配置, len={len(value)}>"


def load_env(path: Path) -> tuple[int, int]:
    """兜底把 .env 灌进 os.environ；已存在的真实环境变量优先。

    现实（2026-10-11 实测）：应用自身并没有配置 ``env_file``，``.env`` 其实是
    **litellm 在 import 时调用 ``load_dotenv()``** 顺带加载的，而 ``load_dotenv()``
    按**进程 CWD** 找文件——所以配置生效的前提是「启动时的 CWD 是仓库根目录」。
    CWD 一旦不对（比如在 /tmp 下启动），所有 ``FACTORY_AGENT_*`` 静默消失、
    全部退回默认值。这里显式再兜一层，让脚本自身不依赖那个副作用。

    返回 ``(载入前已有的 FACTORY_AGENT_* 个数, 本次新载入个数)``。
    """
    already = sum(1 for key in os.environ if key.startswith("FACTORY_AGENT_"))
    if not path.is_file():
        return already, 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")
            loaded += 1
    return already, loaded


def install_log_sink() -> None:
    """把应用内部的 v2_result / prefetch 日志行直接打到 stdout。

    这是关键：``run_once`` 把「门禁拒绝 / 存储不可达 / 取数不完整」都吞成日志，
    不接 sink 的话它们在脚本里是看不见的。
    """
    from loguru import logger as loguru_logger

    def _interesting(record: Any) -> bool:
        message = str(record["message"])
        return any(token in message for token in _LOG_KEYWORDS)

    loguru_logger.add(
        sys.stdout,
        level="DEBUG",
        colorize=False,
        format="   [app ] {time:HH:mm:ss} | {level: <7} | {message} {extra}",
        filter=_interesting,
    )


# ------------------------------------------------------------------------ steps


def step_settings() -> FactoryAgentSettings:
    rule("1/7 配置")
    already, loaded = load_env(ENV_FILE)
    info(f"CWD = {Path.cwd()}")
    info(
        f"环境里已有 FACTORY_AGENT_* {already} 个（litellm 导入时 load_dotenv() 灌的）"
        f"，本脚本另补 {loaded} 个"
    )
    if already == 0 and loaded == 0:
        bad("一个 FACTORY_AGENT_* 都没有：.env 没被加载，后面的判断全是默认值")
    settings = FactoryAgentSettings()
    info(f"environment               = {settings.environment}")
    info(f"redis_url                 = {settings.redis_url}")
    info(f"result_cache_mode         = {settings.result_cache_mode}")
    info(f"prefetch_enabled          = {settings.result_cache_prefetch_enabled}")
    info(f"backtrack_days            = {settings.result_cache_backtrack_days}")
    info(
        f"ttl  unsettled/settled    = {settings.result_cache_unsettled_ttl_seconds} / "
        f"{settings.result_cache_settled_ttl_seconds}"
    )
    info(f"canonical_mes_base_url    = {settings.canonical_mes_base_url}")
    info(f"mes_user_credential_99    = {mask(settings.mes_user_credential_99.get_secret_value())}")
    info(f"mes_page_size/max_pages   = {settings.mes_page_size} / {settings.mes_max_pages}")

    blockers: list[str] = []
    if settings.redis_url is None:
        blockers.append("FACTORY_AGENT_REDIS_URL 未配置")
    if settings.canonical_mes_base_url is None:
        blockers.append("FACTORY_AGENT_CANONICAL_MES_BASE_URL 未配置")
    if not settings.mes_user_credential_99.get_secret_value().strip():
        blockers.append("FACTORY_AGENT_MES_USER_CREDENTIAL_99 为空")
    if not settings.result_cache_prefetch_enabled:
        blockers.append("FACTORY_AGENT_RESULT_CACHE_PREFETCH_ENABLED=false")
    if settings.result_cache_mode not in ("readwrite", "shadow"):
        blockers.append(
            f"result_cache_mode={settings.result_cache_mode!r}（不是 readwrite/shadow，"
            "writes=False → 永远不落盘）"
        )
    if blockers:
        bad("预取构建前提不满足：")
        for item in blockers:
            print(f"          - {item}", flush=True)
    else:
        ok("预取构建前提全部满足")
    return settings


async def step_redis(settings: FactoryAgentSettings) -> tuple[Any, Any]:
    rule("2/7 Redis 可达性")
    cache = boot._build_cache(settings)  # noqa: SLF001 - 与运行中的应用同键，刻意的
    if cache is None:
        bad("_build_cache 返回 None：Redis 未配置，后面的步骤全部会空转")
        raise SystemExit(1)
    reachable = await cache.ping()
    (ok if reachable else bad)(
        f"ping → {reachable}（False 说明 Redis 连不上，读会退化成源、写会被吞掉）"
    )
    return cache, cache  # 同一个对象；保留两个名字便于阅读


def step_result_cache(settings: FactoryAgentSettings, cache: Any) -> tuple[Any, Any]:
    rule("3/7 结果集缓存构建")
    result_cache = boot._build_result_cache(settings, cache, boot.SystemClock())
    if result_cache is None:
        bad("_build_result_cache 返回 None：结果集缓存整体关闭，预取 worker 不会构建")
        raise SystemExit(1)
    mode = result_cache.mode
    info(f"mode       = {mode.value}")
    info(f"reads      = {mode.reads}")
    info(f"writes     = {mode.writes}   ← 只有 True 才可能落盘")
    info(f"verifies   = {mode.verifies}（True = shadow：命中后仍实时比对）")
    policy = result_cache._policy  # noqa: SLF001 - 只读，用于离线复算门禁
    info(f"float_tolerance = {policy.float_tolerance}")
    if not mode.writes:
        bad("writes=False：模式不允许写，缓存永远空")
        raise SystemExit(1)
    ok("结果集缓存允许写入")
    return result_cache, policy


async def step_owner_token(
    settings: FactoryAgentSettings,
) -> tuple[TokenCredentialExchange, Any]:
    rule("4/7 99（老板）凭据换取")
    exchange = TokenCredentialExchange(
        str(settings.canonical_mes_base_url),
        refresh_threshold_seconds=settings.mes_token_refresh_threshold_seconds,
    )
    started = time.perf_counter()
    try:
        principal = await exchange.authenticate(
            settings.mes_user_credential_99.get_secret_value().strip()
        )
    except Exception as error:  # noqa: BLE001 - 诊断脚本要把失败原样摊开
        bad(f"99 凭据换取失败（{type(error).__name__}）：{error}")
        print(
            "          → 预取的取数器在这一步就断了；应用日志里会看到 "
            "v2_result.prefetch.fetch_failed",
            flush=True,
        )
        raise SystemExit(1) from error
    elapsed = (time.perf_counter() - started) * 1000
    ok(f"凭据换取成功（{elapsed:.0f}ms）")
    info(f"role            = {principal.role.value}（MES code {principal.role.mes_code}）")
    info(f"tenant_id       = {principal.credential.tenant_id}")
    info(f"user_id         = {principal.credential.user_id}")
    info(f"display_name    = {principal.display_name}")
    info(f"bound_dept_ids  = {[str(d) for d in principal.bound_dept_ids] or '<无绑定>'}")
    if principal.role.mes_code != "99":
        bad(
            "这个凭据换到的角色不是 99 → 写全厂缓存键的前提（Role.OWNER）不成立，"
            "写会被 _maybe_write 直接跳过"
        )
    return exchange, principal


class AlertPrinter:
    """把 worker 的告警原样打出来（生产里它进告警通道，这里进 stdout）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def alert(self, kind: str, detail: dict[str, Any]) -> None:
        self.calls.append((kind, detail))
        print(f"   [ALERT] {kind} {detail}", flush=True)


class RecordingFetcher:
    """包住真实的 99 取数器，记录每族耗时 / 行数 / 完整性，异常原样抛出。

    99 视角的整年取数很贵（实测 GongziClQuery 17.3 万行 ≈ 55s，且直接压客户
    MES），所以取数成功后把行集落到临时文件；``--reuse-fetch`` 时直接读回并校验
    窗口/参数一致，反复诊断不再重复打 MES。
    """

    def __init__(
        self,
        inner: Any,
        *,
        reuse: bool = False,
        cache_dir: Path | None = None,
    ) -> None:
        self._inner = inner
        self._reuse = reuse
        self._cache_dir = cache_dir or Path(tempfile.gettempdir())
        self.results: list[dict[str, Any]] = []

    def tenant_id(self) -> Any:
        return self._inner.tenant_id()

    def _path(self, operation_id: str) -> Path:
        return self._cache_dir / f"fa_prefetch_fetch_{operation_id}.json"

    def _load(
        self, operation_id: str, window: tuple[str, str], extra_params: Mapping[str, Any]
    ) -> ResourceFetchResult | None:
        path = self._path(operation_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存坏了就走实时
            return None
        if payload.get("window") != list(window) or payload.get("extra_params") != dict(
            extra_params
        ):
            info(f"（{operation_id} 的本地取数缓存与本次窗口/参数不一致，忽略）")
            return None
        return ResourceFetchResult(
            rows=tuple(payload["rows"]),
            total=int(payload["total"]),
            pages_fetched=int(payload["pages_fetched"]),
            complete=bool(payload["complete"]),
            reason=payload.get("reason"),
            footer=payload.get("footer"),
        )

    def _save(
        self,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
        result: ResourceFetchResult,
    ) -> None:
        payload = {
            "operation_id": operation_id,
            "window": list(window),
            "extra_params": dict(extra_params),
            "total": result.total,
            "pages_fetched": result.pages_fetched,
            "complete": result.complete,
            "reason": result.reason,
            "footer": result.footer,
            "rows": result.rows,
        }
        try:
            self._path(operation_id).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - 落盘失败只影响复用，不影响本次诊断
            info(f"（{operation_id} 的取数结果写本地缓存失败，忽略）")

    async def __call__(
        self,
        operation_id: str,
        window: tuple[str, str],
        extra_params: Mapping[str, Any],
    ) -> ResourceFetchResult:
        if self._reuse:
            cached = self._load(operation_id, window, extra_params)
            if cached is not None:
                self.results.append(
                    {
                        "operation": operation_id,
                        "rows": cached.rows,
                        "total": cached.total,
                        "pages_fetched": cached.pages_fetched,
                        "complete": cached.complete,
                        "reason": cached.reason,
                        "footer": cached.footer,
                        "elapsed_ms": 0.0,
                        "reused": True,
                    }
                )
                return cached
        started = time.perf_counter()
        try:
            result = await self._inner(operation_id, window, extra_params)
        except Exception as error:  # noqa: BLE001 - 记录后原样抛给 worker
            self.results.append(
                {
                    "operation": operation_id,
                    "error": f"{type(error).__name__}: {error}",
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                }
            )
            raise
        self._save(operation_id, window, extra_params, result)
        self.results.append(
            {
                "operation": operation_id,
                "rows": result.rows,
                "total": result.total,
                "pages_fetched": result.pages_fetched,
                "complete": result.complete,
                "reason": result.reason,
                "footer": result.footer,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
            }
        )
        return result


def explain_footer(
    operation: str,
    rows: tuple[Mapping[str, Any], ...],
    footer: Mapping[str, str] | None,
    tolerance: float,
) -> bool:
    """逐键复算 footer 对账，把「哪一列、期望多少、本地和多少、差多少」摊开。

    门禁规则（``footer_reconciles``）：每个 ``<col>_total`` 键 = 该列在所有行上的
    和；期望值为整数时**严格相等**，否则按 ``tolerance × max(|期望|,1)`` 的相对
    容差比较。返回是否全部通过。
    """
    if not footer:
        info(f"{operation}: 无 footer → 门禁视为通过")
        return True
    passed_all = True
    for key in sorted(footer):
        expected_raw = footer[key]
        if not key.endswith("_total"):
            print(f"       · {key}={expected_raw}（无 _total 后缀，门禁忽略）", flush=True)
            continue
        column = key[: -len("_total")]
        total = Decimal("0")
        missing = 0
        unparseable = 0
        for row in rows:
            value = row.get(column)
            if value is None or value == "":
                missing += 1
                continue
            try:
                total += Decimal(str(value))
            except Exception:  # noqa: BLE001 - 诊断脚本要数清坏值
                unparseable += 1
        try:
            expected = Decimal(str(expected_raw))
        except Exception:  # noqa: BLE001
            bad(f"       {key}: 期望值不可解析 {expected_raw!r}")
            passed_all = False
            continue
        if expected == expected.to_integral_value():
            passed = total == expected
            how = "整数列严格相等"
        else:
            scale = max(abs(float(expected)), 1.0)
            passed = abs(float(total) - float(expected)) <= tolerance * scale
            how = f"浮点列容差 {tolerance}×{scale:g}"
        passed_all = passed_all and passed
        flag = " OK " if passed else "FAIL"
        print(
            f"       [{flag}] {key}: 期望={expected} 本地和={total} "
            f"差={expected - total} 空/缺列={missing} 不可解析={unparseable}（{how}）",
            flush=True,
        )
    return passed_all


def report_fetch(entry: dict[str, Any], policy: ResultCachePolicy) -> None:
    """第 5 步：逐族取数结果 + 第 6 步的门禁复演。"""
    operation = entry["operation"]
    if "error" in entry:
        bad(f"{operation}: 取数抛异常 —— {entry['error']}")
        return
    rows = entry["rows"]
    footer = entry["footer"]
    print(
        f"   [ .. ] {operation}: rows={len(rows)} total={entry['total']} "
        f"pages={entry['pages_fetched']} complete={entry['complete']} "
        f"reason={entry['reason']} {entry['elapsed_ms']:.0f}ms",
        flush=True,
    )
    if entry.get("reused"):
        info("（本次为 --reuse-fetch，行集来自本地缓存，未再打 MES）")
    info(f"     footer = {dict(footer) if footer else '<无>'}")
    if rows:
        columns = sorted(str(key) for key in rows[0])
        info(f"     行内列（{len(columns)}）= {columns}")
    if not entry["complete"]:
        bad(f"{operation}: 行集不完整 → 门禁绝不入缓存（应用日志 v2_result.prefetch.incomplete）")
        return

    gate_ok = footer_reconciles(rows, footer, tolerance=policy.float_tolerance)
    if not gate_ok:
        print("       —— 门禁逐键复算（权威判定来自 footer_reconciles）——", flush=True)
        explain_footer(operation, rows, footer, policy.float_tolerance)
    gate_verdict = "通过" if gate_ok else "不通过 → write_gate_rejected，不写缓存"
    (ok if gate_ok else bad)(f"{operation}: footer 对账 {gate_verdict}")
    if operation == PROGRESS_OPERATION:
        broken = sum(1 for row in rows if derive_dh_date(row.get("dh")) is None)
        (ok if broken == 0 else bad)(
            f"{operation}: dh 派生 失败行数={broken}"
            + ("" if broken == 0 else " → 整批弃用，不写缓存（v2_result.dh_date_parse_failed）")
        )
    if operation == PAYROLL_OPERATION:
        info(f"{operation}: 明细模式（无 groupbyFields）→ cacheable_grain=True")


def step_plans(settings: FactoryAgentSettings) -> tuple[tuple[str, dict[str, Any]], ...]:
    plans = tuple(prefetch_module.PREFETCH_PLANS)
    print("\n   —— 预取计划 ——", flush=True)
    for operation, extra in plans:
        info(f"{operation}: extra_params={extra}")
    return plans


async def step_run(
    settings: FactoryAgentSettings,
    result_cache: Any,
    policy: ResultCachePolicy,
    exchange: TokenCredentialExchange,
    plans: tuple[tuple[str, dict[str, Any]], ...],
    *,
    dry_run: bool,
    reuse_fetch: bool,
) -> tuple[dict[str, int], RecordingFetcher]:
    rule("5/7 + 6/7 取数与落盘")
    real_fetcher = boot._Owner99PrefetchFetcher(  # noqa: SLF001 - 与生产同一条链路
        settings,
        exchange,
        settings.mes_user_credential_99.get_secret_value().strip(),
        mes_recorder=boot.ContextVarMesCallRecorder(),
    )
    fetcher = RecordingFetcher(real_fetcher, reuse=reuse_fetch)
    # 先显式 bind 一次：凭据已在第 4 步换取并缓存，这里不会再打 MES；作用是让
    # tenant_id 在任何模式下都可用（--reuse-fetch 时取数器不会被调用），并把
    # bind 失败提前暴露在长取数之前。
    try:
        await real_fetcher._bind()  # noqa: SLF001 - 与生产同一条链路
        ok(f"99 取数器已 bind，tenant_id = {real_fetcher.tenant_id()}")
    except Exception as error:  # noqa: BLE001 - 诊断脚本要把失败原样摊开
        bad(f"99 取数器 bind 失败：{type(error).__name__}: {error}")
        raise SystemExit(1) from error
    alerts = AlertPrinter()
    worker = V2PrefetchWorker(
        fetcher,
        result_cache,
        tenant_id=None,
        tenant_resolver=fetcher.tenant_id,
        prefetch_hour=settings.result_cache_prefetch_hour,
        jitter_seconds=settings.result_cache_prefetch_jitter_seconds,
        poll_seconds=settings.result_cache_prefetch_poll_seconds,
        timezone_name=settings.factory_timezone,
        clock=None,
        alerts=alerts,
    )
    today = worker._now().astimezone(worker._timezone).date().isoformat()  # noqa: SLF001
    window = result_cache.prefetch_window(today)
    info(f"预取窗口 = {window[0]} .. {window[1]}（today={today}）")
    info(
        f"窗口分类 = {window_category(window[0], window[1], today, policy.backtrack_days)}"
        f" → TTL {policy.settled_ttl_seconds}s"
    )

    if dry_run:
        info("dry-run：只取数，不写缓存")
        for operation, extra in plans:
            try:
                await fetcher(operation, window, extra)
            except Exception:  # noqa: BLE001 - 已经不完整，继续跑另一族
                pass
        stored: dict[str, int] = {}
    else:
        started = time.perf_counter()
        stored = await worker.run_once()
        info(f"run_once 耗时 {(time.perf_counter() - started):.0f}s → {stored}")

    print("\n   —— 逐族明细 ——", flush=True)
    for entry in fetcher.results:
        report_fetch(entry, policy)

    print("\n   —— 落盘结论 ——", flush=True)
    for operation, _ in plans:
        got = stored.get(operation)
        if got is None:
            info(f"{operation}: 未执行")
        elif got:
            ok(f"{operation}: 落盘 {got} 行")
        else:
            bad(f"{operation}: 落盘 0 行（上面的断言已指出断在哪一环）")
    if alerts.calls:
        print(f"\n   告警共 {len(alerts.calls)} 条：", flush=True)
        for kind, detail in alerts.calls:
            print(f"     - {kind} {detail}", flush=True)
    if not dry_run and not fetcher.tenant_id():
        bad("tenant_id 仍是空：99 取数器从未成功 bind，写缓存会落到 'unknown' 租户键")
    return stored, fetcher


async def step_keyspace(cache: Any) -> int:
    rule("7/7 Redis 键空间快照")
    client = cache._store._client  # noqa: SLF001 - 只读快照
    total = await client.dbsize()
    keys = sorted(await client.keys("fa:*"))
    info(f"db0 总键数 = {total}；fa:* = {len(keys)}")
    for key in keys[:20]:
        text = key.decode() if isinstance(key, bytes) else str(key)
        ttl = await client.ttl(key)
        print(f"     ttl={ttl:>7}s  {text[:150]}", flush=True)
    if len(keys) > 20:
        info(f"... 其余 {len(keys) - 20} 个键略")
    return len(keys)


# ------------------------------------------------------------------------ main


async def run(args: argparse.Namespace) -> int:
    install_log_sink()
    settings = step_settings()
    cache, _ = await step_redis(settings)
    result_cache, policy = step_result_cache(settings, cache)
    exchange, _ = await step_owner_token(settings)
    plans = step_plans(settings)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {operation for operation, _ in plans}
        if unknown:
            bad(f"--only 里有未知操作：{sorted(unknown)}")
            return 1
        plans = tuple((op, extra) for op, extra in plans if op in wanted)
        prefetch_module.PREFETCH_PLANS = plans  # run_once 读的是模块级常量

    stored, recompute = await step_run(
        settings,
        result_cache,
        policy,
        exchange,
        plans,
        dry_run=args.dry_run,
        reuse_fetch=args.reuse_fetch,
    )
    written = await step_keyspace(cache)

    rule("结论")
    if args.dry_run:
        info("dry-run 结束：未写缓存，只看断点")
        return 0
    if written:
        ok(f"缓存里现有 {written} 个 fa:* 键 —— 写通路是好的")
        return 0
    bad("一个键都没落盘")
    if not any(entry.get("rows") for entry in recompute.results):
        print("          → 取数本身没拿到行（先看上一步的 MES 报错）", flush=True)
    else:
        print(
            "          → 取到了行却没写进去：看上面的门禁断言与 [app] 行"
            "（write_gate_rejected / dh_date_parse_failed / store_unavailable）",
            flush=True,
        )
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="以 99 视角诊断 V2 结果集缓存落盘")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只取数并复算门禁，不写缓存",
    )
    parser.add_argument(
        "--reuse-fetch",
        action="store_true",
        help="复用上次落盘的取数结果（不打 MES），用于反复诊断门禁",
    )
    parser.add_argument(
        "--only",
        default="",
        help="只跑指定操作（逗号分隔），如 'GongziClQuery' 或 'ScjdQuery'",
    )
    return parser.parse_args(argv)


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
