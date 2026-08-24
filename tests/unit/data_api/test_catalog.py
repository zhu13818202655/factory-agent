from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from factory_agent.data_api.catalog import DEFAULT_CATALOG_PATH, load_catalog


def test_default_catalog_loads_and_covers_canonical_operations() -> None:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    expected = {
        "A1_getTenantMembership",
        "A2_listOrganizationAssignments",
        "A3_listEffectiveScopes",
        "C1_listPieceworkRecords",
        "C2_listEmployees",
        "C3_listDepartments",
        "C4_listOrders",
        "C5_listStyles",
        "C6_listOperations",
        "C7_listProductionPlans",
        "C8_listPayrollSettlements",
    }
    assert catalog.operation_ids == expected


def test_unknown_operation_is_rejected_at_runtime() -> None:
    from factory_agent.domain.errors import UnsupportedOperationError

    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    with pytest.raises(UnsupportedOperationError):
        catalog.get("X9_notRegistered")


def test_every_resource_operation_declares_scope_sourced_authorization_params() -> None:
    """Authorization IDs may only come from DataScope, never filter or clock."""
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    scope_required = ("tenant_id", "authorized_employee_ids", "authorized_dept_ids")
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        if operation.kind != "resource":
            continue
        for parameter in scope_required:
            assert operation.parameter_sources.get(parameter) == "scope", (
                f"{operation.operation_id}.{parameter} must be scope-sourced"
            )


def test_no_operation_accepts_user_text_as_scope_parameter() -> None:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        for parameter, source in operation.parameter_sources.items():
            if parameter.startswith("authorized_") or parameter == "tenant_id":
                assert source == "scope"


def test_malformed_catalog_fails_closed(tmp_path: Path) -> None:
    bad_document = {
        "version": 1,
        "operations": [
            {
                "operation_id": "X1_bad",
                "path": "/v1/bad",
                "kind": "resource",
                "parameter_sources": {"tenant_id": "user_text"},
                "pagination": "items_total_page_size",
                "timeout_seconds": 10.0,
                "min_role": "employee",
            }
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad_document), encoding="utf-8")
    from factory_agent.domain.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        load_catalog(path)


def test_duplicate_operation_ids_fail_startup(tmp_path: Path) -> None:
    from typing import Any

    from factory_agent.domain.errors import InvalidRequestError

    entry: dict[str, Any] = {
        "operation_id": "X1_dup",
        "path": "/v1/dup",
        "kind": "identity",
        "parameter_sources": {},
        "pagination": "none",
        "timeout_seconds": 10.0,
        "min_role": "employee",
    }
    document: dict[str, Any] = {"version": 1, "operations": [entry, dict(entry)]}
    path = tmp_path / "dup.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_catalog(path)


def test_missing_catalog_file_fails_startup(tmp_path: Path) -> None:
    from factory_agent.domain.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        load_catalog(tmp_path / "missing.yaml")
