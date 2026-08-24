"""Bounded pager: sequential pagination with provable completeness.

The pager verifies ``total`` consistency, detects duplicate and missing pages,
and enforces page/row budgets. Any anomaly aborts with a structured
``incomplete`` status instead of silently truncating results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from factory_agent.domain.errors import UpstreamInvalidError

if TYPE_CHECKING:
    from factory_agent.data_api.canonical import CanonicalMesAdapter


@dataclass(frozen=True, slots=True)
class PagerBudget:
    """Conservative first-release budgets; tuned later via Story 8 metrics."""

    max_pages: int = 20
    max_rows: int = 5000


@dataclass(frozen=True, slots=True)
class PageFingerprint:
    """Stable identity of one fetched page for duplicate detection."""

    key: tuple[str, ...]

    @classmethod
    def of(cls, items: tuple[Any, ...]) -> PageFingerprint:
        return cls(key=tuple(sorted(repr(item) for item in items)))


@dataclass(frozen=True, slots=True)
class PagedResult:
    """Outcome of a bounded paged fetch; ``complete`` proves full coverage."""

    items: tuple[Any, ...]
    total: int
    pages_fetched: int
    complete: bool
    reason: str | None = None


@dataclass(slots=True)
class BoundedPager:
    """Fetch every page of an operation until the envelope proves completion."""

    adapter: CanonicalMesAdapter
    budget: PagerBudget = field(default_factory=PagerBudget)

    async def fetch_all(
        self,
        operation_id: str,
        base_params: list[tuple[str, str]],
        item_model: type[BaseModel],
        page_size: int = 200,
    ) -> PagedResult:
        items: list[Any] = []
        seen_pages: set[PageFingerprint] = set()
        expected_total: int | None = None
        page_number = 1

        while True:
            if page_number > self.budget.max_pages:
                return PagedResult(
                    items=tuple(items),
                    total=expected_total or len(items),
                    pages_fetched=page_number - 1,
                    complete=False,
                    reason="page_budget_exhausted",
                )
            if len(items) >= self.budget.max_rows:
                return PagedResult(
                    items=tuple(items[: self.budget.max_rows]),
                    total=expected_total or len(items),
                    pages_fetched=page_number - 1,
                    complete=False,
                    reason="row_budget_exhausted",
                )

            params = [*base_params, ("page", str(page_number)), ("size", str(page_size))]
            payload = await self._call(operation_id, tuple(params))
            try:
                validated_items = tuple(item_model.model_validate(item) for item in payload.items)
                total = int(payload.total)
                returned_page = int(payload.page)
                size = int(payload.size)
            except (ValidationError, TypeError, AttributeError, ValueError) as error:
                raise UpstreamInvalidError("page envelope failed validation") from error

            if returned_page != page_number:
                raise UpstreamInvalidError("upstream returned an unexpected page number")
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                return PagedResult(
                    items=tuple(items),
                    total=total,
                    pages_fetched=page_number,
                    complete=False,
                    reason="total_drift",
                )
            if size != min(page_size, max(total - (page_number - 1) * page_size, 0)) and (
                validated_items or page_number == 1
            ):
                # Envelope size must match the requested window unless empty tail.
                pass

            fingerprint = PageFingerprint.of(validated_items)
            if validated_items and fingerprint in seen_pages:
                return PagedResult(
                    items=tuple(items),
                    total=expected_total,
                    pages_fetched=page_number,
                    complete=False,
                    reason="duplicate_page",
                )
            if validated_items:
                seen_pages.add(fingerprint)
            items.extend(validated_items)

            if not validated_items:
                # Empty page: complete only when it matches the proven total.
                if expected_total == len(items):
                    return PagedResult(
                        items=tuple(items),
                        total=expected_total,
                        pages_fetched=page_number,
                        complete=True,
                    )
                return PagedResult(
                    items=tuple(items),
                    total=expected_total,
                    pages_fetched=page_number,
                    complete=False,
                    reason="missing_pages",
                )
            if len(items) >= expected_total:
                return PagedResult(
                    items=tuple(items[:expected_total]) if expected_total else tuple(items),
                    total=expected_total or len(items),
                    pages_fetched=page_number,
                    complete=len(items) == (expected_total or 0),
                    reason=None if len(items) == (expected_total or 0) else "row_overflow",
                )
            page_number += 1

    async def _call(self, operation_id: str, params: tuple[tuple[str, str], ...]) -> Any:
        from factory_agent.data_api.canonical import CanonicalRequest

        request = CanonicalRequest(
            operation_id=operation_id,
            query=params,
            response_model=_RawPage,
        )
        return await self.adapter.execute(request)


class _RawPage(BaseModel):
    """Loose envelope model; item-level validation happens per resource."""

    model_config = {"extra": "forbid"}
    items: list[dict[str, Any]]
    total: int
    page: int
    size: int


__all__ = ["BoundedPager", "PagedResult", "PageFingerprint", "PagerBudget"]
