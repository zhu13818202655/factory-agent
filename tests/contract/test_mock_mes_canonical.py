from __future__ import annotations

from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator, FormatChecker
from mock_mes.api.server import create_app

from tests.contract.test_canonical_mes_openapi import (
    load_examples,
    load_openapi,
    operations,
    resolve,
    response_schema,
)


def encode_query(query: dict[str, Any]) -> dict[str, str | int]:
    encoded: dict[str, str | int] = {}
    for key, value in query.items():
        if isinstance(value, list):
            encoded[key] = ",".join(str(item) for item in cast(list[object], value))
        elif isinstance(value, (str, int)):
            encoded[key] = value
        else:
            raise AssertionError(f"unsupported example query value for {key}")
    return encoded


@pytest.mark.asyncio
async def test_every_mock_operation_returns_canonical_schema() -> None:
    document = load_openapi()
    contract_operations = operations(document)
    path_by_operation = {
        path_item["get"]["operationId"]: path for path, path_item in document["paths"].items()
    }
    transport = ASGITransport(app=create_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for operation_id, example in load_examples().items():
            headers = {"Authorization": "Bearer tenant-a-user", **example["request"]["headers"]}
            response = await client.get(
                path_by_operation[operation_id],
                headers=headers,
                params=encode_query(example["request"]["query"]),
            )
            assert response.status_code == 200, (operation_id, response.text)
            schema = resolve(response_schema(contract_operations[operation_id], document), document)
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(  # pyright: ignore[reportUnknownMemberType]
                response.json()
            )
