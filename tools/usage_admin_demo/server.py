"""Dev-only demo frontend for usage-admin (tenant registry + usage dashboard).

Same pattern as ``tools/test_frontend/server.py``: usage-admin has no CORS and
requires ``Authorization: Bearer <token>``, so a browser page cannot call it
directly. This tiny server

  * serves the static dashboard (``./static``) on http://127.0.0.1:8082, and
  * reverse-proxies every other route to usage-admin, injecting the Bearer
    token from the environment (never shipped to the browser).

Run (repo root, factory-agent venv):

    set -a; source .env; set +a
    .venv/bin/python tools/usage_admin_demo/server.py

Env::

    USAGE_ADMIN_DEMO_TOKEN   # optional, defaults to USAGE_ADMIN_API_TOKEN
    UA_DEMO_UPSTREAM         # optional, default http://127.0.0.1:8020
    UA_DEMO_PORT             # optional, default 8082
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

UPSTREAM = os.environ.get("UA_DEMO_UPSTREAM", "http://127.0.0.1:8020").rstrip("/")
PORT = int(os.environ.get("UA_DEMO_PORT", "8082"))
TOKEN = os.environ.get("USAGE_ADMIN_DEMO_TOKEN") or os.environ.get("USAGE_ADMIN_API_TOKEN", "")

app = FastAPI(title="usage-admin demo frontend (dev only)")
_client: httpx.AsyncClient | None = None


@app.get("/api/status")
async def status() -> dict[str, object]:
    return {"token_configured": bool(TOKEN), "upstream": UPSTREAM}


@app.get("/")
async def index() -> Response:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(path: str, request: Request) -> Response:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(300.0, connect=10.0))

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in {"host", "authorization", "content-length", "transfer-encoding", "connection"}
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    else:
        return JSONResponse(
            status_code=500,
            content={"detail": "USAGE_ADMIN_API_TOKEN is not configured in the environment"},
        )

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
