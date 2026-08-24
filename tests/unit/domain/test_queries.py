from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory_agent.domain import (
    DeptId,
    EmployeeId,
    MesError,
    MesErrorCode,
    PaginationRequest,
    ResourceQuery,
    TenantId,
    TimeRange,
)


def test_time_range_requires_timezone_aware_bounds() -> None:
    from factory_agent.domain.queries import TimeRangeError

    with pytest.raises(TimeRangeError):
        TimeRange(datetime(2026, 8, 1), datetime(2026, 8, 2, tzinfo=UTC))


def test_time_range_rejects_empty_or_inverted_interval() -> None:
    with pytest.raises(ValueError):
        TimeRange(datetime(2026, 8, 2, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        TimeRange(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))


def test_time_range_accepts_half_open_utc_interval() -> None:
    time_range = TimeRange(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    assert time_range.start < time_range.end


def test_pagination_request_bounds() -> None:
    assert PaginationRequest(page=1, size=50).size == 50
    with pytest.raises(ValueError):
        PaginationRequest(page=0)
    with pytest.raises(ValueError):
        PaginationRequest(size=0)
    with pytest.raises(ValueError):
        PaginationRequest(size=201)


def test_resource_query_is_frozen_and_scope_typed() -> None:
    query = ResourceQuery(
        tenant_id=TenantId("tenant-a"),
        employee_ids=frozenset({EmployeeId("employee-a1")}),
        dept_ids=frozenset({DeptId("group-a1")}),
        time_range=TimeRange(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
    )
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        query.tenant_id = TenantId("tenant-b")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("error_class", "expected_code"),
    [
        ("InvalidRequestError", MesErrorCode.INVALID_REQUEST),
        ("UnauthenticatedError", MesErrorCode.UNAUTHENTICATED),
        ("ForbiddenError", MesErrorCode.FORBIDDEN),
        ("NotFoundError", MesErrorCode.NOT_FOUND),
        ("RateLimitedError", MesErrorCode.RATE_LIMITED),
        ("MesTimeoutError", MesErrorCode.TIMEOUT),
        ("UpstreamUnavailableError", MesErrorCode.UPSTREAM_UNAVAILABLE),
        ("UpstreamInvalidError", MesErrorCode.UPSTREAM_INVALID),
        ("UnsupportedOperationError", MesErrorCode.UNSUPPORTED_OPERATION),
        ("InternalError", MesErrorCode.INTERNAL_ERROR),
    ],
)
def test_exception_hierarchy_aligns_with_canonical_codes(
    error_class: str, expected_code: MesErrorCode
) -> None:
    import factory_agent.domain.errors as errors_module

    error = getattr(errors_module, error_class)()
    assert isinstance(error, MesError)
    assert error.code is expected_code


def test_rate_limited_error_carries_retry_after() -> None:
    from factory_agent.domain.errors import RateLimitedError

    error = RateLimitedError(retry_after_seconds=7)
    assert error.retry_after_seconds == 7


def test_upstream_invalid_error_does_not_embed_payload_fragments() -> None:
    """Sanitized category text only; raw payload fragments must never attach."""
    from factory_agent.domain.errors import UpstreamInvalidError

    error = UpstreamInvalidError("field type mismatch at items[0]")
    assert "payload" not in error.message or "raw" not in error.message
    assert "{" not in str(error)
