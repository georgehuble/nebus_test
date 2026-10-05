"""Webhook payload contract and stable UUIDv5 event identifier."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest
from app.domain.models import PaymentStatus
from app.domain.webhook import WEBHOOK_NAMESPACE, build_webhook_payload, webhook_event_id

pytestmark = pytest.mark.unit

PROCESSED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_event_id_is_uuid5_over_fixed_namespace_and_canonical_name() -> None:
    payment_id = UUID("11111111-2222-3333-4444-555555555555")
    expected = uuid5(WEBHOOK_NAMESPACE, f"webhook:{payment_id}:succeeded")
    assert webhook_event_id(payment_id, PaymentStatus.SUCCEEDED) == expected


def test_event_id_is_deterministic_across_calls() -> None:
    payment_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert webhook_event_id(payment_id, PaymentStatus.FAILED) == webhook_event_id(payment_id, PaymentStatus.FAILED)


def test_event_id_differs_per_status_and_payment() -> None:
    payment_id = UUID("11111111-1111-1111-1111-111111111111")
    other = UUID("22222222-2222-2222-2222-222222222222")
    assert webhook_event_id(payment_id, PaymentStatus.SUCCEEDED) != webhook_event_id(payment_id, PaymentStatus.FAILED)
    assert webhook_event_id(payment_id, PaymentStatus.SUCCEEDED) != webhook_event_id(other, PaymentStatus.SUCCEEDED)


def test_build_payload_contains_documented_fields() -> None:
    payment_id = UUID("11111111-1111-1111-1111-111111111111")
    event_id = webhook_event_id(payment_id, PaymentStatus.SUCCEEDED)
    payload = build_webhook_payload(
        event_id=event_id,
        payment_id=payment_id,
        status=PaymentStatus.SUCCEEDED,
        processed_at=PROCESSED_AT,
    )
    dumped = payload.model_dump(mode="json")
    assert set(dumped) == {"event_id", "payment_id", "status", "processed_at"}
    assert dumped["event_id"] == str(event_id)
    assert dumped["payment_id"] == str(payment_id)
    assert dumped["status"] == "succeeded"
    parsed = datetime.fromisoformat(dumped["processed_at"])
    assert parsed.tzinfo is not None
    assert parsed == PROCESSED_AT


def test_build_payload_reuses_persisted_event_id() -> None:
    payment_id = UUID("11111111-1111-1111-1111-111111111111")
    persisted = webhook_event_id(payment_id, PaymentStatus.FAILED)
    payload = build_webhook_payload(
        event_id=persisted,
        payment_id=payment_id,
        status=PaymentStatus.FAILED,
        processed_at=PROCESSED_AT,
    )
    assert payload.event_id == persisted
