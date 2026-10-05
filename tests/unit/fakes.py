"""Typed deterministic doubles used by the unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.domain.events import MessageEnvelope
from app.domain.models import AttemptState, GatewayResult, PaymentStatus, WebhookStatus
from app.domain.webhook import WebhookPayload
from app.errors import GatewayTechnicalError, PublishError, WebhookDeliveryError
from app.infrastructure.repositories import OutboxRecord, PaymentRecord

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Manually advanced clock."""

    def __init__(self, current: datetime = BASE_TIME) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class InstantSleeper:
    """Sleeper that records durations instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


class RecordingGateway:
    """Gateway double returning a configured result or raising a configured error."""

    def __init__(
        self,
        *,
        status: PaymentStatus = PaymentStatus.SUCCEEDED,
        error: Exception | None = None,
        duration_seconds: float = 2.5,
        events: list[str] | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.duration_seconds = duration_seconds
        self.calls: list[UUID] = []
        self.events = events

    async def authorize(self, payment_id: UUID, amount: Decimal, currency: str) -> GatewayResult:
        if self.events is not None:
            self.events.append("gateway")
        self.calls.append(payment_id)
        if self.error is not None:
            raise self.error
        return GatewayResult(status=self.status, duration_seconds=self.duration_seconds)


class RecordingWebhookSender:
    """Webhook sender double that can fail a number of times."""

    def __init__(self, *, failures: int = 0, events: list[str] | None = None) -> None:
        self.remaining_failures = failures
        self.sent: list[tuple[str, WebhookPayload]] = []
        self.events = events

    async def send(self, url: str, payload: WebhookPayload) -> None:
        if self.events is not None:
            self.events.append("webhook")
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise WebhookDeliveryError("simulated webhook failure", status_code=500)
        self.sent.append((url, payload))


class RecordingPublisher:
    """Event publisher double capturing routing key, payload and headers."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.dead_letters: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        headers: dict[str, Any],
        *,
        dead_letter: bool = False,
    ) -> None:
        if self.fail:
            raise PublishError("simulated publish failure")
        if dead_letter:
            self.dead_letters.append((routing_key, payload, headers))
        else:
            self.published.append((routing_key, payload, headers))


def make_payment_record(**overrides: Any) -> PaymentRecord:
    """Build a payment snapshot with sensible pending defaults."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "idempotency_key": "key-1",
        "request_fingerprint": "a" * 64,
        "amount": Decimal("10.50"),
        "currency": "RUB",
        "description": "",
        "metadata": {},
        "status": PaymentStatus.PENDING,
        "webhook_url": "http://receiver.test/webhook",
        "created_at": BASE_TIME,
        "processed_at": None,
        "webhook_event_id": None,
        "webhook_status": WebhookStatus.PENDING,
        "webhook_attempts": 0,
        "webhook_last_error": None,
    }
    defaults.update(overrides)
    return PaymentRecord(**defaults)


def make_outbox_record(**overrides: Any) -> OutboxRecord:
    """Build an outbox snapshot with sensible idle defaults."""
    payment_id = overrides.get("payment_id", uuid4())
    event_id = overrides.get("id", uuid4())
    defaults: dict[str, Any] = {
        "id": event_id,
        "payment_id": payment_id,
        "event_type": "payment.created",
        "payload": {
            "event_id": str(event_id),
            "event_type": "payment.created",
            "payment_id": str(payment_id),
            "occurred_at": BASE_TIME.isoformat(),
        },
        "created_at": BASE_TIME,
        "attempt_no": 0,
        "next_attempt_no": None,
        "attempt_state": AttemptState.IDLE,
        "attempt_epoch": 0,
        "attempt_owner": None,
        "attempt_lease_expires_at": None,
        "next_attempt_at": None,
        "recoveries": 0,
        "last_error": None,
        "published_at": None,
    }
    defaults.update(overrides)
    return OutboxRecord(**defaults)


def make_envelope(
    outbox: OutboxRecord,
    *,
    attempt_no: int = 1,
    redelivery: bool = False,
) -> MessageEnvelope:
    """Build a message envelope bound to an outbox record."""
    return MessageEnvelope(
        event_id=outbox.id,
        event_type=outbox.event_type,
        payment_id=outbox.payment_id,
        occurred_at=BASE_TIME,
        attempt_no=attempt_no,
        redelivery=redelivery,
    )


__all__ = [
    "FakeClock",
    "GatewayTechnicalError",
    "InstantSleeper",
    "RecordingGateway",
    "RecordingPublisher",
    "RecordingWebhookSender",
    "make_envelope",
    "make_outbox_record",
    "make_payment_record",
]
