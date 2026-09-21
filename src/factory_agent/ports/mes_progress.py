"""Per-run MES fetch progress, published out of the pager.

The pager sits three layers below the session pipeline
(``kernel → ScopedExecutor → HongzhaoMesAdapter → BoundedPager``), and its one
interesting observable — "page N of a long walk has landed, M rows so far" — is
precisely what a user staring at a slow answer wants to see. Threading a
callback through four signatures would push a presentation concern into three
package boundaries; a context-local sink keeps it where it belongs and costs the
layers in between nothing.

The channel lives in ``ports`` because it crosses a one-way package boundary:
``data_api`` publishes, ``application`` subscribes, and the architecture forbids
``application`` from importing ``data_api``. Neither side owns the other, so the
shared contract sits between them.

Only counts cross this boundary. No operation id, no step id, no parameter
value, no row content: operation and step names are internal identifiers and
must never reach a user-visible transcript.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MesFetchProgress:
    """One page of a paged MES fetch, as observed by the pager."""

    page: int
    rows: int
    total: int | None = None


MesFetchProgressSink = Callable[[MesFetchProgress], None]

_sink: ContextVar[MesFetchProgressSink | None] = ContextVar("mes_fetch_progress", default=None)


@contextmanager
def report_mes_fetch_progress(sink: MesFetchProgressSink) -> Generator[None]:
    """Publish every page fetched inside this block to ``sink``.

    Context-local rather than instance state: the adapter and its pager are
    shared across concurrent interactions, so an instance attribute would leak
    one tenant's progress into another's transcript.
    """
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def publish_mes_fetch_progress(progress: MesFetchProgress) -> None:
    """Report one page; a no-op when nobody is listening.

    Silent by construction: the pager must keep working in contexts that never
    registered a sink (exports, benchmarks, tests).
    """
    sink = _sink.get()
    if sink is not None:
        sink(progress)


__all__ = [
    "MesFetchProgress",
    "MesFetchProgressSink",
    "publish_mes_fetch_progress",
    "report_mes_fetch_progress",
]
