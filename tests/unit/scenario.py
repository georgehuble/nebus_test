"""Reusable orchestrator scenario wiring deterministic doubles and mocked repositories."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

from app.application.processing import PaymentProcessor
from app.domain.events import MessageEnvelope
from app.infrastructure.repositories import AttemptClaim, OutboxRecord, OutboxRepository, PaymentRepository
from app.settings import Settings

from tests.unit.fakes import (
    FakeClock,
    RecordingGateway,
    RecordingPublisher,
    RecordingWebhookSender,
    make_envelope,
)


@dataclass
class Scenario:
    """A fully wired processor plus its observable doubles."""

    processor: PaymentProcessor
    order: list[str]
    gateway: RecordingGateway
    webhooks: RecordingWebhookSender
    publisher: RecordingPublisher
    payments: AsyncMock
    outbox_repo: AsyncMock
    record: OutboxRecord
    clock: FakeClock

    def envelope(self, attempt_no: int = 1) -> MessageEnvelope:
        """Build an envelope for the scenario's outbox record."""
        return make_envelope(self.record, attempt_no=attempt_no)


def build_scenario(
    settings: Settings,
    *,
    record: OutboxRecord,
    payment_sequence: list[object],
    claim: AttemptClaim | None,
    gateway: RecordingGateway | None = None,
    webhooks: RecordingWebhookSender | None = None,
) -> Scenario:
    """Wire a processor around mocked repositories and recording doubles."""
    order: list[str] = []
    payments = AsyncMock(spec=PaymentRepository)
    payments.get_by_id.side_effect = payment_sequence

    async def mark_terminal(**kwargs: object) -> bool:
        order.append("persist_result")
        return True

    async def record_success(**kwargs: object) -> bool:
        order.append("persist_delivery")
        return True

    payments.mark_terminal.side_effect = mark_terminal
    payments.record_delivery_success.side_effect = record_success

    async def claim_attempt(**kwargs: object) -> AttemptClaim | None:
        order.append("claim")
        return claim

    async def complete(**kwargs: object) -> bool:
        order.append("ack")
        return True

    outbox_repo = AsyncMock(spec=OutboxRepository)
    outbox_repo.fetch.return_value = record
    outbox_repo.claim_attempt.side_effect = claim_attempt
    outbox_repo.complete.side_effect = complete

    resolved_gateway = gateway or RecordingGateway(events=order)
    resolved_webhooks = webhooks or RecordingWebhookSender(events=order)
    publisher = RecordingPublisher()
    clock = FakeClock()
    processor = PaymentProcessor(
        settings=settings,
        payments=payments,
        outbox=outbox_repo,
        gateway=resolved_gateway,
        webhooks=resolved_webhooks,
        publisher=publisher,
        clock=clock,
        owner="test-owner",
    )
    return Scenario(
        processor=processor,
        order=order,
        gateway=resolved_gateway,
        webhooks=resolved_webhooks,
        publisher=publisher,
        payments=payments,
        outbox_repo=outbox_repo,
        record=record,
        clock=clock,
    )


__all__ = ["Scenario", "build_scenario"]
