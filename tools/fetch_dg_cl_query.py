"""直连客户 MES：取 token 三要素 → 调「吊挂工序产能」DgClQuery → 结果落 JSON。

调试台（tools/mes_console）的超时是固定 120s，大页（size=20000）会超时；
本脚本把业务请求超时放到 300s（可用 --timeout 覆盖），并把完整响应存盘。

用法（仓库根目录）：

    set -a; source .env; set +a
    .venv/bin/python tools/fetch_dg_cl_query.py

默认行为：角色 99（老板）、page=1、size=20000、dates=2026-07-01、datee=2026-07-31，
结果写入 cache/dg_cl_query_99_2026-07-01_2026-07-31.json。

常用覆盖：

    --role 99            00=员工 / 01=组长 / 02=管理 / 99=老板
    --size 2000          分页大小
    --dates 2026-07-01 --datee 2026-07-31
    --timeout 300        业务请求超时（秒）
    --out some/path.json 输出路径
    --show-secrets       打印 app_key/sign/accessToken 明文（默认脱敏）

凭据值默认脱敏，不落日志。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = "/api/system/token"
DG_CL_QUERY_PATH = "/api/NetYf/Dg/DgClQuery"

DEFAULT_BASE = "http://hzlinkbiz.ywhzsoft.com:9002"
DEFAULT_ROLE = "99"
DEFAULT_PAGE = 1
DEFAULT_SIZE = 20000
DEFAULT_DATES = "2026-07-01"
DEFAULT_DATEE = "2026-07-31"
DEFAULT_TIMEOUT = 300.0
TOKEN_TIMEOUT = 30.0

ROLES = {"00", "01", "02", "99"}


def _mask(value: Any) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}(len={len(text)})"


def _reveal(value: Any, show: bool) -> str:
    return str(value or "") if show else _mask(value)


def exchange_token(
    client: httpx.Client, base: str, role: str, *, show_secrets: bool
) -> dict[str, Any]:
    """POST 认证接口换取 token，返回 result（含 appkey/timestamp/sign/accessToken）。"""
    cred = os.environ.get(f"MES_USER_CREDENTIAL_{role}")
    if not cred:
        sys.exit(
            f"缺少环境变量 MES_USER_CREDENTIAL_{role}（先在仓库根目录执行 "
            f"set -a; source .env; set +a）"
        )

    print(f"[1/2] 换取 token: POST {base}{TOKEN_PATH}  (role={role})")
    started = time.perf_counter()
    try:
        resp = client.post(f"{base}{TOKEN_PATH}", json={"app_key": cred})
    except httpx.HTTPError as exc:
        sys.exit(f"token 接口不可达: {exc}")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    try:
        payload = resp.json()
    except ValueError:
        sys.exit(f"token 接口返回非 JSON: http={resp.status_code} body={resp.text[:500]}")

    if payload.get("code") != 1:
        sys.exit(
            f"token 交换失败: http={resp.status_code} "
            f"code={payload.get('code')} message={payload.get('message')!r}"
        )

    result = payload.get("result") or {}
    print(
        f"      http={resp.status_code} code={payload.get('code')} {elapsed_ms}ms "
        f"user={result.get('user')} uname={result.get('uname')} dept={result.get('dept')}"
    )
    print("      三要素：")
    print(f"        app_key   = {_reveal(result.get('appkey'), show_secrets)}")
    print(f"        timestamp = {_reveal(result.get('timestamp'), show_secrets)}")
    print(f"        sign      = {_reveal(result.get('sign'), show_secrets)}")
    print(f"        accessToken = {_reveal(result.get('accessToken'), show_secrets)}")
    return result


def call_dg_cl_query(
    client: httpx.Client,
    base: str,
    token: dict[str, Any],
    *,
    page: int,
    size: int,
    dates: str,
    datee: str,
) -> tuple[int, float, Any, str | None]:
    """调用吊挂工序产能接口，返回 (http 状态码, 耗时 ms, 响应体, 原始文本或 None)。"""
    body = {
        "app_key": token["appkey"],
        "timestamp": token["timestamp"],
        "sign": token["sign"],
        "page": page,
        "size": size,
        "dates": dates,
        "datee": datee,
    }
    # 凭据脱敏打印，只暴露业务参数便于核对。
    print(
        f"[2/2] 调用吊挂工序产能: POST {base}{DG_CL_QUERY_PATH}  "
        f"body={{page: {page}, size: {size}, dates: {dates!r}, datee: {datee!r}, "
        f"app_key/timestamp/sign: <已注入>}}"
    )
    print(f"      超时 = {client.timeout.read}s（等待中…）")

    started = time.perf_counter()
    try:
        resp = client.post(
            f"{base}{DG_CL_QUERY_PATH}",
            json=body,
            headers={"Authorization": f"Bearer {token['accessToken']}"},
        )
    except httpx.TimeoutException as exc:
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        sys.exit(f"MES 请求超时（已等待 {elapsed}ms）: {exc}")
    except httpx.HTTPError as exc:
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        sys.exit(f"MES 请求失败（{elapsed}ms）: {exc}")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    try:
        return resp.status_code, elapsed_ms, resp.json(), None
    except ValueError:
        return resp.status_code, elapsed_ms, None, resp.text[:100_000]


def summarize(payload: Any) -> None:
    if not isinstance(payload, dict):
        print("      响应不是 JSON 对象，已原样保存")
        return
    result = payload.get("result") or {}
    rows = result.get("list") if isinstance(result, dict) else None
    rows = rows if isinstance(rows, list) else []
    print(
        f"      code={payload.get('code')} message={payload.get('message')!r} "
        f"total={result.get('total') if isinstance(result, dict) else None} rows={len(rows)}"
    )
    if rows:
        print("      首行字段：")
        print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        # 汇总产量（sl）与金额（je），先给个量级参考。
        sl = sum(r.get("sl") or 0 for r in rows if isinstance(r, dict))
        je = sum(r.get("je") or 0 for r in rows if isinstance(r, dict))
        print(f"      本页 sl 合计={sl}  je 合计={je}")
    if isinstance(result, dict) and "footer" in result:
        print("      footer：")
        print(json.dumps(result["footer"], ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="直连 MES：吊挂工序产能 DgClQuery 取数并落盘")
    parser.add_argument("--role", default=DEFAULT_ROLE, choices=sorted(ROLES))
    parser.add_argument("--page", type=int, default=DEFAULT_PAGE)
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--dates", default=DEFAULT_DATES, help="开始日期，如 2026-07-01")
    parser.add_argument("--datee", default=DEFAULT_DATEE, help="结束日期，如 2026-07-31")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="业务请求读超时（秒），默认 300"
    )
    parser.add_argument("--out", default="", help="输出 JSON 路径（默认写入 cache/）")
    parser.add_argument("--show-secrets", action="store_true", help="打印凭据明文")
    args = parser.parse_args()

    base = os.environ.get("FACTORY_AGENT_CANONICAL_MES_BASE_URL", DEFAULT_BASE).rstrip("/")
    print(f"MES base = {base}")

    out_path = (
        Path(args.out).expanduser()
        if args.out
        else ROOT / ".cache" / f"dg_cl_query_{args.role}_{args.dates}_{args.datee}.json"
    )

    timeout = httpx.Timeout(args.timeout, connect=10.0, write=30.0, pool=10.0)
    with httpx.Client(timeout=timeout) as client:
        token = exchange_token(client, base, args.role, show_secrets=args.show_secrets)
        http_status, elapsed_ms, payload, raw_text = call_dg_cl_query(
            client,
            base,
            token,
            page=args.page,
            size=args.size,
            dates=args.dates,
            datee=args.datee,
        )

    print(f"      http={http_status} {elapsed_ms}ms")
    summarize(payload)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(payload, ensure_ascii=False, indent=2)
        if payload is not None
        else (raw_text or "")
    )
    out_path.write_text(text, encoding="utf-8")
    print(f"\n已保存: {out_path}  ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
