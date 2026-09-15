"""Result-card payload builder for the SSE ``interaction.result`` event.

The card is a front-end renderable projection of one capability result: a KPI
area, an optional preview table (truncated, optionally grouped), a totals row,
and structured warnings. Every number on the card comes from the result table
itself (totals or a row value); nothing is fabricated here. A metric with no
data source is surfaced as an explicit ``unavailable`` state.

The payload shape mirrors ``tools/test_frontend/static/app.js``: ``metrics``,
``table`` (``rows`` as a 2-D array; grouped cards keep same-group rows
contiguous in ``groups[]`` order so the client can slice by ``row_count``),
``totals``, ``unavailable_columns`` and ``notes``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from factory_agent.ports.contracts import UNAVAILABLE_VALUE

#: Hard ceiling for preview rows / groups; recipes may pick anything below it.
MAX_PREVIEW_ROWS = 100
MAX_PREVIEW_GROUPS = 200

#: Columns allowed in the KPI area: only typed numeric columns, so a uid-like
#: string can never be presented as a figure.
_NUMERIC_COLUMN_TYPES = frozenset({"money", "quantity", "percent"})

_UNAVAILABLE_LABEL = "暂无数据源"


@dataclass(frozen=True, slots=True)
class CardColumn:
    """Display metadata for one card column (stable name + worker-facing label)."""

    name: str
    title: str | None = None
    column_type: str | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class CardAlertSpec:
    """Semantic highlight: rows where ``column == equals`` (styling is client-side)."""

    column: str
    equals: str


@dataclass(frozen=True, slots=True)
class CardTableSpec:
    """Reviewed card shape resolved from a recipe ``card:`` block.

    ``kind`` is ``kpi`` (KPI area only), ``table`` (preview rows) or ``ranking``
    (preview rows with a ranked column). ``preview_max_rows`` is the per-group
    cap on grouped cards; ``preview_max_groups`` bounds the number of group tabs.
    """

    kind: str
    metrics: tuple[str, ...] = ()
    totals: tuple[str, ...] = ()
    preview_max_rows: int | None = None
    preview_max_groups: int = 50
    group_by: str | None = None
    rank_column: str | None = None
    alert_marker: CardAlertSpec | None = None
    #: Reviewed, static 口径 statements for this capability. They lead the card
    #: notes so a reader always sees the measurement basis before runtime
    #: warnings; they carry no data values.
    notes: tuple[str, ...] = ()

    def has_table(self) -> bool:
        return self.kind in ("table", "ranking")


def build_card(
    spec: CardTableSpec,
    *,
    capability_id: str,
    title: str,
    columns: Sequence[CardColumn],
    rows: tuple[dict[str, object], ...],
    totals: Mapping[str, Decimal],
    incomplete: bool = False,
    incomplete_reason: str | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, object]:
    """Assemble the card payload; only numbers already in the table appear."""
    by_name = {column.name: column for column in columns}
    payload: dict[str, object] = {
        "kind": spec.kind,
        "title": title,
        "capability_id": capability_id,
        "metrics": _metrics_payload(spec.metrics, by_name, rows, totals),
        "totals": _totals_payload(spec.totals, by_name, totals),
    }
    if spec.has_table():
        payload["table"] = _table_payload(spec, by_name, rows)
    unavailable = _unavailable_titles(spec.metrics, by_name, rows, totals)
    if unavailable:
        payload["unavailable_columns"] = unavailable
    notes = [*spec.notes, *warnings]
    if incomplete and incomplete_reason:
        notes.append(f"本次结果不完整（{incomplete_reason}），以导出文件为准。")
    if notes:
        payload["notes"] = notes
    return payload


def _metrics_payload(
    names: Sequence[str],
    by_name: Mapping[str, CardColumn],
    rows: tuple[dict[str, object], ...],
    totals: Mapping[str, Decimal],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in names:
        column = by_name[name]
        item: dict[str, object] = {
            "label": column.title or name,
            "unit": column.unit or "",
        }
        value = _metric_value(name, by_name[name], rows, totals)
        if value is None:
            item["unavailable"] = True
        else:
            item["value"] = value
        items.append(item)
    return items


def _metric_value(
    name: str,
    column: CardColumn,
    rows: tuple[dict[str, object], ...],
    totals: Mapping[str, Decimal],
) -> str | None:
    """Resolve one KPI number: totals first, then the single result row."""
    total = totals.get(name)
    if total is not None:
        return _decimal_str(total)
    if len(rows) == 1:
        value = rows[0].get(name)
        if value is None or value == UNAVAILABLE_VALUE:
            return None
        return _cell_text(value, column.column_type)
    return None


def _totals_payload(
    names: Sequence[str],
    by_name: Mapping[str, CardColumn],
    totals: Mapping[str, Decimal],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in names:
        column = by_name[name]
        item: dict[str, object] = {
            "label": column.title or name,
            "unit": column.unit or "",
        }
        total = totals.get(name)
        if total is None:
            item["unavailable"] = True
        else:
            item["value"] = _decimal_str(total)
        items.append(item)
    return items


def _unavailable_titles(
    names: Sequence[str],
    by_name: Mapping[str, CardColumn],
    rows: tuple[dict[str, object], ...],
    totals: Mapping[str, Decimal],
) -> list[str]:
    titles: list[str] = []
    for name in names:
        if _metric_value(name, by_name[name], rows, totals) is None:
            column = by_name[name]
            titles.append(column.title or name)
    return titles


def _table_payload(
    spec: CardTableSpec,
    by_name: Mapping[str, CardColumn],
    rows: tuple[dict[str, object], ...],
) -> dict[str, object]:
    names = list(by_name)
    total_rows = len(rows)
    table: dict[str, object] = {
        "columns": names,
        "column_titles": {name: by_name[name].title or name for name in names},
        "total_rows": total_rows,
    }
    if spec.group_by is not None:
        kept, groups, distinct_total = _grouped_rows(spec, by_name, rows)
        table["rows"] = kept
        table["group_by"] = spec.group_by
        table["groups"] = groups
        table["groups_total"] = distinct_total
        table["preview_max_rows"] = spec.preview_max_rows
        dropped_groups = len(groups) < distinct_total
        table["truncated"] = any(entry["truncated"] for entry in groups) or dropped_groups
    else:
        limit = spec.preview_max_rows or MAX_PREVIEW_ROWS
        kept_rows = rows[:limit]
        table["rows"] = [_row_cells(by_name, row) for row in kept_rows]
        table["preview_max_rows"] = limit
        table["truncated"] = total_rows > len(kept_rows)
    if spec.alert_marker is not None:
        table["alert_marker"] = {
            "column": spec.alert_marker.column,
            "equals": spec.alert_marker.equals,
        }
    return table


def _grouped_rows(
    spec: CardTableSpec,
    by_name: Mapping[str, CardColumn],
    rows: tuple[dict[str, object], ...],
) -> tuple[list[list[object]], list[dict[str, object]], int]:
    """Slice contiguous same-group runs, capping rows per group and group count.

    Same-group rows are already contiguous and ordered by the recipe (server
    -side ranking); the client slices the flat ``rows`` array by each group's
    ``row_count`` without re-grouping. Returns the kept cell rows, the group
    entries and the total number of distinct groups (dropped groups included).
    """
    group_by = spec.group_by or ""
    distinct_total = len(dict.fromkeys(row.get(group_by) for row in rows))
    per_group = spec.preview_max_rows or MAX_PREVIEW_ROWS
    kept_cells: list[list[object]] = []
    groups: list[dict[str, object]] = []
    index = 0
    while index < len(rows) and len(groups) < spec.preview_max_groups:
        group_value = rows[index].get(group_by)
        end = index
        while end < len(rows) and rows[end].get(group_by) == group_value:
            end += 1
        run = rows[index:end]
        kept = run[:per_group]
        kept_cells.extend(_row_cells(by_name, row) for row in kept)
        groups.append(
            {
                "group": group_value,
                "row_count": len(kept),
                "total_rows": len(run),
                "truncated": len(run) > len(kept),
            }
        )
        index = end
    return kept_cells, groups, distinct_total


def _row_cells(by_name: Mapping[str, CardColumn], row: Mapping[str, object]) -> list[object]:
    cells: list[object] = []
    for name, column in by_name.items():
        value = row.get(name)
        if value is None or value == UNAVAILABLE_VALUE:
            cells.append(None)
        else:
            cells.append(_cell_text(value, column.column_type))
    return cells


def _cell_text(value: object, column_type: str | None) -> str:
    if isinstance(value, Decimal):
        return _decimal_str(value)
    if isinstance(value, str):
        # Only typed numeric strings are normalised ("001" is a department id,
        # never a number); plain text passes through untouched.
        if column_type in _NUMERIC_COLUMN_TYPES:
            return _string_cell(value)
        return value
    return str(value)


def _string_cell(value: str) -> str:
    try:
        return _decimal_str(Decimal(value))
    except InvalidOperation:
        return value


def _decimal_str(value: Decimal) -> str:
    return format(value.normalize(), "f")


__all__ = [
    "MAX_PREVIEW_GROUPS",
    "MAX_PREVIEW_ROWS",
    "CardAlertSpec",
    "CardColumn",
    "CardTableSpec",
    "build_card",
]
