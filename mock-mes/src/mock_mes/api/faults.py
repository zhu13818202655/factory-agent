from __future__ import annotations

import asyncio
from typing import override

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class FaultControlMiddleware(BaseHTTPMiddleware):
    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        fault = request.headers.get("X-Mock-Fault")
        if fault == "429":
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "1"},
                content={
                    "code": "rate_limited",
                    "message": "synthetic rate limit",
                    "trace_id": "00000000000000000000000000000000",
                },
            )
        if fault == "5xx":
            return JSONResponse(
                status_code=503,
                content={
                    "code": "upstream_unavailable",
                    "message": "synthetic upstream failure",
                    "trace_id": "00000000000000000000000000000000",
                },
            )
        if fault == "latency":
            raw_delay = request.headers.get("X-Mock-Latency-Ms", "100")
            try:
                delay_ms = min(max(int(raw_delay), 0), 2000)
            except ValueError:
                delay_ms = 100
            await asyncio.sleep(delay_ms / 1000)
        return await call_next(request)
