"""Payment use cases: idempotent creation and read."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.application.fingerprint import request_fingerprint
from app.domain.dto import PaymentCreateRequest
from app.domain.models import PaymentStatus
from app.errors import IdempotencyConflict
from app.infrastructure.repositories import PaymentRecord, PaymentRepository


@dataclass(frozen=True, slots=True)
class AcceptedPayment:
    """Result of an accepted (created or deduplicated) creation request."""

    payment_id: UUID
    status: PaymentStatus
    created_at: datetime


class PaymentService:
    """Orchestrates payment creation with idempotency and payment retrieval."""

    def __init__(self, payments: PaymentRepository) -> None:
        self._payments = payments

    async def create(self, *, idempotency_key: str, request: PaymentCreateRequest) -> AcceptedPayment:
        """Create a payment or deduplicate/conflict on the idempotency key."""
        fingerprint = request_fingerprint(request)
        result = await self._payments.create(
            idempotency_key=idempotency_key,
            request=request,
            fingerprint=fingerprint,
        )
        if not result.created and result.payment.request_fingerprint != fingerprint:
            raise IdempotencyConflict
        payment = result.payment
        return AcceptedPayment(
            payment_id=payment.id,
            status=payment.status,
            created_at=payment.created_at,
        )

    async def get(self, payment_id: UUID) -> PaymentRecord | None:
        """Return a payment snapshot or ``None`` when it does not exist."""
        return await self._payments.get_by_id(payment_id)
