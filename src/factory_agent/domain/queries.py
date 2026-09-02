"""Typed query boundaries for MES business data access.

Every value here is a frozen value object. Raw URLs, headers, and customer
payload shapes are forbidden at this layer by design; only ``data_api/``
translates these values into HTTP details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from factory_agent.domain.identifiers import TenantId
from factory_agent.domain.identity import DeptId, EmployeeId


class TimeRangeError(ValueError):
    """Raised when a time interval violates the Canonical half-open contract."""


@dataclass(frozen=True, slots=True)
class NarrowedFilters:
    """The single downstream exit for scope parameters.

    ``tenant_id``, ``employee_ids``, and ``dept_ids`` are already intersected
    with the active ``DataScope``; downstream executors must never accept these
    values from any other source.

    ``order_codes`` / ``style_codes`` / ``plan_codes`` are business filters
    that only narrow the requested range and are enforced by MES-side
    row-level filtering plus the "returned range smaller than requested"
    judgement. They can never broaden the active scope.
    """

    tenant_id: TenantId
    employee_ids: frozenset[EmployeeId] | None
    dept_ids: frozenset[DeptId] | None
    order_codes: frozenset[str] | None = None
    style_codes: frozenset[str] | None = None
    plan_codes: frozenset[str] | None = None
    #: User-requested department intersection. ``None`` means the user did not
    #: restrict to a department; the visible range is then the MES-filtered
    #: range. When set it only narrows within the scope.
    requested_dept_ids: frozenset[DeptId] | None = None


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Half-open UTC interval ``[from, to)`` used by every Canonical query."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise TimeRangeError("time range bounds must be timezone-aware")
        if self.start >= self.end:
            raise TimeRangeError("time range start must be before end")


@dataclass(frozen=True, slots=True)
class PaginationRequest:
    """Bounded page request; the upper size limit follows the Canonical cap."""

    page: int = 1
    size: int = 50
    max_size: int = 200

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be >= 1")
        if self.size < 1:
            raise ValueError("size must be >= 1")
        if self.size > self.max_size:
            raise ValueError("size exceeds the approved maximum page size")


@dataclass(frozen=True, slots=True)
class ResourceQuery:
    """Authorized resource-list query for one tenant.

    ``tenant_id``, ``employee_ids``, and ``dept_ids`` always originate from the
    active ``DataScope``; callers must never accept them from user or model
    output. Optional batch filters narrow within the scope only.
    """

    tenant_id: TenantId
    employee_ids: frozenset[EmployeeId] | None
    dept_ids: frozenset[DeptId] | None
    time_range: TimeRange
    pagination: PaginationRequest = PaginationRequest()
    order_ids: frozenset[str] | None = None
    style_ids: frozenset[str] | None = None
    operation_ids: frozenset[str] | None = None
    plan_ids: frozenset[str] | None = None
    settlement_ids: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class MembershipQuery:
    """Canonical A1 query: resolve the unique membership for a credential."""

    subject_id: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class EffectiveScopeQuery:
    """Canonical A3 query: effective scopes of one membership."""

    tenant_id: TenantId
    membership_id: str
    as_of: datetime


class ProvenComplete:
    """Marker type proving a result page series was fully fetched."""


PROVEN_COMPLETE = ProvenComplete()


@dataclass(frozen=True, slots=True)
class ResultPage:
    """One validated page of domain items plus its envelope metadata."""

    items: tuple[object, ...]
    total: int
    page: int
    size: int


@dataclass(frozen=True, slots=True)
class CompleteResult:
    """A fully fetched, proven-complete item sequence with envelope totals."""

    items: tuple[object, ...]
    total: int
    pages_fetched: int
    completeness: ProvenComplete = PROVEN_COMPLETE


__all__ = [
    "PROVEN_COMPLETE",
    "CompleteResult",
    "EffectiveScopeQuery",
    "MembershipQuery",
    "NarrowedFilters",
    "PaginationRequest",
    "ProvenComplete",
    "ResourceQuery",
    "ResultPage",
    "TimeRange",
    "TimeRangeError",
]
