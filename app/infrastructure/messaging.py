"""RabbitMQ topology and confirmed publisher built on aio-pika.

The topology uses only broker built-ins (no plugins): a durable direct exchange,
a durable **quorum** new-payment queue, and a durable direct dead-letter exchange
with a quorum dead-letter queue. Retry is scheduled in the database and
republished by a dispatcher with publisher confirmation, so broker TTL
dead-lettering is never used as the retry mechanism.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractQueue,
    AbstractRobustConnection,
)

from app.errors import PublishError

PAYMENTS_EXCHANGE = "payments"
PAYMENTS_QUEUE = "payments.new"
PAYMENTS_ROUTING_KEY = "payments.new"
DLX_EXCHANGE = "payments.dlx"
DLQ_QUEUE = "payments.new.dlq"
DLQ_ROUTING_KEY = "payments.new"

HEADER_ATTEMPT = "x-attempt"
HEADER_REDELIVERY = "x-redelivery"


@dataclass(frozen=True, slots=True)
class Topology:
    """Declared broker entities."""

    exchange: AbstractExchange
    main_queue: AbstractQueue
    dlx: AbstractExchange
    dlq: AbstractQueue


async def declare_topology(channel: AbstractChannel) -> Topology:
    """Idempotently declare the full topology on ``channel``."""
    exchange = await channel.declare_exchange(PAYMENTS_EXCHANGE, ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX_EXCHANGE, ExchangeType.DIRECT, durable=True)

    main_queue = await channel.declare_queue(
        PAYMENTS_QUEUE,
        durable=True,
        arguments={
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": DLQ_ROUTING_KEY,
        },
    )
    await main_queue.bind(exchange, routing_key=PAYMENTS_ROUTING_KEY)

    dlq = await channel.declare_queue(
        DLQ_QUEUE,
        durable=True,
        arguments={"x-queue-type": "quorum"},
    )
    await dlq.bind(dlx, routing_key=DLQ_ROUTING_KEY)
    return Topology(exchange=exchange, main_queue=main_queue, dlx=dlx, dlq=dlq)


class RabbitPublisher:
    """Publisher that waits for a positive broker confirmation (mandatory routing).

    The main exchange is used for new-payment and retry deliveries; the
    dead-letter exchange is used for messages that exhausted the attempt budget
    or recovery allowance.
    """

    def __init__(self, exchange: AbstractExchange, dlx: AbstractExchange) -> None:
        self._exchange = exchange
        self._dlx = dlx

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        headers: dict[str, Any],
        *,
        dead_letter: bool = False,
    ) -> None:
        """Publish a persistent message and raise if routing is not confirmed."""
        target = self._dlx if dead_letter else self._exchange
        message = Message(
            body=json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"),
            content_type="application/json",
            content_encoding="utf-8",
            delivery_mode=DeliveryMode.PERSISTENT,
            headers=headers,
            message_id=str(payload.get("event_id", "")),
            timestamp=datetime.now(UTC),
        )
        try:
            await target.publish(message, routing_key=routing_key, mandatory=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize broker errors to PublishError
            raise PublishError(f"publish to {routing_key!r} not confirmed: {exc}") from exc


class BrokerPublisher:
    """Event publisher that resolves the confirmed publisher lazily.

    The composition root can build the processor, relay and dispatcher before the
    broker connection is opened; the confirmed exchange is resolved on first use.
    """

    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        headers: dict[str, Any],
        *,
        dead_letter: bool = False,
    ) -> None:
        """Publish through the broker's confirmed publisher."""
        await self._broker.publisher.publish(
            routing_key,
            payload,
            headers,
            dead_letter=dead_letter,
        )


class Broker:
    """Owns the robust connection, a confirmed publish channel and a consume channel."""

    def __init__(self, url: str, *, prefetch_count: int = 1) -> None:
        self._url = url
        self._prefetch_count = prefetch_count
        self._connection: AbstractRobustConnection | None = None
        self._publish_channel: AbstractChannel | None = None
        self._consume_channel: AbstractChannel | None = None
        self._topology: Topology | None = None
        self._close_lock = asyncio.Lock()

    async def start(self) -> None:
        """Open the connection and declare the topology."""
        connection = await aio_pika.connect_robust(self._url, client_properties={"connection_name": "payment-service"})
        publish_channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        consume_channel = await connection.channel(publisher_confirms=False)
        await consume_channel.set_qos(prefetch_count=self._prefetch_count)
        topology = await declare_topology(publish_channel)
        self._connection = connection
        self._publish_channel = publish_channel
        self._consume_channel = consume_channel
        self._topology = topology

    async def close(self) -> None:
        """Close channels and the connection cleanly."""
        async with self._close_lock:
            connection = self._connection
            self._connection = None
            self._publish_channel = None
            self._consume_channel = None
            self._topology = None
            if connection is not None and not connection.is_closed:
                await connection.close()

    @property
    def topology(self) -> Topology:
        """Return the declared topology."""
        if self._topology is None:
            msg = "broker is not started"
            raise RuntimeError(msg)
        return self._topology

    @property
    def publisher(self) -> RabbitPublisher:
        """Return a publisher bound to the confirmed publish channel."""
        topology = self.topology
        return RabbitPublisher(topology.exchange, topology.dlx)

    @property
    def consume_queue(self) -> AbstractQueue:
        """Return the queue used by the consumer."""
        if self._consume_channel is None:
            msg = "broker is not started"
            raise RuntimeError(msg)
        return self.topology.main_queue

    def is_ready(self) -> bool:
        """Return ``True`` when the connection is healthy."""
        connection = self._connection
        return connection is not None and not connection.is_closed
