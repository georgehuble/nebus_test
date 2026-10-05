"""Deterministic gateway emulator behaviour."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from app.domain.models import PaymentStatus
from app.gateway.emulator import EmulatedPaymentGateway
from app.infrastructure.system import DefaultRandomSource

from tests.unit.fakes import InstantSleeper

pytestmark = pytest.mark.unit


def _gateway(*, decline_probability: float) -> tuple[EmulatedPaymentGateway, InstantSleeper]:
    sleeper = InstantSleeper()
    gateway = EmulatedPaymentGateway(
        random_source=DefaultRandomSource(),
        sleeper=sleeper,
        min_delay_seconds=2.0,
        max_delay_seconds=5.0,
        decline_probability=decline_probability,
    )
    return gateway, sleeper


async def test_success_branch() -> None:
    gateway, sleeper = _gateway(decline_probability=0.0)
    result = await gateway.authorize(UUID(int=1), Decimal("10.00"), "RUB")
    assert result.status is PaymentStatus.SUCCEEDED
    assert len(sleeper.calls) == 1


async def test_decline_branch_is_terminal() -> None:
    gateway, _ = _gateway(decline_probability=1.0)
    result = await gateway.authorize(UUID(int=1), Decimal("10.00"), "RUB")
    assert result.status is PaymentStatus.FAILED


async def test_duration_stays_within_bounds() -> None:
    gateway, sleeper = _gateway(decline_probability=0.5)
    for _ in range(50):
        result = await gateway.authorize(uuid4(), Decimal("1.00"), "USD")
        assert 2.0 <= result.duration_seconds <= 5.0
    assert all(2.0 <= call <= 5.0 for call in sleeper.calls)


async def test_reinvocation_returns_the_same_result() -> None:
    gateway, _ = _gateway(decline_probability=0.5)
    payment_id = uuid4()
    first = await gateway.authorize(payment_id, Decimal("1.00"), "EUR")
    second = await gateway.authorize(payment_id, Decimal("1.00"), "EUR")
    assert first.status is second.status
    assert first.duration_seconds == second.duration_seconds


async def test_waiting_mechanism_is_injected() -> None:
    gateway, sleeper = _gateway(decline_probability=0.0)
    result = await gateway.authorize(uuid4(), Decimal("1.00"), "RUB")
    assert sleeper.calls == [result.duration_seconds]


def test_invalid_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        EmulatedPaymentGateway(
            random_source=DefaultRandomSource(),
            sleeper=InstantSleeper(),
            min_delay_seconds=5.0,
            max_delay_seconds=2.0,
        )


def test_invalid_probability_is_rejected() -> None:
    with pytest.raises(ValueError):
        EmulatedPaymentGateway(
            random_source=DefaultRandomSource(),
            sleeper=InstantSleeper(),
            decline_probability=1.5,
        )
