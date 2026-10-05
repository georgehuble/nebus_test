"""Helpers shared by the integration and end-to-end tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import aio_pika
import uvicorn
from app.application.fingerprint import request_fingerprint
from app.application.processing import PaymentProcessor
from app.container import ConsumerContainer
from app.domain.dto import PaymentCreateRequest
from app.infrastructure.db import Database
from app.infrastructure.messaging import PAYMENTS_QUEUE, Broker, Topology
from app.infrastructure.repositories import OutboxRecord, OutboxRepository, PaymentRecord, PaymentRepository
from app.infrastructure.system import SystemClock
from app.protocols import Clock, PaymentGateway, WebhookSender
from app.settings import Settings
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

DEFAULT_WEBHOOK_URL = "http://127.0.0.1:1/hook"


async def purge_queues(broker: Broker) -> None:
    """Remove messages from the main and dead-letter queues."""
    topology = broker.topology
    await topology.main_queue.purge()
    await topology.dlq.purge()


async def get_messages(
    broker: Broker, queue_name: str, *, limit: int = 10, timeout: float = 3.0
) -> list[dict[str, Any]]:
    """Drain up to ``limit`` messages from a queue."""
    topology: Topology = broker.topology
    queue = topology.main_queue if queue_name == PAYMENTS_QUEUE else topology.dlq
    collected: list[dict[str, Any]] = []
    for _ in range(limit):
        try:
            message = await asyncio.wait_for(queue.get(no_ack=False, fail=False), timeout=timeout)
        except TimeoutError:
            break
        if message is None:
            break
        await message.ack()
        collected.append(
            {
                "body": json.loads(message.body),
                "headers": dict(message.headers or {}),
            }
        )
    return collected


async def wait_for(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
    message: str = "condition not met in time",
) -> None:
    """Await an asynchronous predicate until it holds or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(message)


async def wait_for_count(items: list[Any], count: int, *, timeout: float = 20.0) -> None:
    """Wait until ``items`` holds at least ``count`` entries."""

    async def _check() -> bool:
        return len(items) >= count

    await wait_for(_check, timeout=timeout, message=f"expected at least {count} items, got {len(items)}")


async def queue_message_count(broker_url: str, queue_name: str) -> int:
    """Read the message count of a queue without consuming it."""
    connection = await aio_pika.connect_robust(broker_url)
    try:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, passive=True)
        return int(queue.declaration_result.message_count or 0)
    finally:
        await connection.close()


async def wait_for_queue_message(broker_url: str, queue_name: str, *, timeout: float = 30.0) -> None:
    """Wait until a queue reports at least one message."""

    async def _check() -> bool:
        return await queue_message_count(broker_url, queue_name) >= 1

    await wait_for(_check, timeout=timeout, message=f"no message in {queue_name}")


def build_payment_request(**overrides: Any) -> PaymentCreateRequest:
    """Build a valid creation request for integration tests."""
    body: dict[str, Any] = {
        "amount": "10.50",
        "currency": "RUB",
        "description": "integration",
        "metadata": {"order": "42"},
        "webhook_url": DEFAULT_WEBHOOK_URL,
    }
    body.update(overrides)
    return PaymentCreateRequest.model_validate(body)


async def create_payment_records(
    payments: PaymentRepository,
    outbox: OutboxRepository,
    *,
    idempotency_key: str | None = None,
    **overrides: Any,
) -> tuple[PaymentRecord, OutboxRecord]:
    """Create a payment with its outbox event and return both snapshots."""
    request = build_payment_request(**overrides)
    key = idempotency_key or f"key-{uuid4().hex}"
    result = await payments.create(
        idempotency_key=key,
        request=request,
        fingerprint=request_fingerprint(request),
    )
    record = await outbox.fetch_by_payment(result.payment.id)
    assert record is not None
    return result.payment, record


async def create_payment(
    container: ConsumerContainer,
    *,
    idempotency_key: str | None = None,
    **overrides: Any,
) -> tuple[PaymentRecord, OutboxRecord]:
    """Create a payment through a consumer container."""
    return await create_payment_records(
        container.payments,
        container.outbox,
        idempotency_key=idempotency_key,
        **overrides,
    )


def build_processor(
    settings: Settings,
    database: Database,
    broker: Broker,
    *,
    gateway: PaymentGateway,
    webhooks: WebhookSender,
    owner: str = "test-owner",
    clock: Clock | None = None,
) -> PaymentProcessor:
    """Wire a processor over real repositories and a real broker publisher."""
    return PaymentProcessor(
        settings=settings,
        payments=PaymentRepository(database),
        outbox=OutboxRepository(database),
        gateway=gateway,
        webhooks=webhooks,
        publisher=broker.publisher,
        clock=clock or SystemClock(),
        owner=owner,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class WebhookReceiver:
    """A local HTTP receiver that records webhook deliveries.

    ``fail_remaining`` forces the first ``n`` deliveries to answer with a
    retryable ``500``; ``status_code`` and ``delay_seconds`` control success
    responses and timeouts.
    """

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.status_code = 200
        self.fail_remaining = 0
        self.delay_seconds = 0.0
        self._app = FastAPI()
        self._app.post("/hook")(self._hook)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.port = 0

    async def _hook(self, request: Request) -> Response:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        payload = await request.json()
        self.received.append(payload)
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            return JSONResponse({"status": "retryable"}, status_code=500)
        return JSONResponse({"status": "ok"}, status_code=self.status_code)

    @property
    def url(self) -> str:
        """Host/container reachable URL of the receiver."""
        return f"http://127.0.0.1:{self.port}/hook"

    async def start(self) -> None:
        """Start the receiver on a free port."""
        self.port = _free_port()
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(500):
            if self._server.started:
                return
            await asyncio.sleep(0.01)
        raise RuntimeError("webhook receiver did not start")

    async def stop(self) -> None:
        """Stop the receiver cleanly."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._task, timeout=5)
