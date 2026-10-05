"""Durable Outbox publication: confirmation, claiming and crash windows."""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.infrastructure.db import Database
from app.infrastructure.messaging import HEADER_ATTEMPT, PAYMENTS_QUEUE, PAYMENTS_ROUTING_KEY, Broker
from app.infrastructure.relay import OutboxRelay
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.settings import Settings
from sqlalchemy import text

from tests.integration.harness import create_payment_records, get_messages
from tests.unit.fakes import RecordingPublisher

pytestmark = pytest.mark.integration


async def test_event_published_and_marked_after_confirmation(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    relay = OutboxRelay(outbox=outbox, publisher=broker.publisher, settings=settings)

    assert await relay.relay_once() == 1

    refreshed = await outbox.fetch(record.id)
    assert refreshed is not None
    assert refreshed.published_at is not None
    assert await relay.relay_once() == 0  # already marked: not relayed again

    messages = await get_messages(broker, PAYMENTS_QUEUE)
    assert len(messages) == 1
    assert messages[0]["body"]["event_type"] == "payment.created"
    assert messages[0]["body"]["payment_id"] == str(record.payment_id)
    assert messages[0]["headers"][HEADER_ATTEMPT] == 1


async def test_claimed_batch_prevents_double_publication(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    await create_payment_records(payments, outbox)

    first = await outbox.claim_batch(batch_size=10, lease_seconds=60)
    second = await outbox.claim_batch(batch_size=10, lease_seconds=60)
    assert len(first) == 1
    assert second == []


async def test_publish_failure_preserves_the_event(broker: Broker, database: Database, settings: Settings) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    short_lease = settings.model_copy(update={"relay_claim_lease_seconds": 0.05})

    failing = OutboxRelay(outbox=outbox, publisher=RecordingPublisher(fail=True), settings=settings)
    assert await failing.relay_once() == 0
    retained = await outbox.fetch(record.id)
    assert retained is not None
    assert retained.published_at is None

    # Prolonged failure keeps the record; once claimed_at expires it is retried.
    async with database.session() as session, session.begin():
        await session.execute(
            text("UPDATE outbox SET claimed_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": record.id},
        )
    healthy = OutboxRelay(outbox=outbox, publisher=broker.publisher, settings=short_lease)
    assert await healthy.relay_once() == 1
    assert (await outbox.fetch(record.id)).published_at is not None  # type: ignore[union-attr]


async def test_crash_after_confirmation_causes_safe_duplicate(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)

    claimed = await outbox.claim_batch(batch_size=10, lease_seconds=60)
    assert len(claimed) == 1
    # Simulate the crash window: broker confirmed, but published_at was not written.
    await broker.publisher.publish(PAYMENTS_ROUTING_KEY, dict(claimed[0].payload), {HEADER_ATTEMPT: 1})
    async with database.session() as session, session.begin():
        await session.execute(
            text("UPDATE outbox SET claimed_at = now() - interval '1 hour', published_at = NULL WHERE id = :id"),
            {"id": record.id},
        )
    reclaimed = await outbox.claim_batch(batch_size=10, lease_seconds=60)
    assert len(reclaimed) == 1
    await broker.publisher.publish(PAYMENTS_ROUTING_KEY, dict(reclaimed[0].payload), {HEADER_ATTEMPT: 1})

    messages = await get_messages(broker, PAYMENTS_QUEUE, limit=5)
    assert len(messages) >= 2
    assert {message["body"]["event_id"] for message in messages} == {str(record.id)}


async def test_lease_bounds_claim_window(broker: Broker, database: Database, settings: Settings) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    await create_payment_records(payments, outbox)
    await outbox.claim_batch(batch_size=10, lease_seconds=0.05)
    # A fresh claim inside the lease window is skipped.
    assert await outbox.claim_batch(batch_size=10, lease_seconds=0.05) == []
    async with database.session() as session, session.begin():
        await session.execute(text("UPDATE outbox SET claimed_at = now() - interval '1 second'"))
    reclaimable = await outbox.claim_batch(batch_size=10, lease_seconds=0.05)
    assert len(reclaimable) == 1


async def test_mark_published_is_idempotent(broker: Broker, database: Database, settings: Settings) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    await outbox.mark_published([record.id])
    await outbox.mark_published([record.id])
    refreshed = await outbox.fetch(record.id)
    assert refreshed is not None
    assert refreshed.published_at is not None
    assert refreshed.published_at >= record.created_at - timedelta(days=1)
