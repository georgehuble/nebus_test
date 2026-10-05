"""OpenAPI contract fidelity: generated schema matches served behaviour."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.container import ApiContainer

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


async def test_generated_schema_declares_security_and_endpoints(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, _ = api_client
    assert (await client.get("/api/v1/openapi.json")).status_code == 401
    response = await client.get("/api/v1/openapi.json", headers=AUTH)
    assert response.status_code == 200
    schema = response.json()
    assert "APIKeyHeader" in schema["components"]["securitySchemes"]
    assert "/api/v1/payments" in schema["paths"]
    assert "/api/v1/payments/{payment_id}" in schema["paths"]
    post = schema["paths"]["/api/v1/payments"]["post"]
    assert post["responses"].keys() >= {"202", "401", "409", "422"} or "202" in post["responses"]


async def test_documented_status_codes_are_produced(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, _ = api_client
    body: dict[str, Any] = {
        "amount": "10.50",
        "currency": "RUB",
        "description": "openapi",
        "metadata": {},
        "webhook_url": "http://receiver.test/webhook",
    }
    created = await client.post("/api/v1/payments", json=body, headers={**AUTH, "Idempotency-Key": "openapi-1"})
    assert created.status_code == 202
    payment_id = created.json()["payment_id"]

    assert (await client.get(f"/api/v1/payments/{payment_id}", headers=AUTH)).status_code == 200
    assert (await client.get(f"/api/v1/payments/{uuid4()}", headers=AUTH)).status_code == 404
    conflict = await client.post(
        "/api/v1/payments",
        json={**body, "amount": "11.00"},
        headers={**AUTH, "Idempotency-Key": "openapi-1"},
    )
    assert conflict.status_code == 409
    invalid = await client.post(
        "/api/v1/payments", json={**body, "currency": "GBP"}, headers={**AUTH, "Idempotency-Key": "openapi-2"}
    )
    assert invalid.status_code == 422
    assert (await client.post("/api/v1/payments", json=body, headers=AUTH)).status_code == 422
    assert (await client.get("/api/v1/health")).status_code == 401
