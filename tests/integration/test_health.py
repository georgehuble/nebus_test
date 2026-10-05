"""Readiness endpoints that exercise the running processes."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from app.api.app import create_app
from app.consumer.main import ConsumerRuntime, create_consumer_app
from app.infrastructure.db import Database
from app.infrastructure.messaging import Broker
from app.settings import Settings

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


async def test_api_lifespan_and_health(settings: Settings, database: Database) -> None:
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            unauthorized = await client.get("/api/v1/health")
            assert unauthorized.status_code == 401
            ready = await client.get("/api/v1/health", headers=AUTH)
            assert ready.status_code == 200
            assert ready.json() == {"status": "ok", "database": "ok"}


async def test_consumer_readiness_reflects_running_tasks(
    settings: Settings, database: Database, broker: Broker
) -> None:
    application = create_consumer_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with application.router.lifespan_context(application):
        runtime: ConsumerRuntime = application.state.runtime
        async with httpx.AsyncClient(transport=transport, base_url="http://consumer") as client:
            assert (await client.get("/api/v1/health")).status_code == 401
            ready = await client.get("/api/v1/health", headers=AUTH)
            assert ready.status_code == 200
            assert ready.json() == {
                "status": "ok",
                "database": "ok",
                "broker": "ok",
                "handler": "ok",
                "relay": "ok",
                "retry": "ok",
            }

            # Degrade readiness by stopping the relay task.
            assert runtime.relay_task is not None
            runtime.relay_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runtime.relay_task
            degraded = await client.get("/api/v1/health", headers=AUTH)
            assert degraded.status_code == 503
            assert degraded.json()["relay"] == "error"
            assert degraded.json()["status"] == "error"

    # The lifespan exit stopped every background task cleanly.
    assert runtime.runner is not None
    assert runtime.runner.is_running() is False
