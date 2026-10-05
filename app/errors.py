"""Application-wide exception hierarchy.

These exceptions are shared across layers. They carry no infrastructure
dependencies so that both the domain and the application layer can raise them.
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class for all application errors."""


class TechnicalError(ApplicationError):
    """A retryable technical failure (network, timeout, database, broker)."""


class GatewayTechnicalError(TechnicalError):
    """The gateway failed technically (as opposed to a business decline)."""


class WebhookDeliveryError(TechnicalError):
    """A webhook delivery failed technically and may be retried."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PublishError(TechnicalError):
    """A message could not be published with a positive broker confirmation."""


class IdempotencyConflict(ApplicationError):
    """The idempotency key was reused with a different normalized body."""


class PaymentNotFound(ApplicationError):
    """No payment exists for the requested identifier."""
