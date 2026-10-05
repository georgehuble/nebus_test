"""Outbox relay: claims committed records and publishes them with confirmation."""

from __future__ import annotations

import asyncio
from uuid import UUID

from app.errors import PublishError
from app.infrastructure.messaging import HEADER_ATTEMPT, PAYMENTS_ROUTING_KEY
from app.infrastructure.repositories import OutboxRepository
from app.logging_config import BoundLogger, get_logger
from app.protocols import EventPublisher
from app.settings import Settings


class OutboxRelay:
    """Background task publishing committed outbox events exactly-once best effort.

    The relay claims records so concurrent iterations never publish the same row
    twice, publishes outside any database transaction, and marks a record as
    published only after a positive broker confirmation. A crash between the
    confirmation and the mark causes a safe duplicate publication (at-least-once).
    """

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
        self._log: BoundLogger = get_logger("app.relay")

    async def relay_once(self) -> int:
        """Publish one claimed batch; return the number of confirmed messages."""
        batch = await self._outbox.claim_batch(
            batch_size=self._settings.relay_batch_size,
            lease_seconds=self._settings.relay_claim_lease_seconds,
        )
        if not batch:
            return 0
        confirmed: list[UUID] = []
        for record in batch:
            try:
                await self._publisher.publish(
                    PAYMENTS_ROUTING_KEY,
                    dict(record.payload),
                    {HEADER_ATTEMPT: 1},
                )
            except PublishError as exc:
                self._log.bind(event_id=record.id).warning("relay publish failed: %s", exc)
                continue
            confirmed.append(record.id)
        if confirmed:
            await self._outbox.mark_published(confirmed)
        return len(confirmed)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll the outbox until ``stop_event`` is set."""
        while not stop_event.is_set():
            try:
                await self.relay_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a relay cycle must not kill the loop
                self._log.exception("relay cycle failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._settings.relay_poll_interval_seconds)
            except TimeoutError:
                continue
