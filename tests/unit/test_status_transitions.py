"""Payment status machine rules."""

from __future__ import annotations

import pytest
from app.domain.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    GatewayResult,
    PaymentStatus,
    WebhookStatus,
    can_transition,
)
from app.errors import GatewayTechnicalError

pytestmark = pytest.mark.unit


def test_pending_can_transition_to_terminal_statuses() -> None:
    assert can_transition(PaymentStatus.PENDING, PaymentStatus.SUCCEEDED)
    assert can_transition(PaymentStatus.PENDING, PaymentStatus.FAILED)


def test_pending_cannot_stay_pending() -> None:
    assert not can_transition(PaymentStatus.PENDING, PaymentStatus.PENDING)


def test_terminal_statuses_are_immutable() -> None:
    for terminal in TERMINAL_STATUSES:
        assert terminal.is_terminal
        for target in PaymentStatus:
            assert not can_transition(terminal, target)


def test_only_three_business_statuses_exist() -> None:
    assert {status.value for status in PaymentStatus} == {"pending", "succeeded", "failed"}


def test_allowed_transitions_cover_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(PaymentStatus)


def test_webhook_status_is_separate_vocabulary() -> None:
    assert {status.value for status in WebhookStatus} == {"pending", "delivered", "failed"}


def test_gateway_result_accepts_terminal_status() -> None:
    result = GatewayResult(status=PaymentStatus.SUCCEEDED, duration_seconds=3.0)
    assert result.status is PaymentStatus.SUCCEEDED


def test_gateway_result_rejects_non_terminal_status() -> None:
    with pytest.raises(GatewayTechnicalError):
        GatewayResult(status=PaymentStatus.PENDING, duration_seconds=3.0)
