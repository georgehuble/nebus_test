"""Webhook client success criteria and network policy."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from app.domain.models import PaymentStatus
from app.domain.webhook import WebhookPayload
from app.errors import WebhookDeliveryError
from app.infrastructure.webhook_client import HttpxWebhookSender

pytestmark = pytest.mark.unit

URL = "http://receiver.test/webhook"
PAYLOAD = WebhookPayload(
    event_id=uuid4(),
    payment_id=uuid4(),
    status=PaymentStatus.SUCCEEDED,
    processed_at=datetime(2026, 1, 1, tzinfo=UTC),
)


def _sender(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=False,
        timeout=httpx.Timeout(1.0),
    )


async def test_2xx_is_success() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    client = _sender(handler)
    sender = HttpxWebhookSender(client)
    await sender.send(URL, PAYLOAD)
    await client.aclose()
    assert len(calls) == 1
    assert calls[0].headers["content-type"] == "application/json"


async def test_non_2xx_is_retryable() -> None:
    client = _sender(lambda request: httpx.Response(500))
    sender = HttpxWebhookSender(client)
    with pytest.raises(WebhookDeliveryError) as excinfo:
        await sender.send(URL, PAYLOAD)
    await client.aclose()
    assert excinfo.value.status_code == 500


async def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    client = _sender(handler)
    sender = HttpxWebhookSender(client)
    with pytest.raises(WebhookDeliveryError):
        await sender.send(URL, PAYLOAD)
    await client.aclose()


async def test_redirects_are_not_followed() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://elsewhere.test/webhook"})

    client = _sender(handler)
    sender = HttpxWebhookSender(client)
    with pytest.raises(WebhookDeliveryError) as excinfo:
        await sender.send(URL, PAYLOAD)
    await client.aclose()
    assert excinfo.value.status_code == 302
    assert len(calls) == 1
