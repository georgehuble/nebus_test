"""Message envelope published through the transactional Outbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

PaymentCreatedEventType = Literal["payment.created"]

PAYMENT_CREATED_EVENT: PaymentCreatedEventType = "payment.created"


class PaymentCreatedEvent(BaseModel):
    """Payload of the ``payment.created`` outbox event."""

    event_id: UUID
    event_type: PaymentCreatedEventType = PAYMENT_CREATED_EVENT
    payment_id: UUID
    occurred_at: datetime


class MessageEnvelope(BaseModel):
    """A broker message decoded from JSON, including its attempt header."""

    event_id: UUID
    event_type: str
    payment_id: UUID
    occurred_at: datetime
    attempt_no: int
    redelivery: bool = False

    @classmethod
    def from_body(cls, body: dict[str, Any], *, attempt_no: int, redelivery: bool) -> MessageEnvelope:
        """Parse a decoded JSON body together with transport headers."""
        return cls(
            event_id=UUID(str(body["event_id"])),
            event_type=str(body["event_type"]),
            payment_id=UUID(str(body["payment_id"])),
            occurred_at=body["occurred_at"],
            attempt_no=attempt_no,
            redelivery=redelivery,
        )
