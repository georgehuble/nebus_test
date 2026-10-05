"""Replaceable dependency protocols.

Each protocol is deliberately narrow so tests can substitute deterministic
doubles (randomness, clock, sleeper, gateway, webhook sender, publisher)
without changing the application scenarios.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from app.domain.models import GatewayResult
from app.domain.webhook import WebhookPayload


@runtime_checkable
class RandomSource(Protocol):
    """Source of pseudo-random values in the ``[0.0, 1.0)`` range."""

    def random(self) -> float:
        """Return the next random value."""
        ...

    def seed(self, seed: int) -> None:
        """Deterministically reseed the underlying generator."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Source of the current time."""

    def now(self) -> datetime:
        """Return a timezone-aware UTC timestamp."""
        ...


@runtime_checkable
class Sleeper(Protocol):
    """Asynchronous waiting mechanism."""

    async def sleep(self, seconds: float) -> None:
        """Wait for ``seconds`` without blocking the event loop."""
        ...


@runtime_checkable
class PaymentGateway(Protocol):
    """Emulated external payment gateway."""

    async def authorize(self, payment_id: UUID, amount: Decimal, currency: str) -> GatewayResult:
        """Authorize a payment, raising on a technical failure."""
        ...


@runtime_checkable
class WebhookSender(Protocol):
    """HTTP client used to deliver webhook notifications."""

    async def send(self, url: str, payload: WebhookPayload) -> None:
        """Deliver ``payload``; raise on a retryable failure."""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Publisher that confirms routing to a durable queue."""

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        headers: dict[str, Any],
        *,
        dead_letter: bool = False,
    ) -> None:
        """Publish a persistent message and wait for a positive confirmation.

        When ``dead_letter`` is true the message is routed through the
        dead-letter exchange instead of the main payments exchange.
        """
        ...
