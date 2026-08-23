from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from mock_mes.api.server import create_app

COMMON_QUERY = {
    "authorized_employee_ids": "employee-a1",
    "authorized_dept_ids": "group-a1",
    "from": "2026-08-01T00:00:00Z",
    "to": "2026-09-01T00:00:00Z",
}
COMMON_HEADERS = {
    "Authorization": "Bearer tenant-a-user",
    "X-Tenant-Id": "tenant-a",
}


@pytest.mark.asyncio
async def test_scope_ids_cannot_exceed_server_effective_scope() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/piecework-records",
            headers=COMMON_HEADERS,
            params={**COMMON_QUERY, "authorized_employee_ids": "employee-a2"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert "employee-a2" not in response.text


@pytest.mark.asyncio
async def test_each_credential_resolves_unique_tenant_local_scope() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        tenant_a = await client.get(
            "/v1/effective-scopes",
            headers=COMMON_HEADERS,
            params={"as_of": "2026-08-21T08:00:00Z"},
        )
        tenant_b = await client.get(
            "/v1/effective-scopes",
            headers={"Authorization": "Bearer tenant-b-user", "X-Tenant-Id": "tenant-b"},
            params={"as_of": "2026-08-21T08:00:00Z"},
        )

    assert tenant_a.json()["items"][0]["scope_id"] == "scope-a1"
    assert tenant_b.json()["items"][0]["scope_id"] == "scope-b1"
    assert (
        tenant_a.json()["items"][0]["employee_ids"] != tenant_b.json()["items"][0]["employee_ids"]
    )


@pytest.mark.asyncio
async def test_membership_returns_single_object_per_credential() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/identity/memberships",
            headers=COMMON_HEADERS,
            params={"as_of": "2026-08-21T08:00:00Z"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == "user-a"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["role"] in {"employee", "manager", "owner"}
    assert "items" not in payload


@pytest.mark.asyncio
async def test_credential_cannot_access_other_tenant_membership_scope() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cross_tenant = await client.get(
            "/v1/effective-scopes",
            headers={"Authorization": "Bearer tenant-a-user", "X-Tenant-Id": "tenant-b"},
            params={"as_of": "2026-08-21T08:00:00Z"},
        )

    assert cross_tenant.status_code == 403
    assert cross_tenant.json()["code"] == "forbidden"


@pytest.mark.asyncio
async def test_batch_filter_can_return_empty_page() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/piecework-records",
            headers=COMMON_HEADERS,
            params={**COMMON_QUERY, "order_ids": "order-missing"},
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "size": 50}


@pytest.mark.asyncio
async def test_list_pagination_is_stable() -> None:
    headers = {"Authorization": "Bearer manager-a", "X-Tenant-Id": "tenant-a"}
    query = {
        "authorized_employee_ids": "employee-a1,employee-a2,employee-a3,employee-a9",
        "authorized_dept_ids": "workshop-a1,group-a1,group-a2",
        "from": "2026-01-01T00:00:00Z",
        "to": "2027-01-01T00:00:00Z",
        "size": 1,
    }
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/employees", headers=headers, params={**query, "page": 1})
        second = await client.get("/v1/employees", headers=headers, params={**query, "page": 2})

    assert first.json()["total"] == 4
    assert first.json()["items"][0]["employee_id"] == "employee-a1"
    assert second.json()["items"][0]["employee_id"] == "employee-a2"
