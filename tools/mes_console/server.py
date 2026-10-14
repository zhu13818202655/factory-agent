"""MES 直连调试台 —— 本地代理服务（tools 专用，不经 factory-agent）。

页面在浏览器中无法直接访问客户 MES（跨域 + 鉴权），且凭据不应进入浏览器，因此本服务：

* 在 ``127.0.0.1`` 提供调试台页面（tools/mes_console/static/index.html）；
* 代理所有请求：按角色（00/01/02/99）从环境变量
  ``MES_USER_CREDENTIAL_<角色>`` 取加密凭据，先换 token（带缓存，过期自动重取），
  再把 app_key/timestamp/sign 三参平铺注入 body 并附加 ``Authorization: Bearer``；
* 认证接口本身（/api/system/token）不注入三参，直接以角色凭据换取 token；
* 单次上游请求读超时 300s / 连接 10s（见 ``TIMEOUT``）：客户大窗口、大页查询实测可达数分钟；
* 保存返回结果：写入 .cache 目录（默认 ``<仓库根>/.cache``，可用
  ``MES_CONSOLE_CACHE_DIR`` 覆盖）。

用法（仓库根目录，需先 source .env）：

    set -a; source .env; set +a
    uv run tools/mes_console/server.py            # 默认端口 8095

环境变量：
    MES_CONSOLE_PORT          页面端口，默认 8095
    MES_CONSOLE_CACHE_DIR     保存目录，默认 <仓库根>/.cache
    FACTORY_AGENT_CANONICAL_MES_BASE_URL  客户 MES 地址（来自 .env）

凭据值（app_key/sign/accessToken 等）绝不写入日志。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from catalog import TOKEN_PATH, as_json
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

BASE = os.environ.get(
    "FACTORY_AGENT_CANONICAL_MES_BASE_URL", "http://hzlinkbiz.ywhzsoft.com:9002"
).rstrip("/")
PORT = int(os.environ.get("MES_CONSOLE_PORT", "8095"))
CACHE_DIR = Path(os.environ.get("MES_CONSOLE_CACHE_DIR", str(ROOT / ".cache")))

#: 客户 timestamp 窗口（与 direct_mes_client.py 一致，秒）。
TOKEN_TTL_SECONDS = 2400
#: 单次上游请求超时：读 300s（客户大窗口 / 大页查询实测可跑 3 分钟以上）、连接 10s。
TIMEOUT = httpx.Timeout(300.0, connect=10.0)
TIMEOUT_SECONDS = 300

#: 客户实测必须以布尔发送的参数（防字符串 "1"/"true" 导致整包被拒）。
_BOOL_PARAMS = {"queryFooter"}
_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}

ROLES = (
    ("00", "员工", "查看本人的产量、工资与组内排名"),
    ("01", "组长", "查看本部门员工的产量与工资"),
    ("02", "管理", "查看本部门的产量/工资/订单进度汇总"),
    ("99", "老板", "查看全厂产量、工资与各订单进度"),
)

app = FastAPI(title="MES direct-mes console (dev only)")

_client: httpx.AsyncClient | None = None
_token_cache: dict[str, dict[str, Any]] = {}


def _client_instance() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


def _role_credential(role: str) -> str | None:
    return os.environ.get(f"MES_USER_CREDENTIAL_{role}") or None


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


def _normalize(business: dict[str, Any]) -> dict[str, Any]:
    """剔除空值并把已知布尔参数规整为真布尔（与 direct_mes_client 对齐）。"""
    out: dict[str, Any] = {}
    for key, value in business.items():
        if value is None or value == "":
            continue
        if key in _BOOL_PARAMS:
            value = _as_bool(value)
        out[key] = value
    return out


def _identity_of(token: dict[str, Any]) -> dict[str, str]:
    """token 结果中的非敏感身份信息（用于页面展示/自动回填 Uid）。"""
    return {
        "user": str(token.get("user") or ""),
        "uname": str(token.get("uname") or ""),
        "dept": str(token.get("dept") or ""),
        "roles": str(token.get("roles") or ""),
    }


async def _exchange_token(
    client: httpx.AsyncClient, role: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """单次 token 交换，返回 (原始响应 payload, result, identity)；失败抛 HTTPException。

    凭据值不落日志，错误信息只带 code/message。
    """
    cred = _role_credential(role)
    if cred is None:
        raise HTTPException(
            status_code=400,
            detail=f"角色 {role} 未配置凭据（缺少环境变量 MES_USER_CREDENTIAL_{role}）",
        )
    try:
        resp = await client.post(f"{BASE}{TOKEN_PATH}", json={"app_key": cred})
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"token 接口不可达或返回异常: {exc}") from exc

    if payload.get("code") != 1:
        raise HTTPException(
            status_code=502,
            detail=f"token 交换失败: code={payload.get('code')} message={payload.get('message')}",
        )
    result = payload["result"]
    return payload, result, _identity_of(result)


async def _cached_token(client: httpx.AsyncClient, role: str) -> dict[str, Any]:
    """取（带缓存，过期自动重取）token，返回 {result, identity, fresh}。"""
    entry = _token_cache.get(role)
    if entry and int(time.time()) - entry["fetched_at"] < TOKEN_TTL_SECONDS:
        return {"result": entry["result"], "identity": entry["identity"], "fresh": False}
    _, result, identity = await _exchange_token(client, role)
    _token_cache[role] = {"result": result, "identity": identity, "fetched_at": int(time.time())}
    return {"result": result, "identity": identity, "fresh": True}


@app.get("/")
async def index() -> Response:
    return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "base": BASE,
        "cacheDir": str(CACHE_DIR),
        "tokenPath": TOKEN_PATH,
        "timeoutSeconds": TIMEOUT_SECONDS,
        "roles": [
            {
                "code": code,
                "name": name,
                "scope": blurb,
                "configured": _role_credential(code) is not None,
            }
            for code, name, blurb in ROLES
        ],
    }


@app.get("/api/catalog")
async def catalog() -> list[dict[str, Any]]:
    return as_json()


@app.post("/api/invoke")
async def invoke(request: Request) -> JSONResponse:
    """按角色代理一次 MES 调用：自动换 token、注入三参 + Bearer，返回完整响应 JSON。"""
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None

    role = str(data.get("role") or "00")
    path = str(data.get("path") or "").lstrip("/")
    business = data.get("body") or {}
    autofill_uid = bool(data.get("autofillUid"))
    if not isinstance(business, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    if role not in {c for c, _, _ in ROLES}:
        raise HTTPException(status_code=400, detail=f"未知角色: {role}")

    client = _client_instance()
    started = time.perf_counter()
    is_token_endpoint = path == TOKEN_PATH.lstrip("/")

    if is_token_endpoint:
        # 认证接口本身：以角色凭据换取 token（强制刷新），原样返回其完整响应。
        payload, result, identity = await _exchange_token(client, role)
        _token_cache[role] = {
            "result": result,
            "identity": identity,
            "fetched_at": int(time.time()),
        }
        body_value: Any = payload
        http_status = 200
        fresh_token = True
    else:
        # 业务接口：先取 token（带缓存），再平铺注入三参 + Bearer。
        token = await _cached_token(client, role)
        result = token["result"]
        fresh_token = bool(token["fresh"])
        body = _normalize(business)
        if autofill_uid and "Uid" not in body and result.get("user"):
            body["Uid"] = str(result["user"])
        payload = {
            "app_key": result["appkey"],
            "timestamp": result["timestamp"],
            "sign": result["sign"],
            **body,
        }
        try:
            resp = await client.post(
                f"{BASE}/{path}",
                json=payload,
                headers={"Authorization": f"Bearer {result['accessToken']}"},
            )
        except httpx.HTTPError as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"MES 请求失败: {exc}") from exc
        http_status = resp.status_code
        try:
            body_value = resp.json()
        except ValueError:
            body_value = resp.text[:100_000]

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    # 不打印 body / 凭据，仅记录调用概况便于排查。
    print(
        f"[invoke] role={role} {'TOKEN' if is_token_endpoint else 'BUSINESS'} "
        f"path=/{path} http={http_status} {elapsed_ms}ms"
    )

    meta: dict[str, Any] = {
        "role": role,
        "path": f"/{path}",
        "http": http_status,
        "elapsed_ms": elapsed_ms,
        "token": "fresh" if fresh_token else "cached",
        "identity": token["identity"] if not is_token_endpoint else identity,
    }
    return JSONResponse({"ok": True, "meta": meta, "body": body_value})


_SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f]+\.json$", re.IGNORECASE)


def _safe_filename(name: str) -> str:
    """把用户输入规整为 .cache 目录内安全的 json 文件名。"""
    cleaned = (name or "").strip().replace("\\", "/").split("/")[-1].strip()
    if (
        cleaned in ("", ".", "..")
        or cleaned.startswith(".")
        or cleaned.lower().endswith(".json") is False
    ):
        cleaned = f"{cleaned or 'result'}.json"
    if not _SAFE_NAME.match(cleaned):
        raise HTTPException(
            status_code=400, detail="文件名不合法（仅允许单层文件名，可含中文，须以 .json 结尾）"
        )
    return cleaned


@app.post("/api/save")
async def save_result(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
    filename = _safe_filename(str(data.get("filename") or ""))
    content = data.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="缺少 content")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    try:
        text = (
            json.dumps(content, ensure_ascii=False, indent=2)
            if not isinstance(content, str)
            else content
        )
        target.write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"写入失败: {exc}") from exc
    return JSONResponse({"ok": True, "path": str(target), "filename": filename})


@app.get("/api/files")
async def list_saved() -> list[dict[str, Any]]:
    if not CACHE_DIR.is_dir():
        return []
    entries = []
    for p in CACHE_DIR.glob("*.json"):
        try:
            stat = p.stat()
            entries.append(
                {
                    "name": p.name,
                    "size": stat.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                }
            )
        except OSError:  # noqa: BLE001
            continue
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries[:50]


@app.get("/api/files/{name}")
async def read_saved(name: str) -> JSONResponse:
    filename = _safe_filename(name)
    target = CACHE_DIR / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        content = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # noqa: BLE001
        raise HTTPException(status_code=500, detail="读取或解析失败") from None
    return JSONResponse({"ok": True, "filename": filename, "content": content})


if __name__ == "__main__":
    print(f"MES 调试台: http://127.0.0.1:{PORT}   (cache: {CACHE_DIR})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
