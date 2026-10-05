"""Bounded retry, durable scheduling, dispatcher and dead-letter queue."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.application.processing import MessageResult
from app.consumer.runner import ConsumerRunner
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.errors import GatewayTechnicalError
from app.infrastructure.db import Database
from app.infrastructure.messaging import DLQ_QUEUE, HEADER_ATTEMPT, PAYMENTS_QUEUE, Broker
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.infrastructure.retry_dispatcher import RetryDispatcher
from app.settings import Settings
from sqlalchemy import text

from tests.integration.harness import build_processor, create_payment_records, get_messages
from tests.unit.fakes import RecordingGateway, RecordingWebhookSender, make_envelope

pytestmark = pytest.mark.integration


async def _make_due(database: Database, event_id: object) -> None:
    async with database.session() as session, session.begin():
        await session.execute(
            text("UPDATE outbox SET next_attempt_at = now() - interval '1 second' WHERE id = :id"),
            {"id": event_id},
        )


async def test_first_failure_schedules_retry_then_dispatcher_publishes(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox)
    gateway = RecordingGateway(error=GatewayTechnicalError("gateway boom"))
    processor = build_processor(
        settings, database, broker, gateway=gateway, webhooks=RecordingWebhookSender(), owner="p1"
    )

    assert await processor.process(make_envelope(record, attempt_no=1)) is MessageResult.ACK
    scheduled = await outbox.fetch(record.id)
    assert scheduled is not None
    assert scheduled.attempt_no == 1
    assert scheduled.attempt_state is AttemptState.AWAITING_RETRY
    assert scheduled.next_attempt_no == 2
    assert scheduled.next_attempt_at is not None
    assert "gateway boom" in (scheduled.last_error or "")

    await asyncio.sleep(0.1)
    dispatcher = RetryDispatcher(outbox=outbox, publisher=broker.publisher, settings=settings)
    assert await dispatcher.dispatch_once() == 1

    dispatched = await outbox.fetch(record.id)
    assert dispatched is not None
    assert dispatched.next_attempt_at is None
    messages = await get_messages(broker, PAYMENTS_QUEUE)
    assert len(messages) == 1
    assert messages[0]["headers"][HEADER_ATTEMPT] == 2

    # The second attempt succeeds and completes the payment.
    healthy = RecordingGateway(status=PaymentStatus.SUCCEEDED)
    processor_ok = build_processor(
        settings, database, broker, gateway=healthy, webhooks=RecordingWebhookSender(), owner="p2"
    )
    assert await processor_ok.process(make_envelope(record, attempt_no=2)) is MessageResult.ACK
    final = await outbox.fetch(record.id)
    assert final is not None
    assert final.attempt_state is AttemptState.COMPLETED
    completed = await payments.get_by_id(payment.id)
    assert completed is not None
    assert completed.status is PaymentStatus.SUCCEEDED
    assert completed.webhook_status is WebhookStatus.DELIVERED


async def test_budget_exhaustion_dead_letters_unfinished_work(
    broker: Broker, database: Database, settings: Settings
) -> None:
    limited = settings.model_copy(update={"max_attempts": 3})
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox)
    gateway = RecordingGateway(error=GatewayTechnicalError("boom"))

    for attempt_number in (1, 2, 3):
        if attempt_number > 1:
            await _make_due(database, record.id)
        processor = build_processor(
            limited,
            database,
            broker,
            gateway=gateway,
            webhooks=RecordingWebhookSender(),
            owner=f"p{attempt_number}",
        )
        assert await processor.process(make_envelope(record, attempt_no=attempt_number)) is MessageResult.ACK

    refreshed = await outbox.fetch(record.id)
    assert refreshed is not None
    assert refreshed.attempt_no == 3
    assert refreshed.attempt_state is AttemptState.COMPLETED

    dlq = await get_messages(broker, DLQ_QUEUE)
    assert len(dlq) == 1
    assert dlq[0]["body"]["attempt_no"] == 3
    assert dlq[0]["body"]["payment_id"] == str(payment.id)
    assert "boom" in dlq[0]["body"]["reason"]

    unfinished = await payments.get_by_id(payment.id)
    assert unfinished is not None
    assert unfinished.status is PaymentStatus.PENDING  # unfinished work is not completed


async def test_completed_work_is_not_dead_lettered(broker: Broker, database: Database, settings: Settings) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    gateway = RecordingGateway(status=PaymentStatus.SUCCEEDED)
    processor = build_processor(
        settings, database, broker, gateway=gateway, webhooks=RecordingWebhookSender(), owner="done"
    )
    assert await processor.process(make_envelope(record)) is MessageResult.ACK
    # Redelivery of completed work: acknowledged, no gateway, no dead letter.
    assert await processor.process(make_envelope(record)) is MessageResult.ACK
    assert len(gateway.calls) == 1
    assert await get_messages(broker, DLQ_QUEUE) == []


async def test_stale_duplicate_while_retry_awaits_is_ignored(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    gateway = RecordingGateway(error=GatewayTechnicalError("boom"))
    processor = build_processor(
        settings, database, broker, gateway=gateway, webhooks=RecordingWebhookSender(), owner="p1"
    )
    await processor.process(make_envelope(record, attempt_no=1))

    # An old duplicate (attempt 1) arrives while the schedule waits for attempt 2.
    assert await processor.process(make_envelope(record, attempt_no=1)) is MessageResult.ACK
    assert len(gateway.calls) == 1
    scheduled = await outbox.fetch(record.id)
    assert scheduled is not None
    assert scheduled.attempt_state is AttemptState.AWAITING_RETRY
    assert scheduled.next_attempt_no == 2


async def test_invalid_and_unknown_messages_are_handled_deterministically(
    broker: Broker, database: Database, settings: Settings
) -> None:
    processor = build_processor(
        settings,
        database,
        broker,
        gateway=RecordingGateway(),
        webhooks=RecordingWebhookSender(),
        owner="handler",
    )
    runner = ConsumerRunner(
        broker=broker,
        processor=processor,
        stop_event=asyncio.Event(),
        settings=settings,
    )
    assert await runner.handle_raw(b"{not json", {HEADER_ATTEMPT: "1"}) is MessageResult.ACK

    unknown = {
        "event_id": str(uuid4()),
        "event_type": "payment.created",
        "payment_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    assert await runner.handle_raw(json.dumps(unknown).encode("utf-8"), {HEADER_ATTEMPT: "1"}) is MessageResult.ACK
