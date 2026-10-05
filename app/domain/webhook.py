"""Webhook payload contract and deterministic event identifier."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from pydantic import BaseModel

from app.domain.models import PaymentStatus

# Fixed, project-wide namespace constant. It is intentionally hard-coded so the
# event identifier for a given (payment, status) pair is stable forever.
WEBHOOK_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


class WebhookPayload(BaseModel):
    """JSON body delivered to the client's webhook receiver."""

    event_id: UUID
    payment_id: UUID
    status: PaymentStatus
    processed_at: datetime


def webhook_event_id(payment_id: UUID, status: PaymentStatus) -> UUID:
    """Compute the stable UUIDv5 for one logical webhook.

    The canonical name is ``webhook:<payment_id>:<status>`` where the identifier
    uses its canonical lowercase hyphenated form and the status is lowercase.
    """
    name = f"webhook:{payment_id}:{status.value}"
    return uuid5(WEBHOOK_NAMESPACE, name)


def build_webhook_payload(
    *,
    event_id: UUID,
    payment_id: UUID,
    status: PaymentStatus,
    processed_at: datetime,
) -> WebhookPayload:
    """Assemble the webhook payload, preferring a persisted ``event_id``."""
    return WebhookPayload(
        event_id=event_id,
        payment_id=payment_id,
        status=status,
        processed_at=processed_at,
    )
