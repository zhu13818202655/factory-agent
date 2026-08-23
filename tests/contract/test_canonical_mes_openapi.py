from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPOSITORY_ROOT / "contracts" / "mes-canonical.openapi.yaml"
EXAMPLES_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "mes-canonical-v1.json"
EXPECTED_OPERATIONS = {
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


def load_openapi() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8")))


def load_examples() -> dict[str, dict[str, Any]]:
    return cast(dict[str, dict[str, Any]], json.loads(EXAMPLES_PATH.read_text(encoding="utf-8")))


def resolve(node: Any, document: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        mapping = cast(dict[str, Any], node)
        reference = mapping.get("$ref")
        if isinstance(reference, str):
            if not reference.startswith("#/"):
                raise AssertionError(f"external reference is not allowed: {reference}")
            target: Any = document
            for part in reference[2:].split("/"):
                target = target[part]
            return resolve(target, document)
        return {key: resolve(value, document) for key, value in mapping.items()}
    if isinstance(node, list):
        values = cast(list[Any], node)
        return [resolve(value, document) for value in values]
    return node


def operations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        operation["operationId"]: operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method == "get"
    }


def response_schema(operation: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    response = resolve(operation["responses"]["200"], document)
    return cast(dict[str, Any], response["content"]["application/json"]["schema"])


def test_contract_covers_exactly_a1_a3_and_c1_c8() -> None:
    document = load_openapi()

    assert document["openapi"] == "3.1.0"
    assert set(operations(document)) == EXPECTED_OPERATIONS


def test_sanitized_examples_validate_against_response_json_schemas() -> None:
    document = load_openapi()
    contract_operations = operations(document)

    for operation_id, example in load_examples().items():
        schema = resolve(response_schema(contract_operations[operation_id], document), document)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(  # pyright: ignore[reportUnknownMemberType]
            example["response"]
        )


def test_examples_supply_every_required_request_parameter() -> None:
    document = load_openapi()
    contract_operations = operations(document)

    for operation_id, example in load_examples().items():
        request = example["request"]
        for raw_parameter in contract_operations[operation_id]["parameters"]:
            parameter = resolve(raw_parameter, document)
            if not parameter.get("required", False):
                continue
            location = "headers" if parameter["in"] == "header" else "query"
            assert parameter["name"] in request[location], (operation_id, parameter["name"])


def test_every_operation_has_bounded_pagination_and_explicit_time_semantics() -> None:
    document = load_openapi()

    for operation_id, operation in operations(document).items():
        parameters = [resolve(parameter, document) for parameter in operation["parameters"]]
        names = {parameter["name"] for parameter in parameters}
        if operation_id == "A1_getTenantMembership":
            # Single-object identity resolution; no pagination by design.
            assert names == {"as_of"}
            continue
        assert {"page", "size"} <= names
        assert (
            {"as_of"} <= names
            if operation_id.startswith(("A1_", "A3_"))
            else {"from", "to"} <= names
        )


def test_business_lists_require_trusted_tenant_and_complete_scope_filters() -> None:
    document = load_openapi()

    for operation_id, operation in operations(document).items():
        if operation_id.startswith(("A1_", "A3_")):
            continue
        parameters = [resolve(parameter, document) for parameter in operation["parameters"]]
        required_names = {
            parameter["name"] for parameter in parameters if parameter.get("required", False)
        }
        assert {"X-Tenant-Id", "authorized_employee_ids", "authorized_dept_ids"} <= required_names


def test_a1_membership_is_unique_per_credential_pair_with_single_role() -> None:
    membership = load_examples()["A1_getTenantMembership"]["response"]

    assert membership["user_id"] == "user-a"
    assert membership["tenant_id"] == "tenant-a"
    assert membership["role"] in {"employee", "manager", "owner"}
    assert set(membership) == {
        "membership_id",
        "user_id",
        "tenant_id",
        "employee_id",
        "role",
        "dept_ids",
        "valid_from",
        "valid_to",
    }


def test_resource_schemas_expose_stable_relationship_keys() -> None:
    document = load_openapi()
    required_keys = {
        "PieceworkRecord": {
            "record_id",
            "tenant_id",
            "employee_id",
            "dept_id",
            "order_id",
            "style_id",
            "operation_id",
            "work_at",
        },
        "Employee": {"employee_id", "tenant_id", "dept_ids", "effective_from"},
        "Department": {"dept_id", "tenant_id", "parent_id", "effective_from"},
        "Order": {
            "order_id",
            "tenant_id",
            "style_id",
            "responsible_dept_ids",
            "ordered_at",
            "due_at",
        },
        "Style": {"style_id", "tenant_id", "effective_from"},
        "Operation": {"operation_id", "tenant_id", "style_id", "order_id", "effective_from"},
        "ProductionPlan": {
            "plan_id",
            "tenant_id",
            "dept_id",
            "order_id",
            "style_id",
            "starts_at",
            "ends_at",
        },
        "PayrollSettlement": {
            "settlement_id",
            "tenant_id",
            "employee_id",
            "dept_id",
            "period_start",
            "period_end",
        },
    }

    schemas = document["components"]["schemas"]
    for schema_name, keys in required_keys.items():
        assert keys <= set(schemas[schema_name]["required"]), schema_name
