"""Claim leases, heartbeat takeover and fencing against real PostgreSQL and RabbitMQ."""

from __future__ import annotations

import asyncio

import pytest
from app.application.processing import MessageResult
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.infrastructure.db import Database
from app.infrastructure.messaging import HEADER_ATTEMPT, HEADER_REDELIVERY, PAYMENTS_QUEUE, Broker
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.settings import Settings

from tests.integration.harness import build_processor, create_payment_records, get_messages
from tests.unit.fakes import RecordingGateway, RecordingWebhookSender, make_envelope

pytestmark = pytest.mark.integration


async def test_active_foreign_lease_publishes_durable_copy_without_consuming_attempt(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    claim = await outbox.claim_attempt(event_id=record.id, message_attempt_no=1, owner="owner-a", lease_seconds=60)
    assert claim is not None

    processor = build_processor(
        settings,
        database,
        broker,
        gateway=RecordingGateway(),
        webhooks=RecordingWebhookSender(),
        owner="owner-b",
    )
    result = await processor.process(make_envelope(record))
    assert result is MessageResult.ACK

    refreshed = await outbox.fetch(record.id)
    assert refreshed is not None
    assert refreshed.attempt_no == 1
    assert refreshed.attempt_state is AttemptState.OPEN

    messages = await get_messages(broker, PAYMENTS_QUEUE)
    assert len(messages) == 1
    assert messages[0]["headers"][HEADER_REDELIVERY] is True
    assert messages[0]["headers"][HEADER_ATTEMPT] == 1


async def test_expired_lease_takeover_fences_the_previous_owner(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox)
    stale = await outbox.claim_attempt(event_id=record.id, message_attempt_no=1, owner="owner-a", lease_seconds=0.05)
    assert stale is not None
    await asyncio.sleep(0.1)

    gateway = RecordingGateway(status=PaymentStatus.SUCCEEDED)
    processor = build_processor(
        settings,
        database,
        broker,
        gateway=gateway,
        webhooks=RecordingWebhookSender(),
        owner="owner-b",
    )
    result = await processor.process(make_envelope(record))
    assert result is MessageResult.ACK

    refreshed = await outbox.fetch(record.id)
    assert refreshed is not None
    assert refreshed.attempt_no == 1  # same attempt recovered, not a new one
    assert refreshed.attempt_epoch > stale.attempt_epoch
    assert refreshed.attempt_state is AttemptState.COMPLETED

    # The previous owner's writes are rejected by the fencing token.
    assert (
        await outbox.heartbeat(event_id=record.id, owner="owner-a", epoch=stale.attempt_epoch, lease_seconds=5) is False
    )
    assert await outbox.complete(event_id=record.id, owner="owner-a", epoch=stale.attempt_epoch) is False

    completed = await payments.get_by_id(payment.id)
    assert completed is not None
    assert completed.status is PaymentStatus.SUCCEEDED
    assert completed.webhook_status is WebhookStatus.DELIVERED
    assert len(gateway.calls) == 1


async def test_recovery_continues_the_same_attempt_and_produces_one_result(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox)
    await outbox.claim_attempt(event_id=record.id, message_attempt_no=1, owner="crashed", lease_seconds=0.05)
    await asyncio.sleep(0.1)

    gateway = RecordingGateway(status=PaymentStatus.SUCCEEDED)
    webhooks = RecordingWebhookSender()
    processor = build_processor(settings, database, broker, gateway=gateway, webhooks=webhooks, owner="recover")
    assert await processor.process(make_envelope(record)) is MessageResult.ACK

    # A second delivery of the now-completed attempt is acknowledged without a
    # second gateway call and without a second terminal result.
    assert await processor.process(make_envelope(record)) is MessageResult.ACK
    assert len(gateway.calls) == 1
    assert len(webhooks.sent) == 1
    refreshed = await payments.get_by_id(payment.id)
    assert refreshed is not None
    assert refreshed.status is PaymentStatus.SUCCEEDED
