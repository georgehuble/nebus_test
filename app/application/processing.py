"""Consumer orchestration of the payment processing scenario.

The exact order of a single pass is: claim work (lease) -> start the attempt ->
call the gateway -> persist the terminal result -> deliver the webhook -> persist
delivery state -> acknowledge. Claiming never writes the business status and the
terminal status is only written by the separate conditional update. Every state
write is fenced by the attempt owner/epoch so a slow former owner stops.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from app.domain.events import MessageEnvelope
from app.domain.models import AttemptState, WebhookStatus
from app.domain.webhook import build_webhook_payload, webhook_event_id
from app.errors import PublishError, TechnicalError
from app.infrastructure.messaging import (
    DLQ_ROUTING_KEY,
    HEADER_ATTEMPT,
    HEADER_REDELIVERY,
    PAYMENTS_ROUTING_KEY,
)
from app.infrastructure.repositories import (
    AttemptClaim,
    OutboxRecord,
    OutboxRepository,
    PaymentRecord,
    PaymentRepository,
)
from app.logging_config import BoundLogger, get_logger
from app.protocols import Clock, EventPublisher, PaymentGateway, WebhookSender
from app.settings import Settings


class MessageResult(StrEnum):
    """What the consumer must do with the received delivery."""

    ACK = "ack"
    REQUEUE = "requeue"


def _new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class PaymentProcessor:
    """Runs one processing pass for a delivered message."""

    def __init__(
        self,
        *,
        settings: Settings,
        payments: PaymentRepository,
        outbox: OutboxRepository,
        gateway: PaymentGateway,
        webhooks: WebhookSender,
        publisher: EventPublisher,
        clock: Clock,
        owner: str | None = None,
    ) -> None:
        self._settings = settings
        self._payments = payments
        self._outbox = outbox
        self._gateway = gateway
        self._webhooks = webhooks
        self._publisher = publisher
        self._clock = clock
        self._owner = owner or _new_owner()
        self._log: BoundLogger = get_logger("app.processing")

    @property
    def owner(self) -> str:
        """Identifier of this processor instance used as the attempt owner."""
        return self._owner

    async def process(self, envelope: MessageEnvelope) -> MessageResult:
        """Process one delivered message, returning how to settle it."""
        log = self._log.bind(event_id=envelope.event_id, payment_id=envelope.payment_id, attempt_no=envelope.attempt_no)
        outbox = await self._outbox.fetch(envelope.event_id)
        if outbox is None:
            log.warning("outbox event not found; acknowledging deterministically")
            return MessageResult.ACK
        payment = await self._payments.get_by_id(outbox.payment_id)
        if payment is None:
            log.warning("payment not found; acknowledging deterministically")
            return MessageResult.ACK

        try:
            return await self._dispatch(payment, outbox, envelope, log)
        except PublishError as exc:
            log.warning("publication not confirmed; requeueing: %s", exc)
            return MessageResult.REQUEUE
        except asyncio.CancelledError:
            raise

    async def _dispatch(
        self,
        payment: PaymentRecord,
        outbox: OutboxRecord,
        envelope: MessageEnvelope,
        log: BoundLogger,
    ) -> MessageResult:
        state = outbox.attempt_state
        if state is AttemptState.COMPLETED:
            return MessageResult.ACK

        claim: AttemptClaim | None = None
        if state is AttemptState.IDLE:
            if envelope.attempt_no != 1:
                log.info("stale duplicate for idle attempt; acknowledging")
                return MessageResult.ACK
            claim = await self._outbox.claim_attempt(
                event_id=outbox.id,
                message_attempt_no=1,
                owner=self._owner,
                lease_seconds=self._settings.attempt_lease_seconds,
            )
        elif state is AttemptState.AWAITING_RETRY:
            retry_claim = await self._claim_scheduled_retry(outbox, envelope, log)
            if isinstance(retry_claim, MessageResult):
                return retry_claim
            claim = retry_claim
        else:  # AttemptState.OPEN
            open_claim = await self._handle_open(outbox, envelope, log)
            if isinstance(open_claim, MessageResult):
                return open_claim
            claim = open_claim

        if claim is None:
            return MessageResult.ACK
        return await self._run_attempt(payment, outbox, claim.attempt_no, claim.attempt_epoch, log)

    async def _claim_scheduled_retry(
        self,
        outbox: OutboxRecord,
        envelope: MessageEnvelope,
        log: BoundLogger,
    ) -> AttemptClaim | MessageResult:
        expected = outbox.next_attempt_no
        if expected is None:
            log.warning("awaiting_retry without next_attempt_no; acknowledging")
            return MessageResult.ACK
        if envelope.attempt_no != expected:
            log.info("stale duplicate while retry awaited; acknowledging")
            return MessageResult.ACK
        if outbox.next_attempt_at is not None and outbox.next_attempt_at > self._clock.now():
            log.info("retry delivered before its scheduled time; acknowledging")
            return MessageResult.ACK
        claim = await self._outbox.claim_attempt(
            event_id=outbox.id,
            message_attempt_no=envelope.attempt_no,
            owner=self._owner,
            lease_seconds=self._settings.attempt_lease_seconds,
        )
        if claim is None:
            log.info("retry claim raced with another owner; acknowledging")
            return MessageResult.ACK
        return claim

    async def _handle_open(
        self,
        outbox: OutboxRecord,
        envelope: MessageEnvelope,
        log: BoundLogger,
    ) -> AttemptClaim | MessageResult:
        lease_expires = outbox.attempt_lease_expires_at
        if lease_expires is not None and lease_expires > self._clock.now():
            # Another owner holds a live lease: publish a durable copy first so
            # the work survives if that owner fails, then acknowledge.
            await self._publish_durable_copy(outbox, envelope.attempt_no)
            log.info("active lease held by another owner; durable copy published")
            return MessageResult.ACK
        claim = await self._outbox.takeover(
            event_id=outbox.id,
            owner=self._owner,
            lease_seconds=self._settings.attempt_lease_seconds,
            max_recoveries=self._settings.max_recoveries,
        )
        if claim is None:
            log.warning("recovery budget exceeded; dead-lettering unfinished work")
            return await self._dead_letter(
                outbox,
                payment=None,
                attempt_no=outbox.attempt_no,
                epoch=outbox.attempt_epoch,
                error="recovery budget exceeded",
                delivery_phase=False,
                log=log,
            )
        return claim

    async def _run_attempt(
        self,
        payment: PaymentRecord,
        outbox: OutboxRecord,
        attempt_no: int,
        epoch: int,
        log: BoundLogger,
    ) -> MessageResult:
        log = log.bind(attempt_no=attempt_no)
        heartbeat = asyncio.create_task(self._heartbeat_loop(outbox.id, epoch))
        try:
            current = await self._payments.get_by_id(payment.id)
            if current is None:
                return MessageResult.ACK
            if not current.status.is_terminal:
                outcome = await self._authorize(current, outbox, attempt_no, epoch, log)
                if isinstance(outcome, MessageResult):
                    return outcome
                current = outcome
            return await self._deliver(current, outbox, attempt_no, epoch, log)
        finally:
            heartbeat.cancel()
            with_suppressed = asyncio.gather(heartbeat, return_exceptions=True)
            await with_suppressed

    async def _authorize(
        self,
        payment: PaymentRecord,
        outbox: OutboxRecord,
        attempt_no: int,
        epoch: int,
        log: BoundLogger,
    ) -> PaymentRecord | MessageResult:
        try:
            result = await self._gateway.authorize(payment.id, payment.amount, payment.currency)
        except TechnicalError as exc:
            log.warning("gateway technical error: %s", exc)
            return await self._fail_attempt(outbox, payment, attempt_no, epoch, str(exc), delivery_phase=False, log=log)

        event_id = webhook_event_id(payment.id, result.status)
        processed_at = self._clock.now()
        updated = await self._payments.mark_terminal(
            payment_id=payment.id,
            status=result.status,
            processed_at=processed_at,
            webhook_event_id=event_id,
            owner=self._owner,
            epoch=epoch,
        )
        if not updated:
            refreshed = await self._payments.get_by_id(payment.id)
            if refreshed is None or not refreshed.status.is_terminal:
                log.warning("lost ownership before persisting terminal result")
                return MessageResult.REQUEUE
            return refreshed
        refreshed = await self._payments.get_by_id(payment.id)
        if refreshed is None:
            return MessageResult.REQUEUE
        log.info("terminal business result persisted: %s", refreshed.status.value)
        return refreshed

    async def _deliver(
        self,
        payment: PaymentRecord,
        outbox: OutboxRecord,
        attempt_no: int,
        epoch: int,
        log: BoundLogger,
    ) -> MessageResult:
        if payment.webhook_status is WebhookStatus.DELIVERED:
            closed = await self._outbox.complete(event_id=outbox.id, owner=self._owner, epoch=epoch)
            return MessageResult.ACK if closed else MessageResult.REQUEUE

        event_id = payment.webhook_event_id or webhook_event_id(payment.id, payment.status)
        processed_at = payment.processed_at or self._clock.now()
        payload = build_webhook_payload(
            event_id=event_id,
            payment_id=payment.id,
            status=payment.status,
            processed_at=processed_at,
        )
        try:
            await self._webhooks.send(payment.webhook_url, payload)
        except TechnicalError as exc:
            log.warning("webhook delivery failed: %s", exc)
            return await self._fail_attempt(outbox, payment, attempt_no, epoch, str(exc), delivery_phase=True, log=log)

        recorded = await self._payments.record_delivery_success(
            payment_id=payment.id,
            event_id=event_id,
            owner=self._owner,
            epoch=epoch,
        )
        if not recorded:
            return MessageResult.REQUEUE
        closed = await self._outbox.complete(event_id=outbox.id, owner=self._owner, epoch=epoch)
        if not closed:
            return MessageResult.REQUEUE
        log.info("webhook delivered and attempt completed")
        return MessageResult.ACK

    async def _fail_attempt(
        self,
        outbox: OutboxRecord,
        payment: PaymentRecord,
        attempt_no: int,
        epoch: int,
        error: str,
        *,
        delivery_phase: bool,
        log: BoundLogger,
    ) -> MessageResult:
        if attempt_no >= self._settings.max_attempts:
            return await self._dead_letter(
                outbox,
                payment=payment,
                attempt_no=attempt_no,
                epoch=epoch,
                error=error,
                delivery_phase=delivery_phase,
                log=log,
            )
        if delivery_phase and payment.status.is_terminal:
            recorded = await self._payments.record_delivery_failure(
                payment_id=payment.id,
                error=error,
                owner=self._owner,
                epoch=epoch,
                final=False,
            )
            if not recorded:
                return MessageResult.REQUEUE
        delay = self._settings.retry_delay_seconds(attempt_no)
        next_at = self._clock.now() + timedelta(seconds=delay)
        scheduled = await self._outbox.schedule_retry(
            event_id=outbox.id,
            owner=self._owner,
            epoch=epoch,
            next_attempt_no=attempt_no + 1,
            next_attempt_at=next_at,
            error=error,
        )
        if not scheduled:
            return MessageResult.REQUEUE
        log.info("retry scheduled for attempt %d", attempt_no + 1)
        return MessageResult.ACK

    async def _dead_letter(
        self,
        outbox: OutboxRecord,
        *,
        payment: PaymentRecord | None,
        attempt_no: int,
        epoch: int,
        error: str,
        delivery_phase: bool,
        log: BoundLogger,
    ) -> MessageResult:
        payload = {
            "event_id": str(outbox.id),
            "payment_id": str(outbox.payment_id),
            "event_type": outbox.event_type,
            "attempt_no": attempt_no,
            "reason": error,
            "failed_at": self._clock.now().isoformat(),
        }
        # Acknowledge only after the dead-letter publication is confirmed.
        await self._publisher.publish(
            DLQ_ROUTING_KEY,
            payload,
            {HEADER_ATTEMPT: attempt_no},
            dead_letter=True,
        )
        if delivery_phase and payment is not None and payment.status.is_terminal:
            await self._payments.record_delivery_failure(
                payment_id=payment.id,
                error=error,
                owner=self._owner,
                epoch=epoch,
                final=True,
            )
        closed = await self._outbox.complete(
            event_id=outbox.id,
            owner=self._owner,
            epoch=epoch,
            error=error,
        )
        log.warning("unfinished work dead-lettered: %s", error)
        return MessageResult.ACK if closed else MessageResult.REQUEUE

    async def _publish_durable_copy(self, outbox: OutboxRecord, attempt_no: int) -> None:
        body = dict(outbox.payload)
        await self._publisher.publish(
            PAYMENTS_ROUTING_KEY,
            body,
            {HEADER_ATTEMPT: attempt_no, HEADER_REDELIVERY: True},
        )

    async def _heartbeat_loop(self, event_id: UUID, epoch: int) -> None:
        interval = self._settings.heartbeat_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await self._outbox.heartbeat(
                    event_id=event_id,
                    owner=self._owner,
                    epoch=epoch,
                    lease_seconds=self._settings.attempt_lease_seconds,
                )
                if not renewed:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - heartbeat must never break processing
            self._log.bind(event_id=event_id).warning("heartbeat renewal failed")
