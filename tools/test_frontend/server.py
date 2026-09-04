"""Dev-only test-frontend server for the four-role Factory Agent chat UI.

Why a proxy: ``factory-agent`` deliberately has no CORS middleware and resolves
identity exclusively from ``X-Factory-Credential`` (the customer MES app_key).
A browser page on another origin cannot add that header. This tiny server

  * serves the static chat UI (``./static``) on http://127.0.0.1:8081, and
  * reverse-proxies every other route to the upstream factory-agent, injecting
    ``X-Factory-Credential`` from the role selected by the caller
    (``X-Dev-Role: 00|01|02|99`` header).

The app_key therefore never reaches the browser; the UI only says which role it
is acting as. SSE responses are streamed through untouched so
``Last-Event-ID`` reconnect still works.

Run (from the repo root, factory-agent venv):

    set -a; source .env; set +a
    .venv/bin/python tools/test_frontend/server.py

Required env (put the real keys in your git-ignored ``.env``):

    MES_APP_KEY_00   # 员工
    MES_APP_KEY_01   # 组长
    MES_APP_KEY_02   # 管理
    MES_APP_KEY_99   # 老板
    FA_FE_UPSTREAM   # optional, default http://127.0.0.1:8000
    FA_FE_PORT       # optional, default 8081
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

UPSTREAM = os.environ.get("FA_FE_UPSTREAM", "http://127.0.0.1:8000").rstrip("/")
PORT = int(os.environ.get("FA_FE_PORT", "8081"))
ROLE_HEADER = "X-Dev-Role"
CREDENTIAL_HEADER = "X-Factory-Credential"

#: Role metadata for the selector screen (role codes are authoritative, from the
#: customer token; the scope blurb mirrors Appendix A of the frontend API doc).
ROLES = (
    ("00", "员工", "查看本人的产量、工资与组内排名"),
    ("01", "组长", "查看本部门员工的产量与工资"),
    ("02", "管理", "查看本部门的产量/工资/订单进度汇总"),
    ("99", "老板", "查看全厂产量、工资与各订单进度"),
)

app = FastAPI(title="factory-agent test frontend (dev only)")
_client: httpx.AsyncClient | None = None


def _role_key(role: str) -> str | None:
    return os.environ.get(f"MES_APP_KEY_{role}") or None


@app.get("/api/roles")
async def roles() -> list[dict[str, object]]:
    return [
        {
            "code": code,
            "name": name,
            "scope": blurb,
            "configured": _role_key(code) is not None,
        }
        for code, name, blurb in ROLES
    ]


@app.get("/")
async def index() -> Response:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/static/{name}")
async def static_file(name: str) -> Response:
    """Dev-mode static serving without caching (edits show up on plain refresh)."""
    base = STATIC_DIR.resolve()
    path = (base / name).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy(path: str, request: Request) -> Response:
    """Reverse-proxy to factory-agent with the role's credential injected."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(300.0, connect=10.0))

    role = request.headers.get(ROLE_HEADER, "99")
    # Health endpoints need no credential; let the pill work before any role
    # key is configured.
    needs_credential = not path.startswith("health/")
    key = _role_key(role) if needs_credential else None
    if needs_credential and key is None:
        return JSONResponse(
            status_code=400,
            content={"detail": f"role {role} has no configured app_key (MES_APP_KEY_{role})"},
        )

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", ROLE_HEADER.lower(), CREDENTIAL_HEADER.lower(), "content-length"}
    }
    if key is not None:
        headers[CREDENTIAL_HEADER] = key

    body = await request.body()
    upstream_request = _client.build_request(
        request.method,
        path,
        params=request.query_params,
        headers=headers,
        content=body or None,
    )

    upstream_response = await _client.send(upstream_request, stream=True)
    resp_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}
    }
    content_type = upstream_response.headers.get("content-type", "")

    async def stream_body():
        try:
            async for chunk in upstream_response.aiter_bytes():
                yield chunk
        finally:
            await upstream_response.aclose()

    if "text/event-stream" in content_type:
        return StreamingResponse(
            stream_body(),
            status_code=upstream_response.status_code,
            headers={**resp_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            media_type="text/event-stream",
        )
    data = await upstream_response.aread()
    return Response(
        content=data,
        status_code=upstream_response.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
