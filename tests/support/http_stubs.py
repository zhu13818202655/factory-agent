"""Deterministic in-process HTTP transports for adapter unit tests."""

from __future__ import annotations

from typing import Any

import httpx


class _AsyncStubTransport(httpx.AsyncBaseTransport):
    """Shared async request handling for all stubs."""

    def _respond(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover - hook
        raise NotImplementedError

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._respond(request)


class StubTransport(_AsyncStubTransport):
    """Return a fixed status with a Canonical error body."""

    def __init__(self, status_code: int) -> None:
        self._status_code = status_code

    def _respond(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self._status_code,
            json={"code": "stub", "message": "stub", "trace_id": "0" * 32},
            request=request,
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self, base_url="http://mock.invalid")


class SequenceTransport(_AsyncStubTransport):
    """Return canned statuses in order; counts requests."""

    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self.requests = 0

    def _respond(self, request: httpx.Request) -> httpx.Response:
        status = self._statuses.pop(0) if self._statuses else 200
        self.requests += 1
        headers = {"Retry-After": "1"} if status == 429 else {}
        body: dict[str, Any] = (
            {"items": [], "total": 0, "page": 1, "size": 1}
            if status == 200
            else {"code": "stub", "message": "stub", "trace_id": "0" * 32}
        )
        return httpx.Response(status, json=body, headers=headers, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self, base_url="http://mock.invalid")


class JsonBodyTransport(_AsyncStubTransport):
    """Return a fixed JSON body with status 200."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def _respond(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self._body, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self, base_url="http://mock.invalid")


class RaisingTransport(_AsyncStubTransport):
    """Raise the given exception on every request."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def _respond(self, request: httpx.Request) -> httpx.Response:
        raise self._error

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self, base_url="http://mock.invalid")


__all__ = ["JsonBodyTransport", "RaisingTransport", "SequenceTransport", "StubTransport"]
