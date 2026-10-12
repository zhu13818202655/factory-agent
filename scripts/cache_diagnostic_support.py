"""Shared diagnostic setup; importing it does not load application settings."""

import asyncio
import hashlib
import inspect
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from factory_agent.bootstrap import ApplicationContainer
    from factory_agent.data_api.owner_snapshots import OwnerSnapshotFetcher
    from factory_agent.data_api.result_cache import V2ResultCache
    from factory_agent.ports.session import CapabilityRunResult

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load_env(path: Path | None) -> None:
    if path is None or not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def build_runtime(env_file: Path | None, mode: str | None = None) -> "ApplicationContainer":
    sys.path.insert(0, str(ROOT / "src"))
    load_env(env_file)
    from loguru import logger

    logger.disable("factory_agent")
    from factory_agent.bootstrap import build_container
    from factory_agent.config import FactoryAgentSettings

    settings = FactoryAgentSettings()
    if mode is not None:
        settings = settings.model_copy(update={"result_cache_mode": mode})
    return build_container(settings)


def result_cache(container: "ApplicationContainer") -> "V2ResultCache":
    from factory_agent import bootstrap as boot

    cache = boot._build_result_cache(  # noqa: SLF001 - deployed policy and key versions
        container.settings, container.cache, container.clock
    )
    if cache is None:
        raise RuntimeError("result cache is disabled or Redis is not configured")
    return cache


def owner_fetchers(container: "ApplicationContainer") -> tuple["OwnerSnapshotFetcher", ...]:
    if container.owner_snapshots is None:
        raise RuntimeError("authorized owner producers are not configured")
    return container.owner_snapshots.fetchers


def tenant_label(tenant: object) -> str:
    return hashlib.sha256(str(tenant).encode()).hexdigest()[:12]


def question_window(month: str | None, dates: list[str] | None, zone: ZoneInfo) -> tuple[str, str]:
    if dates is not None:
        start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[1])
        if start > end:
            raise ValueError("invalid calendar interval")
        return start.isoformat(), end.isoformat()
    if month is None:
        end = datetime.now(zone).date().replace(day=1) - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    start = date.fromisoformat(f"{month}-01")
    following = (
        date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    )
    return start.isoformat(), (following - timedelta(days=1)).isoformat()


def canonical_interval(window: tuple[str, str], zone: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(date.fromisoformat(window[0]), time.min, zone),
        datetime.combine(date.fromisoformat(window[1]) + timedelta(days=1), time.min, zone),
    )


def equal_results(left: "CapabilityRunResult", right: "CapabilityRunResult") -> bool:
    return (
        left.column_names == right.column_names
        and left.rows == right.rows
        and left.totals == right.totals
        and left.card == right.card
        and left.warnings == right.warnings
        and left.incomplete == right.incomplete
        and left.incomplete_reason == right.incomplete_reason
    )


def error_category(error: Exception) -> str:
    code = getattr(error, "code", None)
    return str(getattr(code, "value", type(error).__name__))


async def close_runtime(container: "ApplicationContainer", *, close_exchange: bool = True) -> None:
    resources: list[Any] = [container.owner_snapshots, container.mes, container.model]
    if close_exchange:
        resources.append(container.credential_exchange)
    if container.cache is not None:
        resources.append(container.cache._store)  # noqa: SLF001 - diagnostic-owned runtime
    seen: set[int] = set()
    pending: list[Any] = []
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                pending.append(result)
    await asyncio.gather(*pending, return_exceptions=True)
