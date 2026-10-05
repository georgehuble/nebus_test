"""End-to-end stack fixture: handler, relay and retry dispatcher in one process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from app.consumer.runner import ConsumerRunner
from app.container import ConsumerContainer, build_consumer_container
from app.infrastructure.db import Database
from app.infrastructure.messaging import Broker
from app.settings import Settings


@pytest.fixture()
async def stack(
    settings: Settings,
    database: Database,
    broker: Broker,
) -> AsyncIterator[ConsumerContainer]:
    """Run the consumer, relay and retry dispatcher against real infrastructure.

    The fixture also exercises clean startup and shutdown: on teardown every
    background task is cancelled and awaited, then connections and pools are closed.
    """
    container = build_consumer_container(settings)
    stop_event = asyncio.Event()
    await container.broker.start()
    runner = ConsumerRunner(
        broker=container.broker,
        processor=container.processor,
        stop_event=stop_event,
        settings=settings,
    )
    await runner.start()
    relay_task = asyncio.create_task(container.relay.run(stop_event), name="relay")
    retry_task = asyncio.create_task(container.retry_dispatcher.run(stop_event), name="retry")
    try:
        yield container
    finally:
        stop_event.set()
        await runner.stop()
        for task in (relay_task, retry_task):
            task.cancel()
        await asyncio.gather(relay_task, retry_task, return_exceptions=True)
        await container.webhook_sender.aclose()
        await container.broker.close()
        await container.database.dispose()
