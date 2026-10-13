from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from re import fullmatch
from typing import Literal

from factory_agent.ports.card import (
    CardAction,
    CardColumn,
    _row_cells,  # pyright: ignore[reportPrivateUsage]
)
from factory_agent.ports.contracts import UNAVAILABLE_VALUE

type OrderGroupKind = Literal["plan", "unlinked"]
type OrderStatus = Literal["all", "finished", "unfinished", "unavailable"]

_SUMMARY_HIDDEN = frozenset({"plan_code", "plan_missing", "order_count", "order_code"})
_GROUP_LIMIT = 50
_PAGE_LIMIT = 100_000
_PAGE_SIZE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class OrderViewSelection:
    view_id: str
    group_kind: OrderGroupKind
    plan_code: str | None = None
    page: int = 1
    page_size: int = 10
    status: OrderStatus = "all"
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.view_id)
        if self.group_kind == "plan":
            if not isinstance(self.plan_code, str) or not self.plan_code.strip():
                raise ValueError("A linked order group requires a plan code")
        elif self.group_kind == "unlinked":
            if self.plan_code is not None:
                raise ValueError("An unlinked order group cannot carry a plan code")
        else:
            raise ValueError("Unsupported order group kind")
        _validate_integer(self.page, "page", _PAGE_LIMIT)
        _validate_integer(self.page_size, "page size", _PAGE_SIZE_LIMIT)
        if self.status not in {"all", "finished", "unfinished", "unavailable"}:
            raise ValueError("Unsupported order status")
        _validate_snapshot(self.snapshot_id)


@dataclass(frozen=True, slots=True)
class OrderDetailData:
    view_id: str
    title: str
    group_by: str
    columns: tuple[CardColumn, ...]
    rows: tuple[Mapping[str, object], ...]
    status_column: str | None = None
    hidden_columns: tuple[str, ...] = ()
    actions: tuple[CardAction, ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.view_id)
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate order detail columns")
        if self.status_column is not None and self.status_column not in names:
            raise ValueError("Order detail status column is not declared")


def build_order_groups(
    capability_id: str,
    summary_rows: Sequence[Mapping[str, object]],
    summary_columns: Sequence[CardColumn],
    details: Sequence[OrderDetailData],
    selection: OrderViewSelection | None = None,
    max_groups: int = _GROUP_LIMIT,
    preview_rows: int = 10,
    group_page: int = 1,
) -> dict[str, object]:
    """Project reviewed order summaries and details without recomputing metrics."""
    _validate_integer(max_groups, "group limit", _GROUP_LIMIT)
    _validate_integer(preview_rows, "preview row limit", _PAGE_SIZE_LIMIT)
    _validate_integer(group_page, "group page", _PAGE_LIMIT)
    if selection is not None and group_page != 1:
        raise ValueError("Order detail selection cannot paginate the group list")
    detail_by_id = {detail.view_id: detail for detail in details}
    if len(detail_by_id) != len(details):
        raise ValueError("Duplicate order detail views")
    summaries: dict[str | None, Mapping[str, object]] = {}
    for row in summary_rows:
        if "plan_code" not in row:
            raise ValueError("Order summary is missing its group key")
        key = _plan_key(row["plan_code"])
        if key in summaries:
            raise ValueError("Duplicate order summary group")
        summaries[key] = row
    if selection is not None:
        selected_key = selection.plan_code if selection.group_kind == "plan" else None
        if selected_key not in summaries:
            raise ValueError("Order group is unavailable in this result")
        selected_detail = detail_by_id.get(selection.view_id)
        if selected_detail is None:
            raise ValueError("Order detail view is unavailable in this result")
        if selection.status != "all" and selected_detail.status_column is None:
            raise ValueError("Order detail view does not support status filtering")
        kept_summaries = [(selected_key, summaries[selected_key])]
        kept_details = (selected_detail,)
    else:
        offset = (group_page - 1) * max_groups
        kept_summaries = list(summaries.items())[offset : offset + max_groups]
        kept_details = tuple(details)
    detail_rows: dict[str, dict[str | None, list[Mapping[str, object]]]] = {}
    for detail in kept_details:
        indexed_rows: dict[str | None, list[Mapping[str, object]]] = {}
        for row in detail.rows:
            if detail.group_by not in row:
                raise ValueError("Order detail is missing its group key")
            indexed_rows.setdefault(_plan_key(row[detail.group_by]), []).append(row)
        detail_rows[detail.view_id] = indexed_rows
    columns = tuple(column for column in summary_columns if column.name not in _SUMMARY_HIDDEN)
    groups: list[dict[str, object]] = []
    for plan_code, row in kept_summaries:
        kind: OrderGroupKind = "unlinked" if plan_code is None else "plan"
        raw_name = row.get("order_code")
        name = (
            raw_name
            if isinstance(raw_name, str) and raw_name.strip() and raw_name != UNAVAILABLE_VALUE
            else (plan_code or "未关联计划号")
        )
        groups.append(
            {
                "id": _group_id(plan_code),
                "name": name,
                "kind": kind,
                "plan_code": plan_code,
                "summary": _summary_payload(columns, row),
                "state_counts": _state_counts(row, summary_columns),
                "default_expanded": len(summaries) == 1,
                "views": [
                    _detail_payload(
                        capability_id,
                        detail,
                        plan_code,
                        selection,
                        preview_rows,
                        detail_rows[detail.view_id].get(plan_code, []),
                    )
                    for detail in kept_details
                ],
            }
        )
    return {
        "version": 1,
        "groups_total": len(summaries),
        "page": group_page,
        "page_size": max_groups,
        "has_more": (
            (group_page - 1) * max_groups + len(kept_summaries) < len(summaries)
            if selection is None
            else False
        ),
        "groups": groups,
    }


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) is None:
        raise ValueError("Invalid order view identifier")


def _validate_integer(value: object, name: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"Invalid order {name}")


def _validate_snapshot(value: object) -> None:
    if value is not None and (
        not isinstance(value, str) or fullmatch(r"[0-9a-fA-F]{64}", value) is None
    ):
        raise ValueError("Invalid order snapshot identifier")


def _plan_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid order group key")
    return value if value.strip() else None


def _group_id(plan_code: str | None) -> str:
    return "unlinked" if plan_code is None else f"plan:{sha256(plan_code.encode()).hexdigest()}"


def _column_payload(column: CardColumn) -> dict[str, object]:
    return {
        "name": column.name,
        "title": column.title or column.name,
        "type": column.column_type,
        "unit": column.unit,
    }


def _summary_payload(
    columns: Sequence[CardColumn], row: Mapping[str, object]
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for column, value in zip(
        columns, _row_cells({column.name: column for column in columns}, row), strict=True
    ):
        item = _column_payload(column)
        if value is None:
            item["unavailable"] = True
        else:
            item["value"] = value
        items.append(item)
    return items


def _state_counts(row: Mapping[str, object], columns: Sequence[CardColumn]) -> dict[str, object]:
    by_name = {column.name: column for column in columns}
    counts: dict[str, object] = {}
    for state in ("finished", "unfinished", "unavailable"):
        name = f"{state}_sheet_count"
        column = by_name.get(name)
        if column is not None:
            counts[state] = _row_cells({name: column}, row)[0]
    return counts


def _status(value: object) -> OrderStatus:
    if value == "已完工":
        return "finished"
    if value == "未完工":
        return "unfinished"
    return "unavailable"


def _detail_payload(
    capability_id: str,
    detail: OrderDetailData,
    plan_code: str | None,
    selection: OrderViewSelection | None,
    preview_rows: int,
    group_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    status: OrderStatus = selection.status if selection is not None else "all"
    filtered_rows = (
        group_rows
        if status == "all"
        else tuple(
            row for row in group_rows if _status(row.get(detail.status_column or "")) == status
        )
    )
    hidden = {detail.group_by, "plan_missing", "order_code", *detail.hidden_columns}
    available_columns: list[CardColumn] = []
    unavailable_columns: list[dict[str, object]] = []
    for column in detail.columns:
        if column.name in hidden:
            continue
        if group_rows and all(
            row.get(column.name) is None or row.get(column.name) == UNAVAILABLE_VALUE
            for row in group_rows
        ):
            unavailable_columns.append(_column_payload(column))
        else:
            available_columns.append(column)
    page = selection.page if selection is not None else 1
    page_size = selection.page_size if selection is not None else preview_rows
    start = (page - 1) * page_size
    page_rows = filtered_rows[start : start + page_size]
    by_name = {column.name: column for column in available_columns}
    request_view: dict[str, object] = {
        "view_id": detail.view_id,
        "group_kind": "unlinked" if plan_code is None else "plan",
        "plan_code": plan_code,
        "page": page,
        "page_size": page_size,
        "status": status,
    }
    return {
        "view_id": detail.view_id,
        "title": detail.title,
        "table": {
            "columns": list(by_name),
            "column_titles": {name: column.title or name for name, column in by_name.items()},
            "column_metadata": [_column_payload(column) for column in available_columns],
            "rows": [_row_cells(by_name, row) for row in page_rows],
            "total_rows": len(group_rows),
            "filtered_total_rows": len(filtered_rows),
            "status": status,
            "page": page,
            "page_size": page_size,
            "has_more": start + len(page_rows) < len(filtered_rows),
            "unavailable_columns": unavailable_columns,
            "actions": [action.payload() for action in detail.actions],
        },
        "request": {"capability_id": capability_id, "order_view": request_view},
    }
