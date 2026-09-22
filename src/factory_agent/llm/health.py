"""Out-of-band LLM endpoint health probing (ADR-0006).

litellm already falls over from an unreachable deployment to the next ``order``,
but only after a request has paid ``1 + num_retries`` failed attempts against
the dead endpoint, and its cooldown memory is short. With sparse traffic that
cost lands on the caller every time. Probing endpoints in the background moves
it off the user's wait: by the time a request arrives, the routing table has
already been reordered. The reviewed registry stays the baseline, so an endpoint
that answers again is restored without editing configuration.

A probe is deliberately not a model call. A completion would enter
``llm_call_fact`` and bill a health check as business usage; a ``GET /models``
against the same endpoint answers "is this server serving" while staying outside
the metering boundary. That request is also the one that already surfaced the
failure mode we hit, an unreachable self-hosted vLLM host.
"""

import asyncio
import inspect
from collections.abc import Callable
from typing import Protocol

import httpx

from factory_agent.llm.registry import ModelRegistry, ResolvedDeployment
from factory_agent.observability.logging_adapter import get_logger

_health_logger = get_logger("factory_agent.llm.health")


class EndpointProbe(Protocol):
    """Answers whether one deployment's endpoint is currently serving."""

    async def is_healthy(self, deployment: ResolvedDeployment) -> bool: ...


class HttpModelsProbe:
    """``GET {api_base}/models`` against an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._owned: httpx.AsyncClient | None = None

    async def is_healthy(self, deployment: ResolvedDeployment) -> bool:
        url = f"{deployment.api_base.rstrip('/')}/models"
        try:
            response = await self._client_or_create().get(
                url,
                headers={"Authorization": f"Bearer {deployment.api_key}"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError:
            # Refused connection, DNS failure and timeout are one verdict here.
            return False
        return 200 <= response.status_code < 300

    async def aclose(self) -> None:
        client, self._owned = self._owned, None
        if client is not None:
            await client.aclose()

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._owned


class EndpointHealthMonitor:
    """Turns per-endpoint probe verdicts into the registry to route with.

    Demotion needs ``failures_to_demote`` consecutive failures, so one blip does
    not reshuffle a working routing table; restoration needs a single success,
    because the cost of a wrong demotion is only "ran on the backup", whereas
    the cost of a late restoration is the slow path the probe exists to remove.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        probe: EndpointProbe,
        *,
        apply: Callable[[ModelRegistry], None],
        failures_to_demote: int = 2,
    ) -> None:
        self._baseline = registry
        self._probe = probe
        self._apply = apply
        self._failures_to_demote = failures_to_demote
        self._consecutive_failures: dict[str, int] = {}
        self._demoted: set[str] = set()
        #: Endpoints whose current failure episode began with the probe itself
        #: raising. Tracked so "the health check is broken" is reported once per
        #: episode rather than once per probe.
        self._probe_errors: set[str] = set()

    @property
    def demoted_endpoints(self) -> frozenset[str]:
        return frozenset(self._demoted)

    @property
    def registry(self) -> ModelRegistry:
        """The routing table the current verdicts imply."""
        return self._baseline.with_demoted_endpoints(self._demoted)

    async def refresh(self) -> ModelRegistry:
        """Probe every endpoint once and publish the resulting routing table.

        A probe that raises is treated as a failed probe: a broken health check
        must degrade the answer, never the process.
        """
        targets = self._baseline.probe_targets()
        await asyncio.gather(*(self._probe_one(target) for target in targets))
        registry = self.registry
        self._apply(registry)
        return registry

    async def probe_forever(self, interval_seconds: float) -> None:
        """Re-probe on a wall-clock cadence until the lifespan cancels this task.

        Pacing uses the real clock on purpose, and a failing pass never ends the
        loop: a probe that stops probing would silently restore the slow path.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - the loop outlives any single failure
                _health_logger.exception("llm.health.refresh_failed")

    async def aclose(self) -> None:
        closer = getattr(self._probe, "aclose", None)
        if inspect.iscoroutinefunction(closer):
            await closer()

    async def _probe_one(self, deployment: ResolvedDeployment) -> None:
        probe_raised = False
        try:
            healthy = await self._probe.is_healthy(deployment)
        except Exception:  # noqa: BLE001 - an unusable probe is a failed probe
            # Not logged here. A persistently unreachable host used to print one
            # line per endpoint per interval forever while saying nothing new;
            # ``_record`` knows whether this verdict is a transition, so it — not
            # the probe — decides what earns a line.
            probe_raised = True
            healthy = False
        self._record(deployment.api_base, healthy, probe_error=probe_raised)

    def _record(self, endpoint: str, healthy: bool, *, probe_error: bool = False) -> None:
        if healthy:
            attempts = self._consecutive_failures.pop(endpoint, 0)
            self._probe_errors.discard(endpoint)
            if endpoint in self._demoted:
                self._demoted.discard(endpoint)
                _health_logger.info(
                    "llm.endpoint.restored endpoint={endpoint} failed_probes={failed}",
                    endpoint=endpoint,
                    failed=attempts,
                )
            return

        failures = self._consecutive_failures.get(endpoint, 0) + 1
        self._consecutive_failures[endpoint] = failures
        if probe_error and endpoint not in self._probe_errors:
            # First error of this episode. Distinct from a probe that answers
            # "unhealthy": here the health check itself is unusable (bad URL,
            # DNS, TLS), which an operator fixes differently.
            self._probe_errors.add(endpoint)
            _health_logger.warning(
                "llm.endpoint.probe_error endpoint={endpoint} failed_probes={failed}",
                endpoint=endpoint,
                failed=failures,
            )
        if endpoint in self._demoted:
            # The verdict has not changed, so neither has anything worth a line:
            # this repetition is exactly the noise the state tracking suppresses.
            return
        if failures >= self._failures_to_demote:
            self._demoted.add(endpoint)
            _health_logger.warning(
                "llm.endpoint.demoted endpoint={endpoint} failed_probes={failed}",
                endpoint=endpoint,
                failed=failures,
            )


__all__ = ["EndpointHealthMonitor", "EndpointProbe", "HttpModelsProbe"]
