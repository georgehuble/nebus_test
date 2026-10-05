"""Consumer loop: decodes deliveries and settles them by the processor's verdict."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from app.application.processing import MessageResult, PaymentProcessor
from app.domain.events import MessageEnvelope
from app.infrastructure.messaging import HEADER_ATTEMPT, HEADER_REDELIVERY, Broker
from app.logging_config import get_logger
from app.settings import Settings


class ConsumerRunner:
    """Runs the single consumer that receives new-payment messages."""

    def __init__(
        self,
        *,
        broker: Broker,
        processor: PaymentProcessor,
        stop_event: asyncio.Event,
        settings: Settings,
    ) -> None:
        self._broker = broker
        self._processor = processor
        self._stop = stop_event
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("app.consumer")

    def is_running(self) -> bool:
        """Return ``True`` while the consumer task is alive."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start consuming in the background."""
        self._task = asyncio.create_task(self._consume(), name="payment-consumer")

    async def stop(self) -> None:
        """Cancel the consumer and await its shutdown."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _consume(self) -> None:
        queue = self._broker.consume_queue
        async with queue.iterator() as iterator:
            async for message in iterator:
                if self._stop.is_set():
                    break
                await self._settle(message.body, message.headers, message)

    async def _settle(self, body: bytes, headers: Mapping[str, Any] | None, message: Any) -> None:
        try:
            result = await self.handle_raw(body, headers)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let one delivery kill the loop
            self._log.exception("unexpected consumer error; requeueing delivery")
            await message.nack(requeue=True)
            return
        if result is MessageResult.ACK:
            await message.ack()
        else:
            await message.nack(requeue=True)

    async def handle_raw(self, body: bytes, headers: Mapping[str, Any] | None) -> MessageResult:
        """Decode a delivery and delegate it to the processor (no ack side effects)."""
        header_map: Mapping[str, Any] = headers or {}
        attempt_no = 1
        raw_attempt = header_map.get(HEADER_ATTEMPT, 1)
        try:
            attempt_no = int(raw_attempt)
        except (TypeError, ValueError):
            attempt_no = 1
        redelivery = bool(header_map.get(HEADER_REDELIVERY, False))
        try:
            payload = json.loads(body)
            envelope = MessageEnvelope.from_body(payload, attempt_no=attempt_no, redelivery=redelivery)
        except (ValueError, KeyError, TypeError):
            self._log.warning("invalid message payload; acknowledging deterministically")
            return MessageResult.ACK
        return await self._processor.process(envelope)
