"""Dev-only demo frontend for the platform statistics surface.

Same pattern as ``tools/test_frontend/server.py``: the statistics routes have no
CORS and require ``Authorization: Bearer <token>``, so a browser page cannot call
them directly. This tiny server

  * serves the static dashboard (``./static``) on http://127.0.0.1:8082, and
  * reverse-proxies every other route to the application, injecting the Bearer
    token from the environment (never shipped to the browser).

Run (repo root, factory-agent venv):

    set -a; source .env; set +a
    .venv/bin/python tools/usage_admin_demo/server.py

Env::

    FACTORY_AGENT_STATISTICS_API_TOKEN  # the platform Bearer token to inject
    UA_DEMO_TOKEN                       # optional override for the above
    UA_DEMO_UPSTREAM                    # optional, default http://127.0.0.1:8000
    UA_DEMO_PORT                        # optional, default 8082

Tenant addressing
-----------------

The dashboard addresses the tenant registry by its ``tenant_ref``: a short,
non-secret handle returned by the list and detail responses. AppKey stays a
secret — shown in plaintext only in the single create response and masked
everywhere else — so it can never be used to build an admin path. That is why
this proxy needs no side cache of plaintext keys: the browser always holds a
usable handle.
"""



import os
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

UPSTREAM = os.environ.get("UA_DEMO_UPSTREAM", "http://127.0.0.1:8000").rstrip("/")
PORT = int(os.environ.get("UA_DEMO_PORT", "8082"))
TOKEN = os.environ.get("UA_DEMO_TOKEN") or os.environ.get(
    "FACTORY_AGENT_STATISTICS_API_TOKEN", ""
)

app = FastAPI(title="statistics demo frontend (dev only)")
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

    if not TOKEN:
        return JSONResponse(
            status_code=500,
            content={
                "detail": "FACTORY_AGENT_STATISTICS_API_TOKEN is not configured in the environment"
            },
        )

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in {"host", "authorization", "content-length", "transfer-encoding", "connection"}
    }
    headers["Authorization"] = f"Bearer {TOKEN}"

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
