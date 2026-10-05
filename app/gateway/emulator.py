"""Deterministic emulator of an external payment gateway."""

from __future__ import annotations

import random
from decimal import Decimal
from uuid import UUID

from app.domain.models import GatewayResult, PaymentStatus
from app.protocols import RandomSource, Sleeper


class EmulatedPaymentGateway:
    """Simulates a gateway call with a deterministic per-payment outcome.

    The outcome (success with ~90% probability, business decline with ~10%) and
    the simulated duration in the ``[min_delay, max_delay]`` window are derived
    from a PRNG seeded with ``payment_id``, so a legitimate re-invocation after
    an indeterminate crash returns the same result. Determinism stabilizes the
    result but does not prevent a repeated invocation; prevention is the job of
    the attempt claim and the terminal-status check.
    """

    def __init__(
        self,
        *,
        random_source: RandomSource,
        sleeper: Sleeper,
        min_delay_seconds: float = 2.0,
        max_delay_seconds: float = 5.0,
        decline_probability: float = 0.10,
    ) -> None:
        if min_delay_seconds < 0 or max_delay_seconds < min_delay_seconds:
            msg = "invalid gateway delay bounds"
            raise ValueError(msg)
        if not 0.0 <= decline_probability <= 1.0:
            msg = "decline_probability must be within [0, 1]"
            raise ValueError(msg)
        self._random = random_source
        self._sleeper = sleeper
        self._min_delay = min_delay_seconds
        self._max_delay = max_delay_seconds
        self._decline_probability = decline_probability

    def _derive(self, payment_id: UUID) -> tuple[PaymentStatus, float]:
        generator = random.Random(int(payment_id))
        roll = generator.random()
        fraction = generator.random()
        delay = self._min_delay + fraction * (self._max_delay - self._min_delay)
        status = PaymentStatus.FAILED if roll < self._decline_probability else PaymentStatus.SUCCEEDED
        return status, delay

    async def authorize(self, payment_id: UUID, amount: Decimal, currency: str) -> GatewayResult:
        """Emulate a gateway authorization for a payment."""
        self._random.seed(int(payment_id))
        status, delay = self._derive(payment_id)
        await self._sleeper.sleep(delay)
        return GatewayResult(status=status, duration_seconds=delay)
