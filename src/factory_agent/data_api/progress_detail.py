"""Rebuild reviewed progress grains from validated default-detail contributions.

The customer's sum/count columns carry aggregate contributions, rather than one
physical item per row. They are added without guessing distinct-package or
distinct-worktype keys. Completion percentages are recomputed from their totals.
The optional pure-sheet package statistics have no confirmed derivation rule and
are excluded from the supported grains.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class ProgressDerivationError(ValueError):
    """The validated detail cannot produce a reviewed grain safely."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"progress detail derivation failed: {reason}")


PROGRESS_ADDITIVE_COLUMNS: tuple[str, ...] = (
    "bs",
    "fhsl",
    "sssl",
    "zpsl",
    "wgs",
    "wts",
)

PROGRESS_REVIEWED_GRAINS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"dh", "jhdh", "chuanghao", "huohao", "bbreed", "dept", "deptname"}),
        frozenset({"dh", "baohao", "userid", "color", "chima"}),
        frozenset({"userid", "uid", "uname", "dept", "deptname", "worktype", "wtname", "wsort"}),
    }
)

_DIMENSION_NAMES: Mapping[str, str] = {
    "huohao": "bbreed",
    "worktype": "wtname",
    "uid": "uname",
    "dept": "deptname",
}


def derive_progress_rows(
    rows: tuple[Mapping[str, object], ...],
) -> tuple[dict[str, str], ...]:
    """Attach the validated calendar date encoded by each detail row's sheet ID.

    Neither the input rows nor their dimensions are changed. A missing or invalid
    sheet date rejects the complete set; silently dropping rows would undercount.
    """
    enriched: list[dict[str, str]] = []
    for row in rows:
        normalized = {key: _scalar(value) for key, value in row.items()}
        head = normalized.get("dh", "")[:8]
        if len(head) != 8 or not head.isascii() or not head.isdigit():
            raise ProgressDerivationError("invalid_sheet_date")
        try:
            sheet_date = date(int(head[:4]), int(head[4:6]), int(head[6:8]))
        except ValueError as error:
            raise ProgressDerivationError("invalid_sheet_date") from error
        normalized["dh_date"] = sheet_date.isoformat()
        enriched.append(normalized)
    return tuple(enriched)


def aggregate_progress_rows(
    rows: tuple[Mapping[str, object], ...],
    groupby_fields: Sequence[str],
) -> tuple[dict[str, str], ...]:
    """Project detail onto a reviewed grain after scope, date and query filtering.

    The six documented additive columns are summed as supplied. In particular,
    repeated material/worktype identities do not justify discarding a customer's
    contribution. ``wcl`` is total received / total issued, rounded to two decimal
    places, with zero for a zero denominator. ``bgs`` and ``bts`` are not inferred.

    Code/name pairs share one dimension. Conflicting names within a code group
    are rejected because the customer contract does not define a name selector.
    """
    if isinstance(groupby_fields, str):
        raise ProgressDerivationError("invalid_group_fields")
    columns = tuple(dict.fromkeys(groupby_fields))
    if not columns:
        return derive_progress_rows(rows)
    if frozenset(columns) not in PROGRESS_REVIEWED_GRAINS:
        raise ProgressDerivationError("unsupported_grain")

    name_fields = {
        name for code, name in _DIMENSION_NAMES.items() if code in columns and name in columns
    }
    key_fields = tuple(sorted(field for field in columns if field not in name_fields))
    grouped: dict[tuple[str, ...], tuple[dict[str, str], dict[str, Decimal]]] = {}
    for row in derive_progress_rows(rows):
        if any(column not in row for column in columns):
            raise ProgressDerivationError("missing_dimension")
        key = tuple(row[column] for column in key_fields)
        current = grouped.get(key)
        if current is None:
            dimensions = {column: row[column] for column in columns}
            totals = {column: Decimal(0) for column in PROGRESS_ADDITIVE_COLUMNS}
            grouped[key] = dimensions, totals
        else:
            dimensions, totals = current
            if any(row[name] != dimensions[name] for name in name_fields):
                raise ProgressDerivationError("inconsistent_dimension_name")
        for column in PROGRESS_ADDITIVE_COLUMNS:
            totals[column] += _contribution(row, column)

    output: list[dict[str, str]] = []
    for key in sorted(grouped):
        dimensions, totals = grouped[key]
        issued = totals["fhsl"]
        ratio = totals["sssl"] / issued * Decimal(100) if issued else Decimal(0)
        output.append(
            {
                **dimensions,
                **{column: str(totals[column]) for column in PROGRESS_ADDITIVE_COLUMNS},
                "wcl": str(ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            }
        )
    return tuple(output)


def _scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (str, int, float, Decimal)):
        return str(value)
    raise ProgressDerivationError("invalid_scalar")


def _contribution(row: Mapping[str, str], column: str) -> Decimal:
    value = row.get(column, "")
    if not value:
        raise ProgressDerivationError("missing_contribution")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ProgressDerivationError("invalid_contribution") from error
    if not amount.is_finite() or amount < 0:
        raise ProgressDerivationError("invalid_contribution")
    return amount
