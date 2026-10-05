"""Asynchronous boundaries, session ownership and clean task shutdown."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from unittest.mock import AsyncMock

import app.application.processing as processing_module
import app.infrastructure.db as db_module
import app.infrastructure.relay as relay_module
import app.infrastructure.retry_dispatcher as retry_module
import app.infrastructure.system as system_module
import app.infrastructure.webhook_client as webhook_module
import pytest
from app.infrastructure.db import Database
from app.infrastructure.relay import OutboxRelay
from app.infrastructure.repositories import OutboxRepository
from app.infrastructure.retry_dispatcher import RetryDispatcher
from app.settings import Settings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.unit.fakes import RecordingPublisher

pytestmark = pytest.mark.unit


def test_database_uses_an_async_engine(settings_factory: Callable[..., Settings]) -> None:
    database = Database(settings_factory().database_url)
    assert isinstance(database.engine, AsyncEngine)
    assert database.session_factory.kw["expire_on_commit"] is False


async def test_sessions_are_never_shared(settings_factory: Callable[..., Settings]) -> None:
    database = Database(settings_factory().database_url)
    async with database.session() as first, database.session() as second:
        assert isinstance(first, AsyncSession)
        assert isinstance(second, AsyncSession)
        assert first is not second
    await database.dispose()


def test_async_modules_do_not_block_the_event_loop() -> None:
    modules = (
        db_module,
        system_module,
        processing_module,
        webhook_module,
        relay_module,
        retry_module,
    )
    for module in modules:
        source = inspect.getsource(module)
        assert "time.sleep" not in source, module.__name__
        assert "requests." not in source, module.__name__
    assert "asyncio.sleep" in inspect.getsource(system_module)


async def test_relay_runs_and_stops_cleanly(settings_factory: Callable[..., Settings]) -> None:
    outbox = AsyncMock(spec=OutboxRepository)
    outbox.claim_batch.return_value = []
    relay = OutboxRelay(outbox=outbox, publisher=RecordingPublisher(), settings=settings_factory())
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert task.exception() is None


async def test_retry_dispatcher_runs_and_stops_cleanly(settings_factory: Callable[..., Settings]) -> None:
    outbox = AsyncMock(spec=OutboxRepository)
    outbox.fetch_due_retries.return_value = []
    dispatcher = RetryDispatcher(
        outbox=outbox,
        publisher=RecordingPublisher(),
        settings=settings_factory(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert task.exception() is None
