"""Idempotent creation against the database uniqueness constraint."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from app.container import ApiContainer
from sqlalchemy import text

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": "10.50",
        "currency": "RUB",
        "description": "idempotency",
        "metadata": {},
        "webhook_url": "http://receiver.test/webhook",
    }
    body.update(overrides)
    return body


async def _counts(container: ApiContainer) -> tuple[int, int]:
    async with container.database.session() as session:
        payments = (await session.execute(text("SELECT count(*) FROM payments"))).scalar_one()
        outbox = (await session.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
    return int(payments), int(outbox)


async def test_repeat_with_same_body_is_deduplicated(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, container = api_client
    headers = {**AUTH, "Idempotency-Key": "same-body"}
    first = await client.post("/api/v1/payments", json=_body(), headers=headers)
    second = await client.post("/api/v1/payments", json=_body(), headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert first.json()["created_at"] == second.json()["created_at"]
    assert await _counts(container) == (1, 1)


async def test_equivalent_decimal_spellings_are_the_same_body(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, container = api_client
    headers = {**AUTH, "Idempotency-Key": "decimal"}
    first = await client.post("/api/v1/payments", json=_body(amount="10.5"), headers=headers)
    second = await client.post("/api/v1/payments", json=_body(amount="10.50"), headers=headers)
    assert second.status_code == 202
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert await _counts(container) == (1, 1)


async def test_conflicting_body_is_rejected(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, container = api_client
    headers = {**AUTH, "Idempotency-Key": "conflict"}
    first = await client.post("/api/v1/payments", json=_body(amount="10.50"), headers=headers)
    assert first.status_code == 202
    conflict = await client.post("/api/v1/payments", json=_body(amount="11.00"), headers=headers)
    assert conflict.status_code == 409
    assert await _counts(container) == (1, 1)


async def test_concurrent_identical_requests_create_one_payment(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, container = api_client
    headers = {**AUTH, "Idempotency-Key": "concurrent"}
    responses = await asyncio.gather(
        *(client.post("/api/v1/payments", json=_body(), headers=headers) for _ in range(2))
    )
    assert all(response.status_code == 202 for response in responses)
    payment_ids = {response.json()["payment_id"] for response in responses}
    assert len(payment_ids) == 1
    assert await _counts(container) == (1, 1)
