"""Unified MES error taxonomy aligned with the Canonical error codes.

Exception messages must never contain sensitive values (raw payloads,
credentials, employee/dept ID lists, or business quantities). Callers pass
sanitized, category-level text only.
"""

from __future__ import annotations

from enum import StrEnum


class MesErrorCode(StrEnum):
    """Canonical error codes; no new vocabulary is introduced here."""

    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_INVALID = "upstream_invalid"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    INTERNAL_ERROR = "internal_error"


class MesError(Exception):
    """Base class for every structured MES execution failure."""

    def __init__(self, code: MesErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


class InvalidRequestError(MesError):
    def __init__(self, message: str = "request parameters are invalid") -> None:
        super().__init__(MesErrorCode.INVALID_REQUEST, message)


class UnauthenticatedError(MesError):
    def __init__(self, message: str = "service credential was rejected") -> None:
        super().__init__(MesErrorCode.UNAUTHENTICATED, message)


class ForbiddenError(MesError):
    def __init__(self, message: str = "active tenant or scope denied the request") -> None:
        super().__init__(MesErrorCode.FORBIDDEN, message)


class NotFoundError(MesError):
    def __init__(self, message: str = "requested resource does not exist") -> None:
        super().__init__(MesErrorCode.NOT_FOUND, message)


class RateLimitedError(MesError):
    def __init__(
        self, message: str = "upstream rate limit reached", retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(MesErrorCode.RATE_LIMITED, message)
        self.retry_after_seconds = retry_after_seconds


class MesTimeoutError(MesError):
    def __init__(self, message: str = "upstream call timed out") -> None:
        super().__init__(MesErrorCode.TIMEOUT, message)


class UpstreamUnavailableError(MesError):
    def __init__(self, message: str = "upstream service is unavailable") -> None:
        super().__init__(MesErrorCode.UPSTREAM_UNAVAILABLE, message)


class UpstreamInvalidError(MesError):
    """Upstream payload failed schema validation; raw payload never attached."""

    def __init__(self, message: str = "upstream payload failed validation") -> None:
        super().__init__(MesErrorCode.UPSTREAM_INVALID, message)


class UnsupportedOperationError(MesError):
    def __init__(self, message: str = "operation is not in the approved catalog") -> None:
        super().__init__(MesErrorCode.UNSUPPORTED_OPERATION, message)


class InternalError(MesError):
    def __init__(self, message: str = "internal execution failure") -> None:
        super().__init__(MesErrorCode.INTERNAL_ERROR, message)


__all__ = [
    "ForbiddenError",
    "InternalError",
    "InvalidRequestError",
    "MesError",
    "MesErrorCode",
    "NotFoundError",
    "RateLimitedError",
    "MesTimeoutError",
    "UnauthenticatedError",
    "UnsupportedOperationError",
    "UpstreamInvalidError",
    "UpstreamUnavailableError",
]
