"""Runtime guard isolating platform operations from factory business paths."""

from __future__ import annotations

from factory_agent.domain import DataScope, PlatformScope


class PlatformScopeViolationError(RuntimeError):
    """Raised when a platform principal enters a factory business path."""


class PlatformBoundaryGuard:
    """Blocks PlatformScope holders from MES/interaction/export execution paths."""

    def assert_factory_context(self, scope: DataScope | None) -> None:
        if isinstance(scope, PlatformScope):
            raise PlatformScopeViolationError(
                "platform scope must never enter factory business paths"
            )
        if scope is None:
            raise PlatformScopeViolationError(
                "factory business paths require an authorized DataScope"
            )

    def assert_platform_context(self, scope: PlatformScope | None) -> None:
        if not isinstance(scope, PlatformScope):
            raise PlatformScopeViolationError(
                "platform operations require a separately authenticated PlatformScope"
            )
