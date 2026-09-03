"""One-shot operator CLI entry points (external cron friendly).

Periodic maintenance in this repository is expressed as one-shot console
scripts driven by an external cron rather than in-process timers (same model
as usage-admin's ``usage-admin-retention``). The scope-review CLI runs the
read-only deviation review once and prints a redacted report.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Sequence

from factory_agent.application.scope_review import ScopeReviewService
from factory_agent.config import get_settings
from factory_agent.persistence.engine import create_session_engine
from factory_agent.persistence.scope_violation import SqlScopeViolationStore


def scope_review_main(argv: Sequence[str] | None = None) -> None:
    """Run the scope-deviation review once and print the redacted report."""
    parser = argparse.ArgumentParser(description="Run the role-consistency deviation review")
    parser.add_argument("--window-days", type=int, default=7, help="review window in days")
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.postgres_url is None:
        raise SystemExit("FACTORY_AGENT_POSTGRES_URL is required for scope review")

    engine = create_session_engine(str(settings.postgres_url))
    service = ScopeReviewService(
        SqlScopeViolationStore(engine),
        window_days=args.window_days,
    )

    async def _run() -> str:
        report = await service.run_once()
        return service.render_text(report)

    print(asyncio.run(_run()))


__all__ = ["scope_review_main"]
