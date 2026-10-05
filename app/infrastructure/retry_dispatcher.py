"""Retry dispatcher: republishes scheduled retries with publisher confirmation.

Retry scheduling lives durably in the database. The dispatcher selects rows whose
scheduled time has arrived, publishes them with the expected ``x-attempt`` header
and only clears the schedule after the broker confirms routing. A crash between
the confirmation and the clear causes a safe duplicate that the consumer
recognises by its attempt header.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from app.errors import PublishError
from app.infrastructure.messaging import HEADER_ATTEMPT, PAYMENTS_ROUTING_KEY
from app.infrastructure.repositories import OutboxRepository
from app.logging_config import BoundLogger, get_logger
from app.protocols import EventPublisher
from app.settings import Settings


class RetryDispatcher:
    """Background task republishing durable retry schedules."""

    def __init__(
        self,
        *,
        outbox: OutboxRepository,
        publisher: EventPublisher,
        settings: Settings,
    ) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._settings = settings
        self._log: BoundLogger = get_logger("app.retry")

    async def dispatch_once(self) -> int:
        """Publish all currently due retries; return the confirmed count."""
        due = await self._outbox.fetch_due_retries(batch_size=self._settings.relay_batch_size)
        confirmed: list[UUID] = []
        for record in due:
            attempt_no = record.next_attempt_no or record.attempt_no + 1
            try:
                await self._publisher.publish(
                    PAYMENTS_ROUTING_KEY,
                    dict(record.payload),
                    {HEADER_ATTEMPT: attempt_no},
                )
            except PublishError as exc:
                self._log.bind(event_id=record.id).warning("retry publish failed: %s", exc)
                continue
            confirmed.append(record.id)
        if confirmed:
            await self._outbox.mark_dispatched(confirmed)
        return len(confirmed)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll for due retries until ``stop_event`` is set."""
        while not stop_event.is_set():
            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dispatcher cycle must not kill the loop
                self._log.exception("retry dispatch cycle failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._settings.retry_poll_interval_seconds)
            except TimeoutError:
                continue
