"""Consumer orchestration order and claim/status separation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from app.application.processing import MessageResult
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.infrastructure import messaging
from app.infrastructure.repositories import AttemptClaim
from app.settings import Settings

from tests.unit.fakes import (
    BASE_TIME,
    FakeClock,
    make_outbox_record,
    make_payment_record,
)
from tests.unit.scenario import build_scenario

pytestmark = pytest.mark.unit


async def test_consumer_completes_scenario_in_order(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    terminal = make_payment_record(
        id=payment.id,
        status=PaymentStatus.SUCCEEDED,
        processed_at=BASE_TIME,
        webhook_status=WebhookStatus.PENDING,
    )
    record = make_outbox_record(payment_id=payment.id)
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=[payment, payment, terminal],
        claim=AttemptClaim(attempt_no=1, attempt_epoch=1),
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    assert scenario.order == ["claim", "gateway", "persist_result", "webhook", "persist_delivery", "ack"]
    assert len(scenario.gateway.calls) == 1
    assert len(scenario.webhooks.sent) == 1
    assert scenario.publisher.published == []
    assert scenario.payments.mark_terminal.await_count == 1


async def test_completed_work_is_acknowledged_without_gateway(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    record = make_outbox_record(payment_id=payment.id, attempt_state=AttemptState.COMPLETED)
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=[payment],
        claim=None,
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    assert scenario.gateway.calls == []
    assert scenario.outbox_repo.claim_attempt.await_count == 0


async def test_active_foreign_lease_publishes_durable_copy_before_ack(
    settings_factory: Callable[..., Settings],
) -> None:
    payment = make_payment_record()
    clock = FakeClock()
    record = make_outbox_record(
        payment_id=payment.id,
        attempt_no=1,
        attempt_state=AttemptState.OPEN,
        attempt_owner="other-owner",
        attempt_lease_expires_at=clock.now() + timedelta(seconds=60),
    )
    scenario = build_scenario(settings_factory(), record=record, payment_sequence=[payment], claim=None)
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    assert scenario.gateway.calls == []
    assert scenario.webhooks.sent == []
    assert len(scenario.publisher.published) == 1
    routing_key, _, headers = scenario.publisher.published[0]
    assert routing_key == messaging.PAYMENTS_ROUTING_KEY
    assert headers[messaging.HEADER_REDELIVERY] is True
    assert scenario.outbox_repo.claim_attempt.await_count == 0
    assert scenario.outbox_repo.takeover.await_count == 0


async def test_terminal_result_skips_gateway_and_resumes_delivery(
    settings_factory: Callable[..., Settings],
) -> None:
    payment = make_payment_record()
    terminal = make_payment_record(
        id=payment.id,
        status=PaymentStatus.SUCCEEDED,
        processed_at=BASE_TIME,
        webhook_status=WebhookStatus.PENDING,
    )
    record = make_outbox_record(
        payment_id=payment.id,
        attempt_no=1,
        attempt_state=AttemptState.AWAITING_RETRY,
        next_attempt_no=2,
        next_attempt_at=BASE_TIME - timedelta(seconds=1),
    )
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=[terminal, terminal],
        claim=AttemptClaim(attempt_no=2, attempt_epoch=3),
    )
    result = await scenario.processor.process(scenario.envelope(attempt_no=2))
    assert result is MessageResult.ACK
    assert scenario.gateway.calls == []
    assert len(scenario.webhooks.sent) == 1
    assert scenario.payments.mark_terminal.await_count == 0
