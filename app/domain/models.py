"""Core domain value objects and payment lifecycle rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.errors import GatewayTechnicalError


class PaymentStatus(StrEnum):
    """Business status of a payment."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Whether no further business transition is allowed."""
        return self in TERMINAL_STATUSES


class WebhookStatus(StrEnum):
    """Delivery state of the webhook, kept separate from the business status."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class AttemptState(StrEnum):
    """Durable processing-attempt state of an outbox record."""

    IDLE = "idle"
    OPEN = "open"
    AWAITING_RETRY = "awaiting_retry"
    COMPLETED = "completed"


TERMINAL_STATUSES: frozenset[PaymentStatus] = frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED})

ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: TERMINAL_STATUSES,
    PaymentStatus.SUCCEEDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
}


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    """Return ``True`` when ``current -> target`` is an allowed transition."""
    return target in ALLOWED_TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """Outcome of a single gateway invocation.

    ``status`` is always terminal (``succeeded`` or ``failed``). A business
    decline is a successful *call* that returns ``failed``; technical failures
    are raised as :class:`~app.errors.GatewayTechnicalError`.
    """

    status: PaymentStatus
    duration_seconds: float

    def __post_init__(self) -> None:
        if not self.status.is_terminal:
            raise GatewayTechnicalError("gateway result status must be terminal")
