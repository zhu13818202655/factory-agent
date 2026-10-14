import threading
import time

import pytest
from sageql_core import RunControl, RunStopped

from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.bounded import run_sandbox_call
from factory_agent.execution.sandbox_runtime import InteractionSandbox


class InterruptibleSandbox(InteractionSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.interrupted = threading.Event()
        self.finished = threading.Event()

    def interrupt(self) -> None:
        self.interrupted.set()

    def calculation(self) -> int:
        self.started.set()
        self.interrupted.wait(timeout=2)
        self.finished.set()
        return 1


@pytest.mark.asyncio
async def test_cancellation_interrupts_and_joins_worker_before_returning() -> None:
    sandbox = InterruptibleSandbox()
    control = RunControl(time.monotonic() + 5, cancelled=sandbox.started.is_set)
    with pytest.raises(RunStopped) as rejected:
        await run_sandbox_call(sandbox, sandbox.calculation, control)
    assert rejected.value.code == "cancelled"
    assert sandbox.interrupted.is_set()
    assert sandbox.finished.is_set()
    sandbox.close()


@pytest.mark.asyncio
async def test_worker_errors_are_classified_without_sensitive_callback_text() -> None:
    sandbox = InteractionSandbox()

    def invalid_calculation() -> int:
        raise ValueError("synthetic-sensitive-row-canary")

    try:
        with pytest.raises(InvalidRequestError) as rejected:
            await run_sandbox_call(sandbox, invalid_calculation, RunControl(time.monotonic() + 5))
        assert rejected.value.message == "bounded sandbox computation failed"
        assert rejected.value.__cause__ is None
        assert rejected.value.__context__ is None
    finally:
        sandbox.close()
