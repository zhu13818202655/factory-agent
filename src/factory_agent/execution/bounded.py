import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TypeVar

from sageql_core import RunControl, RunStopped

from factory_agent.domain.errors import InvalidRequestError
from factory_agent.ports.sandbox import InteractionSandboxPort

ResultT = TypeVar("ResultT")


async def run_sandbox_call(
    sandbox: InteractionSandboxPort, callback: Callable[[], ResultT], control: RunControl
) -> ResultT:
    """Run trusted computation within the shared cancellation and time budget.

    The worker must finish before its interaction connection can be destroyed.
    Interrupting DuckDB releases an in-flight statement when a turn is stopped.
    """
    control.check()
    task = asyncio.create_task(asyncio.to_thread(callback))
    deadline = time.monotonic() + control.limits.call_timeout
    try:
        while not task.done():
            control.check()
            if time.monotonic() >= deadline:
                control.stop("deadline_exceeded")
                raise RunStopped("deadline_exceeded")
            await asyncio.wait({task}, timeout=0.05)
        control.check()
        with suppress(Exception):
            return task.result()
        raise InvalidRequestError("bounded sandbox computation failed")
    finally:
        if not task.done():
            sandbox.interrupt()
            with suppress(Exception):
                await asyncio.shield(task)
