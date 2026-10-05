"""Webhook delivery success criteria, retries and the duplicate window."""

from __future__ import annotations

from typing import Any

import pytest
from app.application.processing import MessageResult
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.infrastructure.db import Database
from app.infrastructure.messaging import Broker
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.infrastructure.webhook_client import HttpxWebhookSender
from app.settings import Settings
from sqlalchemy import text

from tests.integration.harness import WebhookReceiver, build_processor, create_payment_records
from tests.unit.fakes import RecordingGateway, make_envelope

pytestmark = pytest.mark.integration


async def _delivered(database: Database, payment_id: Any) -> bool:
    async with database.session() as session:
        status = (
            await session.execute(text("SELECT webhook_status FROM payments WHERE id = :id"), {"id": payment_id})
        ).scalar_one()
    return bool(status == WebhookStatus.DELIVERED.value)


async def test_successful_delivery_persists_state(
    broker: Broker, database: Database, settings: Settings, receiver: WebhookReceiver
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox, webhook_url=receiver.url)
    sender = HttpxWebhookSender.create(timeout_seconds=2.0)
    processor = build_processor(
        settings,
        database,
        broker,
        gateway=RecordingGateway(status=PaymentStatus.SUCCEEDED),
        webhooks=sender,
        owner="webhook-1",
    )
    try:
        assert await processor.process(make_envelope(record)) is MessageResult.ACK
    finally:
        await sender.aclose()

    assert len(receiver.received) == 1
    body = receiver.received[0]
    assert body["status"] == "succeeded"
    assert body["payment_id"] == str(payment.id)
    assert set(body) == {"event_id", "payment_id", "status", "processed_at"}

    stored = await payments.get_by_id(payment.id)
    assert stored is not None
    assert stored.webhook_status is WebhookStatus.DELIVERED
    assert stored.webhook_attempts == 1
    assert stored.status is PaymentStatus.SUCCEEDED


async def test_non_2xx_is_retryable_and_business_status_unchanged(
    broker: Broker, database: Database, settings: Settings, receiver: WebhookReceiver
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox, webhook_url=receiver.url)
    receiver.fail_remaining = 1
    sender = HttpxWebhookSender.create(timeout_seconds=2.0)
    processor = build_processor(
        settings,
        database,
        broker,
        gateway=RecordingGateway(status=PaymentStatus.SUCCEEDED),
        webhooks=sender,
        owner="webhook-2",
    )
    try:
        assert await processor.process(make_envelope(record)) is MessageResult.ACK
        assert len(receiver.received) == 1
        stored = await payments.get_by_id(payment.id)
        assert stored is not None
        assert stored.status is PaymentStatus.SUCCEEDED  # business status unchanged
        assert stored.webhook_status is WebhookStatus.PENDING
        assert stored.webhook_attempts == 1
        assert stored.webhook_last_error is not None
        retried = await outbox.fetch(record.id)
        assert retried is not None
        assert retried.attempt_state is AttemptState.AWAITING_RETRY

        # Make the scheduled retry due and deliver again with a healthy receiver.
        async with database.session() as session, session.begin():
            await session.execute(
                text("UPDATE outbox SET next_attempt_at = now() - interval '1 second' WHERE id = :id"),
                {"id": record.id},
            )
        assert await processor.process(make_envelope(record, attempt_no=2)) is MessageResult.ACK
    finally:
        await sender.aclose()

    assert len(receiver.received) == 2
    assert await _delivered(database, payment.id)


async def test_timeout_is_retryable(
    broker: Broker, database: Database, settings: Settings, receiver: WebhookReceiver
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox, webhook_url=receiver.url)
    receiver.delay_seconds = 0.5
    sender = HttpxWebhookSender.create(timeout_seconds=0.1)
    processor = build_processor(
        settings,
        database,
        broker,
        gateway=RecordingGateway(status=PaymentStatus.SUCCEEDED),
        webhooks=sender,
        owner="webhook-3",
    )
    try:
        assert await processor.process(make_envelope(record)) is MessageResult.ACK
    finally:
        await sender.aclose()
    stored = await payments.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatus.SUCCEEDED
    retried = await outbox.fetch(record.id)
    assert retried is not None
    assert retried.attempt_state is AttemptState.AWAITING_RETRY


async def test_crash_after_acceptance_redelivers_with_the_same_event_id(
    broker: Broker, database: Database, settings: Settings, receiver: WebhookReceiver
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox, webhook_url=receiver.url)
    sender = HttpxWebhookSender.create(timeout_seconds=2.0)
    processor = build_processor(
        settings,
        database,
        broker,
        gateway=RecordingGateway(status=PaymentStatus.SUCCEEDED),
        webhooks=sender,
        owner="webhook-4",
    )
    try:
        assert await processor.process(make_envelope(record)) is MessageResult.ACK
        # Simulate a crash after the receiver accepted the webhook but before the
        # delivery mark was persisted: reset the delivery state and outbox attempt.
        async with database.session() as session, session.begin():
            await session.execute(
                text("UPDATE payments SET webhook_status = 'pending' WHERE id = :id"), {"id": payment.id}
            )
            await session.execute(
                text(
                    "UPDATE outbox SET attempt_state = 'awaiting_retry', attempt_owner = NULL, "
                    "next_attempt_no = 2, next_attempt_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": record.id},
            )
        assert await processor.process(make_envelope(record, attempt_no=2)) is MessageResult.ACK
    finally:
        await sender.aclose()

    assert len(receiver.received) == 2
    assert receiver.received[0]["event_id"] == receiver.received[1]["event_id"]
    assert await _delivered(database, payment.id)
