"""Fault injection middleware adapted to the customer envelope.

Opt-in via the ``X-Mock-Fault`` header; never active by default. Structural
faults (duplicate page, missing page, wrong total, footer/list mismatch, null
fields, field drift) are applied to list responses inside the envelope.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast, override

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class FaultControlMiddleware(BaseHTTPMiddleware):
    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        fault = request.headers.get("X-Mock-Fault")
        if fault == "429":
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "1"},
                content={
                    "code": 0,
                    "message": "synthetic rate limit",
                    "result": None,
                    "timestamp": 0,
                },
            )
        if fault == "5xx":
            return JSONResponse(
                status_code=503,
                content={
                    "code": 0,
                    "message": "synthetic upstream failure",
                    "result": None,
                    "timestamp": 0,
                },
            )
        if fault == "404":
            return JSONResponse(
                status_code=404,
                content={"code": 0, "message": "无登录权限", "result": None, "timestamp": 0},
            )
        if fault == "latency":
            raw_delay = request.headers.get("X-Mock-Latency-Ms", "100")
            try:
                delay_ms = min(max(int(raw_delay), 0), 2000)
            except ValueError:
                delay_ms = 100
            await asyncio.sleep(delay_ms / 1000)

        response = await call_next(request)
        if fault in (
            "duplicate_page",
            "missing_page",
            "wrong_total",
            "footer_mismatch",
            "null",
            "field_drift",
        ):
            footer_field = request.headers.get("X-Mock-Footer-Field", "sl_total")
            raw_delta = request.headers.get("X-Mock-Footer-Delta", "999")
            try:
                footer_delta = int(raw_delta)
            except ValueError:
                footer_delta = 999
            response = await _apply_structural_fault(
                response, fault, footer_field=footer_field, footer_delta=footer_delta
            )
        return response


async def _apply_structural_fault(
    response: Response, fault: str, *, footer_field: str = "sl_total", footer_delta: int = 999
) -> Response:
    # Buffer the streaming body produced by BaseHTTPMiddleware.call_next.
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        chunks.append(cast(bytes, chunk))
    body_bytes = b"".join(chunks)
    try:
        payload = json.loads(body_bytes)
    except ValueError:
        return Response(
            content=body_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
    payload = cast(dict[str, Any], payload)
    result = payload.get("result")
    if not isinstance(result, dict):
        return Response(
            content=body_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    result = cast(dict[str, Any], result)
    if not isinstance(result.get("list"), list):
        return Response(
            content=body_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
    items = cast(list[dict[str, Any]], result["list"])
    if fault == "duplicate_page" and items:
        items.append(dict(items[0]))
    elif fault == "missing_page":
        result["list"] = []
    elif fault == "wrong_total":
        result["total"] = int(result.get("total", 0)) + 7
    elif fault == "footer_mismatch" and isinstance(result.get("footer"), dict):
        # Footer disagrees with the visible rows: contract drift detection case.
        # The field and delta are configurable so wage (je_total) and output
        # (sl_total) reconciliations can both be faulted.
        footer = cast(dict[str, Any], result["footer"])
        current = footer.get(footer_field, "0") or "0"
        try:
            current_value = float(current)
        except ValueError:
            current_value = 0.0
        footer[footer_field] = str(current_value + footer_delta)
    elif fault == "null" and items:
        target = next((key for key in items[0] if key.endswith("_id")), next(iter(items[0])))
        items[0][target] = None
    elif fault == "field_drift" and items:
        items[0]["synthetic_drift_field"] = "unexpected"

    return Response(
        content=json.dumps(payload, ensure_ascii=False).encode(),
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type="application/json",
    )
