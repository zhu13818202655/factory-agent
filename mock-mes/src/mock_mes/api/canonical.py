from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated, Iterable, cast

from fastapi import APIRouter, Header, Query, Request

from mock_mes.seed import Dataset, Record

router = APIRouter(prefix="/v1", tags=["canonical"])
Page = Annotated[int, Query(ge=1, alias="page")]
Size = Annotated[int, Query(ge=1, le=200)]
RequiredIds = Annotated[str, Query(min_length=1)]
OptionalIds = Annotated[str | None, Query()]
TenantHeader = Annotated[str, Header(alias="X-Tenant-Id")]
BearerHeader = Annotated[str, Header(alias="Authorization")]


class CanonicalError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


def dataset_from(request: Request) -> Dataset:
    return cast(Dataset, request.app.state.dataset)


def subject_from(authorization: str, dataset: Dataset) -> str:
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise CanonicalError(401, "unauthenticated", "missing service identity")
    subject = authorization.removeprefix(prefix)
    if subject not in dataset.memberships_by_subject:
        raise CanonicalError(401, "unauthenticated", "unknown service identity")
    return subject


def split_ids(value: str | None) -> set[str]:
    if value is None:
        return set()
    values = {item.strip() for item in value.split(",") if item.strip()}
    if not values:
        raise CanonicalError(400, "invalid_request", "ID batch must not be empty")
    return values


def active_memberships(dataset: Dataset, subject: str, as_of: datetime) -> list[Record]:
    instant = as_of.astimezone(timezone.utc)
    return [
        item
        for item in dataset.memberships_by_subject[subject]
        if parse_time(item["valid_from"]) <= instant
        and (item["valid_to"] is None or instant < parse_time(item["valid_to"]))
    ]


def scope_sets(
    dataset: Dataset,
    subject: str,
    tenant_id: str,
    authorized_employee_ids: str,
    authorized_dept_ids: str,
) -> tuple[set[str], set[str]]:
    memberships = dataset.memberships_by_subject[subject]
    if tenant_id not in {str(item["tenant_id"]) for item in memberships}:
        raise CanonicalError(403, "forbidden", "active tenant is not authorized")
    scopes = dataset.scopes_by_subject.get(subject, {}).get(tenant_id, [])
    allowed_employees = {
        str(employee_id)
        for scope in scopes
        for employee_id in cast(list[object], scope["employee_ids"])
    }
    allowed_depts = {
        str(dept_id) for scope in scopes for dept_id in cast(list[object], scope["dept_ids"])
    }
    requested_employees = split_ids(authorized_employee_ids)
    requested_depts = split_ids(authorized_dept_ids)
    if not requested_employees <= allowed_employees or not requested_depts <= allowed_depts:
        raise CanonicalError(403, "forbidden", "requested scope exceeds effective scope")
    return requested_employees, requested_depts


def parse_time(value: object) -> datetime:
    text = str(value)
    if len(text) == 10:
        text = f"{text}T00:00:00+00:00"
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def overlaps(
    record: Record, start: datetime, end: datetime, start_field: str, end_field: str | None = None
) -> bool:
    record_start = parse_time(record[start_field])
    if end_field is None:
        return start <= record_start < end
    raw_end = record.get(end_field)
    record_end = (
        parse_time(raw_end) if raw_end is not None else datetime.max.replace(tzinfo=timezone.utc)
    )
    return record_start < end and start < record_end


def in_scope(record: Record, employee_ids: set[str], dept_ids: set[str]) -> bool:
    employee_id = record.get("employee_id")
    if employee_id is not None and str(employee_id) not in employee_ids:
        return False
    dept_id = record.get("dept_id")
    if dept_id is not None and str(dept_id) not in dept_ids:
        return False
    record_depts = record.get("dept_ids") or record.get("responsible_dept_ids")
    if isinstance(record_depts, list):
        values = cast(list[object], record_depts)
        if not {str(item) for item in values} & dept_ids:
            return False
    return True


def authorized_records(
    dataset: Dataset,
    subject: str,
    resource: str,
    tenant_id: str,
    authorized_employee_ids: str,
    authorized_dept_ids: str,
) -> list[Record]:
    employee_scope, dept_scope = scope_sets(
        dataset,
        subject,
        tenant_id,
        authorized_employee_ids,
        authorized_dept_ids,
    )
    return [
        item
        for item in dataset.resources[resource]
        if item["tenant_id"] == tenant_id and in_scope(item, employee_scope, dept_scope)
    ]


def by_ids(records: Iterable[Record], field: str, values: str | None) -> list[Record]:
    selected = split_ids(values) if values is not None else None
    return [item for item in records if selected is None or str(item.get(field)) in selected]


def by_related_ids(records: Iterable[Record], field: str, values: str | None) -> list[Record]:
    selected = split_ids(values) if values is not None else None
    if selected is None:
        return list(records)
    filtered: list[Record] = []
    for item in records:
        raw_value = item.get(field)
        related = cast(list[object], raw_value) if isinstance(raw_value, list) else [raw_value]
        if {str(value) for value in related} & selected:
            filtered.append(item)
    return filtered


def page(
    records: list[Record], page_number: int, page_size: int, request: Request
) -> dict[str, object]:
    start = (page_number - 1) * page_size
    response: dict[str, object] = {
        "items": deepcopy(records[start : start + page_size]),
        "total": len(records),
        "page": page_number,
        "size": page_size,
    }
    fault = request.headers.get("X-Mock-Fault")
    items = cast(list[Record], response["items"])
    if fault == "duplicate_page" and items:
        items.append(deepcopy(items[0]))
    elif fault == "missing_page":
        response["items"] = []
    elif fault == "wrong_total":
        response["total"] = len(records) + 7
    elif fault == "null" and items:
        field = (
            "status"
            if "status" in items[0]
            else next(key for key in items[0] if not key.endswith("_id"))
        )
        items[0][field] = None
    elif fault == "field_drift" and items:
        items[0]["synthetic_drift_field"] = "unexpected"
    return response


@router.get("/identity/memberships")
async def list_memberships(
    request: Request,
    authorization: BearerHeader,
    as_of: datetime,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    return page(active_memberships(dataset, subject, as_of), page_number, size, request)


@router.get("/effective-scopes")
async def list_effective_scopes(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    as_of: datetime,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    memberships = active_memberships(dataset, subject, as_of)
    if tenant_id not in {str(item["tenant_id"]) for item in memberships}:
        raise CanonicalError(403, "forbidden", "active tenant is not authorized")
    return page(
        dataset.scopes_by_subject.get(subject, {}).get(tenant_id, []),
        page_number,
        size,
        request,
    )


@router.get("/organization-assignments")
async def list_assignments(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    employee_ids: OptionalIds = None,
    dept_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset,
        subject,
        "organization_assignments",
        tenant_id,
        authorized_employee_ids,
        authorized_dept_ids,
    )
    records = by_ids(records, "employee_id", employee_ids)
    records = by_ids(records, "dept_id", dept_ids)
    records = [item for item in records if overlaps(item, start, end, "valid_from", "valid_to")]
    return page(records, page_number, size, request)


@router.get("/piecework-records")
async def list_piecework(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    employee_ids: OptionalIds = None,
    dept_ids: OptionalIds = None,
    order_ids: OptionalIds = None,
    style_ids: OptionalIds = None,
    operation_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset,
        subject,
        "piecework_records",
        tenant_id,
        authorized_employee_ids,
        authorized_dept_ids,
    )
    for field, values in (
        ("employee_id", employee_ids),
        ("dept_id", dept_ids),
        ("order_id", order_ids),
        ("style_id", style_ids),
        ("operation_id", operation_ids),
    ):
        records = by_ids(records, field, values)
    records = [item for item in records if overlaps(item, start, end, "work_at")]
    records.sort(key=lambda item: (str(item["work_at"]), str(item["record_id"])))
    return page(records, page_number, size, request)


@router.get("/employees")
async def list_employees(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    employee_ids: OptionalIds = None,
    dept_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset, subject, "employees", tenant_id, authorized_employee_ids, authorized_dept_ids
    )
    records = by_ids(records, "employee_id", employee_ids)
    records = by_related_ids(records, "dept_ids", dept_ids)
    records = [
        item for item in records if overlaps(item, start, end, "effective_from", "effective_to")
    ]
    records.sort(key=lambda item: str(item["employee_id"]))
    return page(records, page_number, size, request)


@router.get("/departments")
async def list_departments(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    dept_ids: OptionalIds = None,
    parent_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset, subject, "departments", tenant_id, authorized_employee_ids, authorized_dept_ids
    )
    records = by_ids(records, "dept_id", dept_ids)
    records = by_ids(records, "parent_id", parent_ids)
    records = [
        item for item in records if overlaps(item, start, end, "effective_from", "effective_to")
    ]
    records.sort(key=lambda item: str(item["dept_id"]))
    return page(records, page_number, size, request)


@router.get("/orders")
async def list_orders(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    order_ids: OptionalIds = None,
    style_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset, subject, "orders", tenant_id, authorized_employee_ids, authorized_dept_ids
    )
    records = by_ids(records, "order_id", order_ids)
    records = by_ids(records, "style_id", style_ids)
    records = [item for item in records if overlaps(item, start, end, "ordered_at")]
    records.sort(key=lambda item: (str(item["ordered_at"]), str(item["order_id"])))
    return page(records, page_number, size, request)


@router.get("/styles")
async def list_styles(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    style_ids: OptionalIds = None,
    keyword: str | None = Query(default=None, max_length=100),
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset, subject, "styles", tenant_id, authorized_employee_ids, authorized_dept_ids
    )
    records = by_ids(records, "style_id", style_ids)
    records = [
        item for item in records if overlaps(item, start, end, "effective_from", "effective_to")
    ]
    if keyword is not None:
        lowered = keyword.casefold()
        records = [
            item
            for item in records
            if lowered in f"{item['style_number']} {item['name']}".casefold()
        ]
    records.sort(key=lambda item: str(item["style_id"]))
    return page(records, page_number, size, request)


@router.get("/operations")
async def list_operations(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    operation_ids: OptionalIds = None,
    order_ids: OptionalIds = None,
    style_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset, subject, "operations", tenant_id, authorized_employee_ids, authorized_dept_ids
    )
    for field, values in (
        ("operation_id", operation_ids),
        ("order_id", order_ids),
        ("style_id", style_ids),
    ):
        records = by_ids(records, field, values)
    records = [
        item for item in records if overlaps(item, start, end, "effective_from", "effective_to")
    ]
    records.sort(
        key=lambda item: (
            str(item["style_id"]),
            int(cast(int, item["sequence"])),
            str(item["operation_id"]),
        )
    )
    return page(records, page_number, size, request)


@router.get("/production-plans")
async def list_plans(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    plan_ids: OptionalIds = None,
    dept_ids: OptionalIds = None,
    order_ids: OptionalIds = None,
    style_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset,
        subject,
        "production_plans",
        tenant_id,
        authorized_employee_ids,
        authorized_dept_ids,
    )
    for field, values in (
        ("plan_id", plan_ids),
        ("dept_id", dept_ids),
        ("order_id", order_ids),
        ("style_id", style_ids),
    ):
        records = by_ids(records, field, values)
    records = [item for item in records if overlaps(item, start, end, "starts_at", "ends_at")]
    records.sort(key=lambda item: (str(item["starts_at"]), str(item["plan_id"])))
    return page(records, page_number, size, request)


@router.get("/payroll-settlements")
async def list_payroll(
    request: Request,
    authorization: BearerHeader,
    tenant_id: TenantHeader,
    authorized_employee_ids: RequiredIds,
    authorized_dept_ids: RequiredIds,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
    settlement_ids: OptionalIds = None,
    employee_ids: OptionalIds = None,
    dept_ids: OptionalIds = None,
    page_number: Page = 1,
    size: Size = 50,
) -> dict[str, object]:
    dataset = dataset_from(request)
    subject = subject_from(authorization, dataset)
    records = authorized_records(
        dataset,
        subject,
        "payroll_settlements",
        tenant_id,
        authorized_employee_ids,
        authorized_dept_ids,
    )
    for field, values in (
        ("settlement_id", settlement_ids),
        ("employee_id", employee_ids),
        ("dept_id", dept_ids),
    ):
        records = by_ids(records, field, values)
    records = [item for item in records if overlaps(item, start, end, "period_start", "period_end")]
    records.sort(key=lambda item: (str(item["period_start"]), str(item["settlement_id"])))
    return page(records, page_number, size, request)
