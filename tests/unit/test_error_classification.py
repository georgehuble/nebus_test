"""Business decline versus technical error versus delivery failure."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from app.application.processing import MessageResult
from app.domain.models import PaymentStatus, WebhookStatus
from app.errors import GatewayTechnicalError
from app.infrastructure.repositories import AttemptClaim
from app.settings import Settings

from tests.unit.fakes import (
    BASE_TIME,
    RecordingGateway,
    RecordingWebhookSender,
    make_outbox_record,
    make_payment_record,
)
from tests.unit.scenario import build_scenario

pytestmark = pytest.mark.unit


def _pending_and_terminal(status: PaymentStatus, webhook_status: WebhookStatus) -> list[object]:
    pending = make_payment_record()
    terminal = make_payment_record(
        id=pending.id,
        status=status,
        processed_at=BASE_TIME,
        webhook_status=webhook_status,
    )
    return [pending, pending, terminal]


async def test_business_decline_is_terminal_and_delivered(settings_factory: Callable[..., Settings]) -> None:
    gateway = RecordingGateway(status=PaymentStatus.FAILED)
    record = make_outbox_record()
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=_pending_and_terminal(PaymentStatus.FAILED, WebhookStatus.PENDING),
        claim=AttemptClaim(attempt_no=1, attempt_epoch=1),
        gateway=gateway,
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    kwargs = scenario.payments.mark_terminal.await_args.kwargs
    assert kwargs["status"] is PaymentStatus.FAILED
    assert len(scenario.webhooks.sent) == 1
    assert scenario.webhooks.sent[0][1].status is PaymentStatus.FAILED
    scenario.outbox_repo.schedule_retry.assert_not_awaited()
    assert scenario.publisher.published == []
    assert len(gateway.calls) == 1


async def test_webhook_failure_retries_without_changing_business_status(
    settings_factory: Callable[..., Settings],
) -> None:
    webhooks = RecordingWebhookSender(failures=1)
    record = make_outbox_record()
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=_pending_and_terminal(PaymentStatus.SUCCEEDED, WebhookStatus.PENDING),
        claim=AttemptClaim(attempt_no=1, attempt_epoch=1),
        webhooks=webhooks,
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    scenario.payments.mark_terminal.assert_awaited_once()
    scenario.payments.record_delivery_failure.assert_awaited_once()
    failure_kwargs = scenario.payments.record_delivery_failure.await_args.kwargs
    assert failure_kwargs["final"] is False
    scenario.outbox_repo.schedule_retry.assert_awaited_once()


async def test_gateway_technical_error_retries_without_webhook(settings_factory: Callable[..., Settings]) -> None:
    payment = make_payment_record()
    gateway = RecordingGateway(error=GatewayTechnicalError("boom"))
    record = make_outbox_record(payment_id=payment.id)
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=[payment, payment],
        claim=AttemptClaim(attempt_no=1, attempt_epoch=1),
        gateway=gateway,
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    assert scenario.webhooks.sent == []
    scenario.payments.mark_terminal.assert_not_awaited()
    scenario.payments.record_delivery_failure.assert_not_awaited()
    scenario.outbox_repo.schedule_retry.assert_awaited_once()


async def test_delivered_work_is_not_reprocessed_or_dead_lettered(
    settings_factory: Callable[..., Settings],
) -> None:
    terminal = make_payment_record(
        status=PaymentStatus.SUCCEEDED,
        processed_at=BASE_TIME,
        webhook_status=WebhookStatus.DELIVERED,
    )
    record = make_outbox_record(payment_id=terminal.id)
    scenario = build_scenario(
        settings_factory(),
        record=record,
        payment_sequence=[terminal, terminal],
        claim=AttemptClaim(attempt_no=1, attempt_epoch=1),
    )
    result = await scenario.processor.process(scenario.envelope())
    assert result is MessageResult.ACK
    assert scenario.gateway.calls == []
    assert scenario.webhooks.sent == []
    assert scenario.publisher.published == []
    scenario.outbox_repo.schedule_retry.assert_not_awaited()
    scenario.outbox_repo.complete.assert_awaited()
