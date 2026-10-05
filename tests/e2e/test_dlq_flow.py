"""End-to-end failure path: exhausted attempts route unfinished delivery to the DLQ."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from app.container import ApiContainer, ConsumerContainer
from app.domain.models import PaymentStatus, WebhookStatus
from app.infrastructure.messaging import DLQ_QUEUE, Broker
from app.settings import Settings

from tests.infra import TEST_API_KEY
from tests.integration.harness import (
    WebhookReceiver,
    get_messages,
    wait_for,
    wait_for_queue_message,
)

pytestmark = pytest.mark.e2e

AUTH = {"X-API-Key": TEST_API_KEY}


async def test_attempt_exhaustion_reaches_dead_letter_queue(
    stack: ConsumerContainer,
    api_client: tuple[httpx.AsyncClient, ApiContainer],
    receiver: WebhookReceiver,
    broker: Broker,
    settings: Settings,
) -> None:
    client, _ = api_client
    receiver.fail_remaining = 50  # every delivery fails, so the budget is exhausted
    body: dict[str, Any] = {
        "amount": "7.00",
        "currency": "USD",
        "description": "dlq",
        "metadata": {},
        "webhook_url": receiver.url,
    }
    response = await client.post("/api/v1/payments", json=body, headers={**AUTH, "Idempotency-Key": "e2e-dlq"})
    assert response.status_code == 202
    payment_id = response.json()["payment_id"]

    # The business result still reaches a terminal status despite delivery failures.
    async def _succeeded() -> bool:
        read = await client.get(f"/api/v1/payments/{payment_id}", headers=AUTH)
        return bool(read.json()["status"] == PaymentStatus.SUCCEEDED.value)

    await wait_for(_succeeded, timeout=30, message="business result was not persisted")

    await wait_for_queue_message(settings.rabbitmq_url, DLQ_QUEUE, timeout=40)
    dead_letters = await get_messages(broker, DLQ_QUEUE)
    assert len(dead_letters) == 1
    assert dead_letters[0]["body"]["payment_id"] == payment_id
    assert dead_letters[0]["body"]["attempt_no"] == 3
    assert dead_letters[0]["body"]["reason"]
    assert len(receiver.received) >= 3

    stored = await stack.payments.get_by_id(UUID(payment_id))
    assert stored is not None
    assert stored.status is PaymentStatus.SUCCEEDED
    assert stored.webhook_status is WebhookStatus.FAILED
