from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator, FormatChecker
from mock_mes.api.server import create_app

from tests.contract.test_canonical_mes_openapi import (
    load_openapi,
    operations,
    resolve,
    response_schema,
)


@pytest.mark.asyncio
async def test_mock_token_matches_customer_contract() -> None:
    document = load_openapi()
    operation = operations(document)["SystemToken"]
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post("/api/system/token", json={"app_key": "APPKEY-A"})

    assert response.status_code == 200
    schema = resolve(response_schema(operation, document), document)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(response.json())  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_mock_customer_endpoint_returns_envelope_and_list_result() -> None:
    document = load_openapi()
    operation = operations(document)["YskQuery"]
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        token = (await client.post("/api/system/token", json={"app_key": "APPKEY-A"})).json()[
            "result"
        ]
        response = await client.post(
            "/api/NetYf/Sclzd/YskQuery",
            headers={"Authorization": f"Bearer {token['accessToken']}"},
            json={
                "app_key": token["appkey"],
                "timestamp": token["timestamp"],
                "sign": token["sign"],
                "page": 1,
                "size": 50,
                "dates": "2026-07-01",
                "datee": "2026-08-31",
                "Uid": "01001",
            },
        )

    assert response.status_code == 200
    schema = resolve(response_schema(operation, document), document)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(response.json())  # pyright: ignore[reportUnknownMemberType]
