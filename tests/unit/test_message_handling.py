"""Deterministic handling of invalid payloads and unknown identifiers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from unittest.mock import AsyncMock, Mock

import pytest
from app.application.processing import MessageResult, PaymentProcessor
from app.consumer.runner import ConsumerRunner
from app.infrastructure.messaging import HEADER_ATTEMPT
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.settings import Settings

from tests.unit.fakes import (
    BASE_TIME,
    FakeClock,
    RecordingGateway,
    RecordingPublisher,
    RecordingWebhookSender,
    make_envelope,
    make_outbox_record,
    make_payment_record,
)

pytestmark = pytest.mark.unit


def _processor(settings: Settings, payments: AsyncMock, outbox: AsyncMock) -> PaymentProcessor:
    return PaymentProcessor(
        settings=settings,
        payments=payments,
        outbox=outbox,
        gateway=RecordingGateway(),
        webhooks=RecordingWebhookSender(),
        publisher=RecordingPublisher(),
        clock=FakeClock(),
        owner="test-owner",
    )


async def test_invalid_payload_is_acknowledged_without_processing(
    settings_factory: Callable[..., Settings],
) -> None:
    processor = Mock(spec=PaymentProcessor)
    runner = ConsumerRunner(
        broker=Mock(),
        processor=processor,
        stop_event=asyncio.Event(),
        settings=settings_factory(),
    )
    result = await runner.handle_raw(b"{not json", {HEADER_ATTEMPT: "1"})
    assert result is MessageResult.ACK
    processor.process.assert_not_called()


async def test_unknown_outbox_event_is_acknowledged(settings_factory: Callable[..., Settings]) -> None:
    payments = AsyncMock(spec=PaymentRepository)
    outbox = AsyncMock(spec=OutboxRepository)
    outbox.fetch.return_value = None
    processor = _processor(settings_factory(), payments, outbox)
    record = make_outbox_record()
    result = await processor.process(make_envelope(record))
    assert result is MessageResult.ACK
    payments.get_by_id.assert_not_awaited()


async def test_unknown_payment_is_acknowledged(settings_factory: Callable[..., Settings]) -> None:
    payments = AsyncMock(spec=PaymentRepository)
    payments.get_by_id.return_value = None
    record = make_outbox_record()
    outbox = AsyncMock(spec=OutboxRepository)
    outbox.fetch.return_value = record
    processor = _processor(settings_factory(), payments, outbox)
    result = await processor.process(make_envelope(record))
    assert result is MessageResult.ACK


async def test_valid_payload_is_decoded_and_delegated(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    record = make_outbox_record(payment_id=payment.id)
    valid_body = json.dumps(record.payload).encode("utf-8")
    processor = Mock(spec=PaymentProcessor)
    processor.process.return_value = MessageResult.ACK
    runner = ConsumerRunner(
        broker=Mock(),
        processor=processor,
        stop_event=asyncio.Event(),
        settings=settings_factory(),
    )
    result = await runner.handle_raw(valid_body, {HEADER_ATTEMPT: "2"})
    assert result is MessageResult.ACK
    processor.process.assert_awaited_once()
    envelope = processor.process.await_args.args[0]
    assert envelope.attempt_no == 2
    assert envelope.occurred_at == BASE_TIME
    assert envelope.payment_id == record.payment_id
