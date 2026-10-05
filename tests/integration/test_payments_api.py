"""Payment creation and read endpoints against real PostgreSQL."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.container import ApiContainer

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": "10.50",
        "currency": "RUB",
        "description": "integration",
        "metadata": {"order": "42"},
        "webhook_url": "http://receiver.test/webhook",
    }
    body.update(overrides)
    return body


async def test_create_and_read_payment(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    response = await client.post("/api/v1/payments", json=_body(), headers={**AUTH, "Idempotency-Key": "create-1"})
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "pending"
    assert set(data) == {"payment_id", "status", "created_at"}

    read = await client.get(f"/api/v1/payments/{data['payment_id']}", headers=AUTH)
    assert read.status_code == 200
    view = read.json()
    assert view["payment_id"] == data["payment_id"]
    assert view["amount"] == "10.50"
    assert view["currency"] == "RUB"
    assert view["status"] == "pending"
    assert view["processed_at"] is None
    assert view["metadata"] == {"order": "42"}


async def test_unknown_payment_is_404_and_malformed_is_422(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    unknown = await client.get(f"/api/v1/payments/{uuid4()}", headers=AUTH)
    assert unknown.status_code == 404
    malformed = await client.get("/api/v1/payments/not-a-uuid", headers=AUTH)
    assert malformed.status_code == 422


async def test_invalid_input_is_422(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    bad_currency = await client.post(
        "/api/v1/payments", json=_body(currency="GBP"), headers={**AUTH, "Idempotency-Key": "bad"}
    )
    assert bad_currency.status_code == 422
    bad_amount = await client.post(
        "/api/v1/payments", json=_body(amount="0"), headers={**AUTH, "Idempotency-Key": "bad-2"}
    )
    assert bad_amount.status_code == 422
    too_many_decimals = await client.post(
        "/api/v1/payments", json=_body(amount="1.234"), headers={**AUTH, "Idempotency-Key": "bad-3"}
    )
    assert too_many_decimals.status_code == 422
    bad_url = await client.post(
        "/api/v1/payments",
        json=_body(webhook_url="not-a-url"),
        headers={**AUTH, "Idempotency-Key": "bad-4"},
    )
    assert bad_url.status_code == 422


async def test_missing_idempotency_key_is_422(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    response = await client.post("/api/v1/payments", json=_body(), headers=AUTH)
    assert response.status_code == 422
    empty = await client.post("/api/v1/payments", json=_body(), headers={**AUTH, "Idempotency-Key": ""})
    assert empty.status_code == 422
