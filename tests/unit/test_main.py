import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import uvicorn

from factory_agent import __main__ as entrypoint
from factory_agent.config import FactoryAgentSettings


def record_startup(monkeypatch: pytest.MonkeyPatch, platform: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def run(app: str, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    def settings() -> FactoryAgentSettings:
        return FactoryAgentSettings(environment="local", host="127.0.0.1", port=8080)

    monkeypatch.setattr(entrypoint.sys, "platform", platform)
    monkeypatch.setattr(entrypoint, "get_settings", settings)
    monkeypatch.setattr(entrypoint.uvicorn, "run", run)
    return calls


def test_windows_startup_uses_psycopg_compatible_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_startup(monkeypatch, "win32")
    entrypoint.main([])
    assert calls == [
        {
            "app": "factory_agent.api.server:create_app",
            "factory": True,
            "host": "127.0.0.1",
            "port": 8080,
            "loop": "factory_agent.__main__:create_selector_event_loop",
        }
    ]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_use_uvicorn_auto_loop(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    calls = record_startup(monkeypatch, platform)
    entrypoint.main([])
    assert calls[0]["loop"] == "auto"
    assert calls[0]["port"] == 8080


def test_host_and_port_arguments_override_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_startup(monkeypatch, "win32")
    entrypoint.main(["--host", "127.0.0.1", "--port", "8090"])
    assert calls[0]["port"] == 8090


@pytest.mark.parametrize("port", ["0", "65536"])
def test_invalid_ports_never_start_uvicorn(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    calls = record_startup(monkeypatch, "win32")
    with pytest.raises(SystemExit):
        entrypoint.main(["--port", port])
    assert calls == []


def test_uvicorn_resolves_factory_and_runs_selector_loop() -> None:
    config = uvicorn.Config(
        "factory_agent.api.server:create_app",
        factory=True,
        loop="factory_agent.__main__:create_selector_event_loop",
        log_config=None,
    )
    factory: Callable[[], asyncio.AbstractEventLoop] | None = config.get_loop_factory()
    assert factory is entrypoint.create_selector_event_loop

    async def running_loop() -> asyncio.AbstractEventLoop:
        await asyncio.sleep(0)
        return asyncio.get_running_loop()

    with asyncio.Runner(loop_factory=factory) as runner:
        loop = runner.run(running_loop())
        assert isinstance(loop, asyncio.SelectorEventLoop)
