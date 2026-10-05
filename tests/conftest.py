"""Shared pytest fixtures.

The infrastructure fixtures below are only used by ``integration`` and ``e2e``
tests. They are deliberately not autouse, so ``pytest -m unit`` never connects to
PostgreSQL or RabbitMQ.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from alembic import command
from app.api.app import create_app
from app.container import ApiContainer, build_api_container
from app.infrastructure.db import Database
from app.infrastructure.messaging import Broker
from app.settings import Settings
from sqlalchemy import text

from tests.infra import TEST_API_KEY, TEST_DATABASE_URL, TEST_RABBITMQ_URL, alembic_config
from tests.integration.harness import WebhookReceiver, purge_queues


@pytest.fixture()
def settings_factory() -> Callable[..., Settings]:
    """Return a factory building :class:`Settings` without touching the environment."""

    def _factory(
        *,
        api_key: str = "test-api-key",
        database_url: str = TEST_DATABASE_URL,
        rabbitmq_url: str = TEST_RABBITMQ_URL,
        max_attempts: int = 3,
        max_recoveries: int = 5,
        retry_base_delay_seconds: float = 1.0,
        attempt_lease_seconds: float = 15.0,
        heartbeat_interval_seconds: float = 300.0,
        relay_poll_interval_seconds: float = 0.01,
        retry_poll_interval_seconds: float = 0.01,
        gateway_decline_probability: float = 0.1,
    ) -> Settings:
        return Settings(
            api_key=api_key,  # type: ignore[arg-type]
            database_url=database_url,
            rabbitmq_url=rabbitmq_url,
            max_attempts=max_attempts,
            max_recoveries=max_recoveries,
            retry_base_delay_seconds=retry_base_delay_seconds,
            attempt_lease_seconds=attempt_lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            relay_poll_interval_seconds=relay_poll_interval_seconds,
            retry_poll_interval_seconds=retry_poll_interval_seconds,
            gateway_decline_probability=gateway_decline_probability,
        )

    return _factory


@pytest.fixture(scope="session")
def migrated_schema() -> Iterator[None]:
    """Apply migrations once for the whole integration/E2E session."""
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture()
def settings(migrated_schema: None) -> Settings:
    """Settings pointing at the local infrastructure with fast timers."""
    return Settings(
        api_key=TEST_API_KEY,  # type: ignore[arg-type]
        database_url=TEST_DATABASE_URL,
        rabbitmq_url=TEST_RABBITMQ_URL,
        gateway_min_delay_seconds=0.0,
        gateway_max_delay_seconds=0.0,
        gateway_decline_probability=0.0,
        retry_base_delay_seconds=0.05,
        attempt_lease_seconds=5.0,
        heartbeat_interval_seconds=1.0,
        relay_poll_interval_seconds=0.05,
        retry_poll_interval_seconds=0.05,
    )


@pytest.fixture()
async def database(settings: Settings) -> AsyncIterator[Database]:
    """Provide a database handle and truncate the domain tables first."""
    db = Database(settings.database_url)
    async with db.session() as session, session.begin():
        await session.execute(text("TRUNCATE payments, outbox RESTART IDENTITY CASCADE"))
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture()
async def broker(settings: Settings) -> AsyncIterator[Broker]:
    """Provide a started broker connection with freshly purged queues."""
    connection = Broker(settings.rabbitmq_url, prefetch_count=10)
    await connection.start()
    await purge_queues(connection)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture()
async def receiver() -> AsyncIterator[WebhookReceiver]:
    """Provide a local HTTP webhook receiver."""
    webhook_receiver = WebhookReceiver()
    await webhook_receiver.start()
    try:
        yield webhook_receiver
    finally:
        await webhook_receiver.stop()


@pytest.fixture()
async def api_client(settings: Settings, database: Database) -> AsyncIterator[tuple[httpx.AsyncClient, ApiContainer]]:
    """Provide an in-process HTTP client bound to the real database."""
    container = build_api_container(settings)
    application = create_app(settings)
    application.state.container = container
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, container
    await container.database.dispose()
