"""Stable terminal result under crash windows and concurrent writers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.domain.webhook import webhook_event_id
from app.infrastructure.db import Database
from app.infrastructure.messaging import Broker
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.settings import Settings

from tests.integration.harness import create_payment_records

pytestmark = pytest.mark.integration


async def test_conditional_terminal_update_has_exactly_one_winner(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox)
    claim = await outbox.claim_attempt(event_id=record.id, message_attempt_no=1, owner="owner", lease_seconds=60)
    assert claim is not None

    processed_at = datetime.now(UTC)
    event_id = webhook_event_id(payment.id, PaymentStatus.SUCCEEDED)
    first = await payments.mark_terminal(
        payment_id=payment.id,
        status=PaymentStatus.SUCCEEDED,
        processed_at=processed_at,
        webhook_event_id=event_id,
        owner="owner",
        epoch=claim.attempt_epoch,
    )
    second = await payments.mark_terminal(
        payment_id=payment.id,
        status=PaymentStatus.FAILED,
        processed_at=datetime.now(UTC),
        webhook_event_id=webhook_event_id(payment.id, PaymentStatus.FAILED),
        owner="owner",
        epoch=claim.attempt_epoch,
    )
    assert first is True
    assert second is False

    stored = await payments.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatus.SUCCEEDED
    assert stored.processed_at == processed_at
    assert stored.webhook_event_id == event_id


async def test_fenced_writes_fail_after_takeover(broker: Broker, database: Database, settings: Settings) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    payment, record = await create_payment_records(payments, outbox)
    stale = await outbox.claim_attempt(event_id=record.id, message_attempt_no=1, owner="old", lease_seconds=0.01)
    assert stale is not None
    await asyncio.sleep(0.1)

    current = await outbox.takeover(event_id=record.id, owner="new", lease_seconds=60, max_recoveries=5)
    assert current is not None
    assert current.attempt_epoch > stale.attempt_epoch
    assert current.attempt_no == stale.attempt_no

    # A write from the fenced owner is rejected and leaves the stored state untouched.
    assert (
        await payments.mark_terminal(
            payment_id=payment.id,
            status=PaymentStatus.SUCCEEDED,
            processed_at=datetime.now(UTC),
            webhook_event_id=webhook_event_id(payment.id, PaymentStatus.SUCCEEDED),
            owner="old",
            epoch=stale.attempt_epoch,
        )
        is False
    )
    stored = await payments.get_by_id(payment.id)
    assert stored is not None
    assert stored.status is PaymentStatus.PENDING
    assert stored.processed_at is None
    assert stored.webhook_status is WebhookStatus.PENDING


async def test_recovery_before_result_keeps_attempt_number(
    broker: Broker, database: Database, settings: Settings
) -> None:
    payments = PaymentRepository(database)
    outbox = OutboxRepository(database)
    _, record = await create_payment_records(payments, outbox)
    await outbox.claim_attempt(event_id=record.id, message_attempt_no=1, owner="crashed", lease_seconds=0.01)
    await asyncio.sleep(0.1)

    recovered = await outbox.takeover(event_id=record.id, owner="recovery", lease_seconds=60, max_recoveries=5)
    assert recovered is not None
    assert recovered.attempt_no == 1  # crash between claim and work does not create attempt 0 or 2
    stored = await outbox.fetch(record.id)
    assert stored is not None
    assert stored.attempt_state is AttemptState.OPEN
    assert stored.recoveries == 1
