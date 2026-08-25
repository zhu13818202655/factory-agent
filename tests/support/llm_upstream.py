"""pytest-managed temporary OpenAI-compatible upstream processes.

AGENTS.md requires routing tests to start real upstreams rather than in-process
fakes, so this module runs a stdlib HTTP server in a subprocess bound to an
ephemeral loopback port. It never reaches the public network and is torn down by
the fixture that started it.

Each instance is launched with a fixed default scenario, which is what lets a
test stand up an always-failing upstream next to a healthy one and prove that
fallback really crosses a process boundary. A single request can still override
the scenario with the ``X-Test-Scenario`` header.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from socket import AF_INET, SOCK_STREAM, socket

_SERVER_SOURCE = r"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
DEFAULT_SCENARIO = sys.argv[2]
LABEL = sys.argv[3]

CONTENT = '{"capability_id": "FR-001", "confidence": 0.95, "slots": {}}'


def completion(content, model):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, b'{"status":"ok"}')
            return
        self._send(404, b"{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        scenario = self.headers.get("X-Test-Scenario") or DEFAULT_SCENARIO
        model = payload.get("model", "unknown")

        if scenario == "timeout":
            time.sleep(30)
            return
        if scenario == "rate_limited":
            self._send(429, b'{"error":{"message":"rate limited"}}')
            return
        if scenario == "server_error":
            self._send(500, b'{"error":{"message":"upstream exploded"}}')
            return
        if scenario == "unauthorized":
            self._send(401, b'{"error":{"message":"invalid api key"}}')
            return
        if scenario == "malformed_json":
            self._send(200, b'{"choices": [')
            return
        if scenario == "missing_choices":
            self._send(200, json.dumps({"model": model, "usage": {}}).encode())
            return

        body = completion(CONTENT, LABEL)
        self._send(200, json.dumps(body).encode())

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
"""


@dataclass(frozen=True, slots=True)
class TemporaryUpstream:
    """A running upstream bound to a loopback port."""

    base_url: str
    label: str

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"


def _free_port() -> int:
    with closing(socket(AF_INET, SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_ready(port: int, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"temporary upstream exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
                f"http://127.0.0.1:{port}/healthz", timeout=0.5
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    raise RuntimeError("temporary upstream did not become ready")


@contextmanager
def temporary_upstream(
    scenario: str = "ok", *, label: str = "primary", startup_timeout: float = 10.0
) -> Generator[TemporaryUpstream]:
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and inline source
        [sys.executable, "-c", _SERVER_SOURCE, str(port), scenario, label],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_ready(port, process, startup_timeout)
        yield TemporaryUpstream(base_url=f"http://127.0.0.1:{port}/v1", label=label)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


__all__ = ["TemporaryUpstream", "temporary_upstream"]
