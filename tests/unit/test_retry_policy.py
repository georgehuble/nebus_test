"""Bounded retry accounting, attempt matching and dead-lettering."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from app.application.processing import MessageResult
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.errors import GatewayTechnicalError
from app.infrastructure import messaging
from app.infrastructure.repositories import AttemptClaim, OutboxRecord
from app.settings import Settings

from tests.unit.fakes import (
    BASE_TIME,
    FakeClock,
    RecordingGateway,
    make_outbox_record,
    make_payment_record,
)
from tests.unit.scenario import build_scenario

pytestmark = pytest.mark.unit


async def test_first_failure_schedules_retry_with_delay(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    record = make_outbox_record(payment_id=payment.id)
    gateway = RecordingGateway(error=GatewayTechnicalError("gateway boom"))
    scenario = build_scenario(
        settings_factory(retry_base_delay_seconds=1.0),
        record=record,
        payment_sequence=[payment, payment],
        claim=AttemptClaim(attempt_no=1, attempt_epoch=1),
        gateway=gateway,
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    scenario.outbox_repo.schedule_retry.assert_awaited_once()
    kwargs = scenario.outbox_repo.schedule_retry.await_args.kwargs
    assert kwargs["next_attempt_no"] == 2
    assert kwargs["next_attempt_at"] == scenario.clock.now() + timedelta(seconds=1)
    assert "gateway boom" in kwargs["error"]
    assert scenario.publisher.published == []
    assert scenario.webhooks.sent == []


def _due_retry(payment_id: UUID, *, attempt_no: int, next_attempt_no: int) -> OutboxRecord:
    return make_outbox_record(
        payment_id=payment_id,
        attempt_no=attempt_no,
        attempt_state=AttemptState.AWAITING_RETRY,
        next_attempt_no=next_attempt_no,
        next_attempt_at=BASE_TIME - timedelta(seconds=1),
    )


async def test_second_delay_is_exponential(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    record = _due_retry(payment.id, attempt_no=1, next_attempt_no=2)
    gateway = RecordingGateway(error=GatewayTechnicalError("boom"))
    scenario = build_scenario(
        settings_factory(retry_base_delay_seconds=1.0),
        record=record,
        payment_sequence=[payment, payment],
        claim=AttemptClaim(attempt_no=2, attempt_epoch=2),
        gateway=gateway,
    )
    await scenario.processor.process(scenario.envelope(attempt_no=2))
    kwargs = scenario.outbox_repo.schedule_retry.await_args.kwargs
    assert kwargs["next_attempt_no"] == 3
    assert kwargs["next_attempt_at"] == scenario.clock.now() + timedelta(seconds=2)


async def test_exhausted_budget_dead_letters_unfinished_work(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    record = _due_retry(payment.id, attempt_no=2, next_attempt_no=3)
    gateway = RecordingGateway(error=GatewayTechnicalError("boom"))
    scenario = build_scenario(
        settings_factory(max_attempts=3),
        record=record,
        payment_sequence=[payment, payment],
        claim=AttemptClaim(attempt_no=3, attempt_epoch=4),
        gateway=gateway,
    )
    result = await scenario.processor.process(scenario.envelope(attempt_no=3))
    assert result is MessageResult.ACK
    assert len(scenario.publisher.dead_letters) == 1
    routing_key, payload, _ = scenario.publisher.dead_letters[0]
    assert routing_key == messaging.DLQ_ROUTING_KEY
    assert payload["attempt_no"] == 3
    assert "boom" in payload["reason"]
    assert payload["payment_id"] == str(payment.id)
    scenario.outbox_repo.schedule_retry.assert_not_awaited()
    scenario.outbox_repo.complete.assert_awaited()


async def test_stale_duplicate_in_awaiting_retry_is_not_processed(
    settings_factory: Callable[..., Settings],
) -> None:
    payment = make_payment_record()
    record = make_outbox_record(
        payment_id=payment.id,
        attempt_no=1,
        attempt_state=AttemptState.AWAITING_RETRY,
        next_attempt_no=2,
        next_attempt_at=BASE_TIME - timedelta(seconds=1),
    )
    scenario = build_scenario(settings_factory(), record=record, payment_sequence=[payment], claim=None)
    result = await scenario.processor.process(scenario.envelope(attempt_no=1))
    assert result is MessageResult.ACK
    assert scenario.outbox_repo.claim_attempt.await_count == 0
    assert scenario.gateway.calls == []


async def test_early_retry_delivery_preserves_scheduled_delay(
    settings_factory: Callable[..., Settings],
) -> None:
    payment = make_payment_record()
    clock = FakeClock()
    record = make_outbox_record(
        payment_id=payment.id,
        attempt_no=1,
        attempt_state=AttemptState.AWAITING_RETRY,
        next_attempt_no=2,
        next_attempt_at=clock.now() + timedelta(seconds=30),
    )
    scenario = build_scenario(settings_factory(), record=record, payment_sequence=[payment], claim=None)
    result = await scenario.processor.process(scenario.envelope(attempt_no=2))
    assert result is MessageResult.ACK
    assert scenario.outbox_repo.claim_attempt.await_count == 0
    assert scenario.gateway.calls == []


async def test_matching_retry_attempt_is_claimed(settings_factory: Callable[..., Settings]) -> None:
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
    scenario.outbox_repo.claim_attempt.assert_awaited_once()
    assert scenario.outbox_repo.claim_attempt.await_args.kwargs["message_attempt_no"] == 2


async def test_expired_lease_is_recovered_by_takeover_without_new_attempt(
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
        attempt_state=AttemptState.OPEN,
        attempt_owner="dead-owner",
        attempt_lease_expires_at=BASE_TIME - timedelta(seconds=1),
    )
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=[terminal, terminal],
        claim=None,
    )
    scenario.outbox_repo.takeover.return_value = AttemptClaim(attempt_no=1, attempt_epoch=2)
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    scenario.outbox_repo.takeover.assert_awaited_once()
    scenario.outbox_repo.claim_attempt.assert_not_awaited()
    assert scenario.gateway.calls == []


async def test_exceeded_recovery_budget_dead_letters_unfinished_work(
    settings_factory: Callable[..., Settings],
) -> None:
    payment = make_payment_record()
    record = make_outbox_record(
        payment_id=payment.id,
        attempt_no=1,
        attempt_state=AttemptState.OPEN,
        attempt_owner="dead-owner",
        attempt_lease_expires_at=BASE_TIME - timedelta(seconds=1),
    )
    scenario = build_scenario(settings_factory(), record=record, payment_sequence=[payment], claim=None)
    scenario.outbox_repo.takeover.return_value = None
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    assert len(scenario.publisher.dead_letters) == 1
    routing_key, _, _ = scenario.publisher.dead_letters[0]
    assert routing_key == messaging.DLQ_ROUTING_KEY
    scenario.outbox_repo.complete.assert_awaited()


async def test_unconfirmed_dead_letter_publication_requeues(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    record = _due_retry(payment.id, attempt_no=2, next_attempt_no=3)
    gateway = RecordingGateway(error=GatewayTechnicalError("boom"))
    scenario = build_scenario(
        settings_factory(max_attempts=3),
        record=record,
        payment_sequence=[payment, payment],
        claim=AttemptClaim(attempt_no=3, attempt_epoch=4),
        gateway=gateway,
    )
    scenario.publisher.fail = True
    result = await scenario.processor.process(scenario.envelope(attempt_no=3))
    assert result is MessageResult.REQUEUE
