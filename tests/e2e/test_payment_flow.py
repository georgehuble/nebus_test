"""End-to-end happy path: POST -> outbox -> RabbitMQ -> consumer -> webhook -> GET."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from app.container import ApiContainer, ConsumerContainer
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.domain.webhook import webhook_event_id
from app.infrastructure.messaging import Broker
from app.settings import Settings

from tests.infra import TEST_API_KEY
from tests.integration.harness import WebhookReceiver, wait_for, wait_for_count

pytestmark = pytest.mark.e2e

AUTH = {"X-API-Key": TEST_API_KEY}


async def test_full_payment_flow(
    stack: ConsumerContainer,
    api_client: tuple[httpx.AsyncClient, ApiContainer],
    receiver: WebhookReceiver,
    broker: Broker,
    settings: Settings,
) -> None:
    client, _ = api_client
    body: dict[str, Any] = {
        "amount": "42.00",
        "currency": "RUB",
        "description": "e2e",
        "metadata": {"order": "e2e"},
        "webhook_url": receiver.url,
    }
    response = await client.post("/api/v1/payments", json=body, headers={**AUTH, "Idempotency-Key": "e2e-flow"})
    assert response.status_code == 202
    payment_id = response.json()["payment_id"]

    async def _terminal() -> bool:
        read = await client.get(f"/api/v1/payments/{payment_id}", headers=AUTH)
        return bool(read.json()["status"] != PaymentStatus.PENDING.value)

    await wait_for(_terminal, timeout=30, message="payment did not reach a terminal status")

    view = (await client.get(f"/api/v1/payments/{payment_id}", headers=AUTH)).json()
    assert view["status"] == PaymentStatus.SUCCEEDED.value
    assert view["processed_at"] is not None
    assert view["amount"] == "42.00"

    await wait_for_count(receiver.received, 1, timeout=20)
    delivered = receiver.received[0]
    assert delivered["payment_id"] == payment_id
    assert delivered["status"] == PaymentStatus.SUCCEEDED.value
    assert delivered["event_id"] == str(webhook_event_id(UUID(payment_id), PaymentStatus.SUCCEEDED))

    stored = await stack.payments.get_by_id(UUID(payment_id))
    assert stored is not None
    assert stored.webhook_status is WebhookStatus.DELIVERED
    assert stored.webhook_attempts >= 1

    outbox = await stack.outbox.fetch_by_payment(UUID(payment_id))
    assert outbox is not None
    assert outbox.published_at is not None
    assert outbox.attempt_state is AttemptState.COMPLETED
