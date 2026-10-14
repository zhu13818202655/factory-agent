from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol


class RunBudget(Protocol):
    def check(self) -> None: ...

    def consume(
        self,
        resource: Literal[
            "mes_requests", "mes_pages", "model_rounds", "model_tokens", "semantic_repairs"
        ],
        amount: int = 1,
    ) -> None: ...


_ACTIVE_BUDGET: ContextVar[RunBudget | None] = ContextVar("analysis_run_budget", default=None)


@contextmanager
def bind_run_budget(budget: RunBudget) -> Generator[None]:
    token = _ACTIVE_BUDGET.set(budget)
    try:
        yield
    finally:
        _ACTIVE_BUDGET.reset(token)


def consume_upstream_attempt(*, business_page: bool = True) -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.check()
        budget.consume("mes_requests")
        if business_page:
            budget.consume("mes_pages")


def consume_model_attempt() -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.check()
        budget.consume("model_rounds")


def consume_model_tokens(tokens: int) -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.consume("model_tokens", tokens)


def consume_semantic_repair() -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.consume("semantic_repairs")
