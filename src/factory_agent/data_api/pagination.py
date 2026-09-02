"""Bounded pager: sequential pagination with provable completeness.

The pager walks ``page``/``size`` until the accumulated row count reaches
``result.total``, verifying total consistency, detecting duplicate and missing
pages, and enforcing page/row budgets. Any anomaly aborts with a structured
``incomplete`` status instead of silently truncating results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ValidationError

from factory_agent.domain.errors import UpstreamInvalidError

if TYPE_CHECKING:
    from factory_agent.data_api.hongzhao import HongzhaoMesAdapter


@dataclass(frozen=True, slots=True)
class PagerBudget:
    """Conservative first-release budgets.

    The customer declares no pagination upper bound; ``page_size`` default and
    ``max_pages`` are configuration placeholders to be re-verified during
    joint debugging with the customer.
    """

    max_pages: int = 20
    max_rows: int = 5000
    page_size: int = 200


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
    footer: dict[str, str] | None = None


@dataclass(slots=True)
class BoundedPager:
    """Fetch every page of an operation until the envelope proves completion."""

    adapter: HongzhaoMesAdapter
    budget: PagerBudget = field(default_factory=PagerBudget)

    async def fetch_all(
        self,
        operation_id: str,
        base_params: dict[str, Any],
        item_model: type[BaseModel],
    ) -> PagedResult:
        from factory_agent.data_api.hongzhao import MesRequest

        items: list[Any] = []
        seen_pages: set[PageFingerprint] = set()
        expected_total: int | None = None
        footer: dict[str, str] | None = None
        page_number = 1

        while True:
            if page_number > self.budget.max_pages:
                return PagedResult(
                    items=tuple(items),
                    total=expected_total or len(items),
                    pages_fetched=page_number - 1,
                    complete=False,
                    reason="page_budget_exhausted",
                    footer=footer,
                )
            if len(items) >= self.budget.max_rows:
                return PagedResult(
                    items=tuple(items[: self.budget.max_rows]),
                    total=expected_total or len(items),
                    pages_fetched=page_number - 1,
                    complete=False,
                    reason="row_budget_exhausted",
                    footer=footer,
                )

            params = {
                **base_params,
                "page": page_number,
                "size": self.budget.page_size,
            }
            payload = await self.adapter.execute(MesRequest(operation_id, params))
            if payload.footer is not None:
                footer = payload.footer
            try:
                result = cast(dict[str, Any], payload.result)
                raw_items = cast(list[Any], result["list"])
                total = int(result["total"])
            except (KeyError, TypeError, ValueError) as error:
                raise UpstreamInvalidError("list envelope failed validation") from error
            try:
                validated_items = tuple(
                    item_model.model_validate(cast(dict[str, Any], item)) for item in raw_items
                )
            except ValidationError as error:
                raise UpstreamInvalidError("row failed schema validation") from error

            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                return PagedResult(
                    items=tuple(items),
                    total=total,
                    pages_fetched=page_number,
                    complete=False,
                    reason="total_drift",
                    footer=footer,
                )

            fingerprint = PageFingerprint.of(validated_items)
            if validated_items and fingerprint in seen_pages:
                return PagedResult(
                    items=tuple(items),
                    total=expected_total,
                    pages_fetched=page_number,
                    complete=False,
                    reason="duplicate_page",
                    footer=footer,
                )
            if validated_items:
                seen_pages.add(fingerprint)
            items.extend(validated_items)

            if len(items) >= expected_total:
                trimmed = tuple(items[:expected_total])
                complete = len(trimmed) == expected_total
                return PagedResult(
                    items=trimmed,
                    total=expected_total,
                    pages_fetched=page_number,
                    complete=complete,
                    reason=None if complete else "row_overflow",
                    footer=footer,
                )
            if not validated_items:
                # Empty page before reaching total: missing pages detected.
                return PagedResult(
                    items=tuple(items),
                    total=expected_total,
                    pages_fetched=page_number,
                    complete=False,
                    reason="missing_pages",
                    footer=footer,
                )
            page_number += 1


__all__ = ["BoundedPager", "PagedResult", "PageFingerprint", "PagerBudget"]
