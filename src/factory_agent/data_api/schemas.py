"""Pydantic models validating external Canonical MES responses.

These models exist only at the external boundary: validated rows are converted
to plain dicts before entering the DuckDB sandbox. Raw customer payload shapes
never leak past ``data_api/``.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StableId(str):
    """Canonical stable ID pattern enforced on response fields."""

    __slots__ = ()

    @classmethod
    def validate(cls, value: str) -> StableId:
        if not value or len(value) > 128:
            raise ValueError("stable ID length out of range")
        first = value[0]
        if not (first.isalnum() and first.isascii()):
            raise ValueError("stable ID must start with an alphanumeric character")
        if not all(ch.isalnum() and ch.isascii() or ch in "._:-" for ch in value):
            raise ValueError("stable ID contains unsupported characters")
        return cls(value)


class _CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TenantMembershipResponse(_CanonicalModel):
    membership_id: str
    user_id: str
    tenant_id: str
    employee_id: str
    role: str
    dept_ids: tuple[str, ...]
    valid_from: str
    valid_to: str | None


class EffectiveScopeResponse(_CanonicalModel):
    scope_id: str
    membership_id: str
    tenant_id: str
    employee_ids: tuple[str, ...]
    dept_ids: tuple[str, ...]
    evaluated_at: str


class OrganizationAssignmentResponse(_CanonicalModel):
    assignment_id: str
    tenant_id: str
    employee_id: str
    dept_id: str
    valid_from: str
    valid_to: str | None


class PieceworkRecordResponse(_CanonicalModel):
    record_id: str
    tenant_id: str
    employee_id: str
    dept_id: str
    order_id: str
    style_id: str
    operation_id: str
    plan_id: str | None
    work_at: str
    completed_quantity: str
    qualified_quantity: str
    defective_quantity: str
    unit_rate: str
    amount: str
    status: str

    @field_validator("completed_quantity", "qualified_quantity", "defective_quantity")
    @classmethod
    def _quantity(cls, value: str) -> str:
        return _validate_decimal_pattern(value)

    @field_validator("unit_rate", "amount")
    @classmethod
    def _money(cls, value: str) -> str:
        return _validate_decimal_pattern(value)


class EmployeeResponse(_CanonicalModel):
    employee_id: str
    tenant_id: str
    employee_number: str
    display_name: str
    dept_ids: tuple[str, ...]
    status: str
    effective_from: str
    effective_to: str | None


class DepartmentResponse(_CanonicalModel):
    dept_id: str
    tenant_id: str
    parent_id: str | None
    name: str
    organization_type: str
    effective_from: str
    effective_to: str | None


class OrderResponse(_CanonicalModel):
    order_id: str
    tenant_id: str
    order_number: str
    style_id: str
    responsible_dept_ids: tuple[str, ...]
    ordered_at: str
    due_at: str
    ordered_quantity: str
    completed_quantity: str
    status: str


class StyleResponse(_CanonicalModel):
    style_id: str
    tenant_id: str
    style_number: str
    name: str
    effective_from: str
    effective_to: str | None
    status: str


class OperationResponse(_CanonicalModel):
    operation_id: str
    tenant_id: str
    style_id: str
    order_id: str | None
    name: str
    sequence: int
    unit: str
    unit_rate: str
    effective_from: str
    effective_to: str | None


class ProductionPlanResponse(_CanonicalModel):
    plan_id: str
    tenant_id: str
    dept_id: str
    order_id: str
    style_id: str
    starts_at: str
    ends_at: str
    planned_quantity: str
    completed_quantity: str
    status: str


class PayrollSettlementResponse(_CanonicalModel):
    settlement_id: str
    tenant_id: str
    employee_id: str
    dept_id: str
    period_start: str
    period_end: str
    piece_count: str
    gross_amount: str
    status: str
    published_at: str | None


def _validate_decimal_pattern(value: str) -> str:
    import re

    if not re.fullmatch(r"-?[0-9]+(\.[0-9]{1,4})?", value):
        raise ValueError("decimal string does not match the Canonical pattern")
    return value


class PageEnvelopeResponse(_CanonicalModel):
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1, le=200)


_RESPONSE_MODEL_BY_ITEM: dict[str, type[BaseModel]] = {
    "organization_assignments": OrganizationAssignmentResponse,
    "effective_scopes": EffectiveScopeResponse,
    "piecework_records": PieceworkRecordResponse,
    "employees": EmployeeResponse,
    "departments": DepartmentResponse,
    "orders": OrderResponse,
    "styles": StyleResponse,
    "operations": OperationResponse,
    "production_plans": ProductionPlanResponse,
    "payroll_settlements": PayrollSettlementResponse,
}


def item_model_for(resource: str) -> type[BaseModel]:
    try:
        return _RESPONSE_MODEL_BY_ITEM[resource]
    except KeyError as error:
        raise ValueError(f"no response model registered for resource: {resource}") from error


def row_to_plain_dict(model: BaseModel) -> dict[str, Any]:
    """Convert a validated row into a plain dict for sandbox registration."""
    return {key: _plain(value) for key, value in model.model_dump().items()}


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        items: list[Any] = []
        for item in cast("tuple[Any, ...]", value):
            items.append(_plain(item))
        return items
    return value


__all__ = [
    "DepartmentResponse",
    "EffectiveScopeResponse",
    "EmployeeResponse",
    "OperationResponse",
    "OrderResponse",
    "OrganizationAssignmentResponse",
    "PageEnvelopeResponse",
    "PayrollSettlementResponse",
    "PieceworkRecordResponse",
    "ProductionPlanResponse",
    "StyleResponse",
    "TenantMembershipResponse",
    "item_model_for",
    "row_to_plain_dict",
]
