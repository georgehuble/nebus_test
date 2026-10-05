"""Authenticated API access: every route requires a valid X-API-Key."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from app.container import ApiContainer

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


def _body() -> dict[str, Any]:
    return {
        "amount": "10.50",
        "currency": "RUB",
        "description": "auth",
        "metadata": {},
        "webhook_url": "http://receiver.test/webhook",
    }


async def test_missing_and_wrong_api_key_are_rejected(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    missing = await client.post("/api/v1/payments", json=_body(), headers={"Idempotency-Key": "auth-1"})
    assert missing.status_code == 401
    wrong = await client.post(
        "/api/v1/payments", json=_body(), headers={"Idempotency-Key": "auth-1", "X-API-Key": "wrong"}
    )
    assert wrong.status_code == 401


async def test_every_route_is_key_protected(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    for path in ("/api/v1/health", "/api/v1/openapi.json", f"/api/v1/payments/{uuid4()}"):
        response = await client.get(path)
        assert response.status_code == 401, path
    assert (await client.post("/api/v1/payments", json=_body())).status_code == 401


async def test_health_requires_the_api_key(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    unauthorized = await client.get("/api/v1/health")
    assert unauthorized.status_code == 401
    authorized = await client.get("/api/v1/health", headers=AUTH)
    assert authorized.status_code == 200
    assert authorized.json() == {"status": "ok", "database": "ok"}


async def test_introspection_routes_are_not_publicly_readable(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, _ = api_client
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = await client.get(path)
        assert response.status_code in (401, 404), path
