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
    "SystemToken",
    "QuerySign",
    "TestPermissions",
    "UserInfoQuery",
    "MoveMenuQuery",
    "HuohaoQuery",
    "HuohaoFormQuery",
    "ScTypeQuery",
    "RfidWorktypeQuery",
    "HuohaoWorktypeQuery",
    "EmployeeQuery",
    "DeptQuery",
    "PlanGridPageList",
    "SclzdGridPageList",
    "SclzdWorktypeQuery",
    "SclzdBarcodeQuery",
    "BarcodeClQuery",
    "HuohaoWtCLQuery",
    "PinFengGridPageList",
    "WorktypeProgressQuery",
    "YskQuery",
    "WskQuery",
    "GongziMxQuery",
    "GongziJeOrderQuery",
    "DgGridPageList",
    "DgZuGridPageList",
    "DgClQuery",
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
    result: dict[str, dict[str, Any]] = {}
    for path_item in document["paths"].values():
        for method, operation in path_item.items():
            if method == "post":
                result[str(operation["operationId"])] = cast(dict[str, Any], operation)
    return result


def response_schema(operation: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    response = resolve(operation["responses"]["200"], document)
    content = cast(dict[str, Any], response)["content"]
    return cast(dict[str, Any], cast(dict[str, Any], content)["application/json"])["schema"]


def test_contract_covers_exactly_the_customer_operations() -> None:
    document = load_openapi()
    assert document["openapi"] == "3.1.0"
    assert set(operations(document)) == EXPECTED_OPERATIONS
    assert all("post" in item and "get" not in item for item in document["paths"].values())


def test_sanitized_examples_validate_against_response_schemas() -> None:
    document = load_openapi()
    contract_operations = operations(document)
    for operation_id, example in load_examples().items():
        schema = resolve(response_schema(contract_operations[operation_id], document), document)
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(example["response"])  # pyright: ignore[reportUnknownMemberType]


def test_examples_supply_every_required_request_body_parameter() -> None:
    document = load_openapi()
    contract_operations = operations(document)
    for operation_id, example in load_examples().items():
        contract_op = contract_operations[operation_id]
        body = example["request"]["body"]
        request_body = resolve(contract_op["requestBody"], document)
        schema = request_body["content"]["application/json"]["schema"]
        required = set(schema.get("required", ()))
        assert required <= set(body), operation_id


def test_customer_envelope_and_list_result_are_canonical() -> None:
    schemas = load_openapi()["components"]["schemas"]
    assert set(schemas["MesEnvelope"]["required"]) == {"code", "message", "result", "timestamp"}
    assert schemas["ListResult"]["required"] == ["list", "total"]
    assert schemas["MesEnvelope"]["properties"]["code"]["enum"] == [0, 1]


def test_business_operations_use_bearer_and_common_json_parameters() -> None:
    document = load_openapi()
    contract_operations = operations(document)
    for operation_id in contract_operations:
        if operation_id == "SystemToken":
            continue
        common = resolve(document["components"]["schemas"]["CommonParams"], document)
        assert {"app_key", "timestamp", "sign"} <= set(common["required"])
        assert {"bearerAuth"} <= {next(iter(item)) for item in document["security"]}
