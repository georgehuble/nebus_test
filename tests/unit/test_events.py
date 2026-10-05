"""Outbox event serialization and envelope decoding."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.domain.events import PAYMENT_CREATED_EVENT, MessageEnvelope, PaymentCreatedEvent

pytestmark = pytest.mark.unit

OCCURRED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_payment_created_event_serialization() -> None:
    event_id = uuid4()
    payment_id = uuid4()
    event = PaymentCreatedEvent(event_id=event_id, payment_id=payment_id, occurred_at=OCCURRED_AT)
    dumped = event.model_dump(mode="json")
    assert set(dumped) == {"event_id", "event_type", "payment_id", "occurred_at"}
    assert dumped["event_id"] == str(event_id)
    assert dumped["payment_id"] == str(payment_id)
    assert dumped["event_type"] == PAYMENT_CREATED_EVENT == "payment.created"
    assert datetime.fromisoformat(dumped["occurred_at"]) == OCCURRED_AT


def test_envelope_decodes_body_and_headers() -> None:
    body = {
        "event_id": str(uuid4()),
        "event_type": "payment.created",
        "payment_id": str(uuid4()),
        "occurred_at": OCCURRED_AT.isoformat(),
    }
    envelope = MessageEnvelope.from_body(body, attempt_no=2, redelivery=True)
    assert isinstance(envelope.event_id, UUID)
    assert envelope.attempt_no == 2
    assert envelope.redelivery is True
    assert envelope.occurred_at == OCCURRED_AT


def test_envelope_requires_core_fields() -> None:
    with pytest.raises(KeyError):
        MessageEnvelope.from_body({"event_type": "payment.created"}, attempt_no=1, redelivery=False)
