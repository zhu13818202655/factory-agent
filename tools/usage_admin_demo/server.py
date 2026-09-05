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
    UA_DEMO_KEYRING          # optional, path of the plaintext-key cache

The AppKey keyring
------------------

usage-admin masks every AppKey except in the single create response (D9), so the
registry list only carries ``fac-37***``. A masked key is a lossy truncation,
not encryption: nothing can recover the plaintext from it, and the UI therefore
cannot disable or re-enable a tenant by sending the value it just displayed.

This dev-only server keeps the missing link: it records the plaintext key from
each create response and rewrites tenant-by-key routes back to the plaintext
before they reach usage-admin. The production contract stays untouched — the
list endpoint keeps masking, and only this local process ever sees plaintext.

SECURITY: the keyring file holds plaintext AppKeys on local disk. It lives in
the git-ignored ``/data/`` directory and must never be copied anywhere else.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from usage_admin.masking import mask_app_key

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
REPOSITORY_ROOT = ROOT.parents[1]

UPSTREAM = os.environ.get("UA_DEMO_UPSTREAM", "http://127.0.0.1:8020").rstrip("/")
PORT = int(os.environ.get("UA_DEMO_PORT", "8082"))
TOKEN = os.environ.get("USAGE_ADMIN_DEMO_TOKEN") or os.environ.get("USAGE_ADMIN_API_TOKEN", "")
KEYRING_PATH = Path(
    os.environ.get("UA_DEMO_KEYRING", REPOSITORY_ROOT / "data" / "usage_admin_demo_keyring.json")
)

#: ``/admin/v1/tenants/registry/<urlencoded app_key>`` and its ``/enable`` suffix.
_TENANT_KEY_ROUTE = re.compile(r"^admin/v1/tenants/registry/(?P<key>[^/]+?)(?P<suffix>/enable)?$")
#: bare create endpoint, with or without trailing slash, no path param.
_TENANT_CREATE_PATH = "admin/v1/tenants/registry"

app = FastAPI(title="usage-admin demo frontend (dev only)")
_client: httpx.AsyncClient | None = None

#: masked AppKey -> plaintext AppKey, mirrored to ``KEYRING_PATH``.
_keyring: dict[str, str] = {}


def _load_keyring() -> None:
    try:
        raw = json.loads(KEYRING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(raw, dict):
        _keyring.update({str(k): str(v) for k, v in raw.items() if isinstance(v, str)})


def _store_plaintext(app_key: str) -> None:
    """Remember a plaintext key seen once (create response or manual entry)."""
    if not app_key:
        return
    masked = mask_app_key(app_key)
    if masked is None or _keyring.get(masked) == app_key:
        return
    _keyring[masked] = app_key
    _persist_keyring()


def _persist_keyring() -> None:
    KEYRING_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEYRING_PATH.write_text(
        json.dumps(_keyring, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


@app.get("/api/status")
async def status() -> dict[str, object]:
    return {"token_configured": bool(TOKEN), "upstream": UPSTREAM}


@app.get("/api/keyring")
async def keyring() -> dict[str, object]:
    """Masked keys this demo can operate on (never returns plaintext)."""
    return {"masked": sorted(_keyring), "path": str(KEYRING_PATH)}


@app.post("/api/keyring")
async def add_to_keyring(request: Request) -> dict[str, object]:
    """Register a plaintext AppKey so its registry row becomes operable.

    Use this for tenants created outside this browser session: the create toast
    is the only place usage-admin ever shows the plaintext key.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "body must be JSON"})
    app_key = str(body.get("app_key", "")).strip() if isinstance(body, dict) else ""
    if not app_key:
        return JSONResponse(status_code=422, content={"detail": "app_key is required"})
    masked = mask_app_key(app_key)
    _keyring[str(masked)] = app_key
    _persist_keyring()
    return {"masked": masked}


@app.delete("/api/keyring")
async def clear_keyring() -> dict[str, object]:
    _keyring.clear()
    _persist_keyring()
    return {"masked": []}


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

    # A masked AppKey is unusable as a route parameter; swap in the plaintext.
    path = _with_plaintext_key(path)

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
    _remember_created_key(request.method, path, upstream_response.status_code, data)
    return Response(
        content=data,
        status_code=upstream_response.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


def _with_plaintext_key(path: str) -> str:
    """Rewrite ``.../registry/<masked key>`` to the plaintext key when known."""
    match = _TENANT_KEY_ROUTE.match(path)
    if match is None:
        return path
    candidate = unquote(match.group("key"))
    plaintext = _keyring.get(candidate)
    if plaintext is None:
        return path
    suffix = match.group("suffix") or ""
    return f"admin/v1/tenants/registry/{quote(plaintext, safe='')}{suffix}"


def _remember_created_key(method: str, path: str, status_code: int, body: bytes) -> None:
    """Cache the plaintext key carried by the single create response (D9).

    The create endpoint has no key in its path (``POST .../registry``), so a
    separate match from the by-key routes is needed to avoid missing the
    cache write and silently regressing every later operation.
    """
    if method != "POST" or status_code != 201:
        return
    if path.rstrip("/") != _TENANT_CREATE_PATH:
        return
    try:
        payload = json.loads(body)
    except ValueError:
        return
    if isinstance(payload, dict):
        _store_plaintext(str(payload.get("app_key", "")))


if __name__ == "__main__":
    _load_keyring()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
