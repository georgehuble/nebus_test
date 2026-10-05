"""HTTP webhook client with a bounded timeout and a strict success criterion."""

from __future__ import annotations

import httpx

from app.domain.webhook import WebhookPayload
from app.errors import WebhookDeliveryError


class HttpxWebhookSender:
    """Deliver webhook notifications using a shared :class:`httpx.AsyncClient`.

    Success requires a ``2xx`` response. Timeouts, network errors, non-``2xx``
    responses and redirects are all treated as retryable failures. Redirects are
    never followed because the client is created with ``follow_redirects=False``.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, url: str, payload: WebhookPayload) -> None:
        """Deliver ``payload`` to ``url`` or raise :class:`WebhookDeliveryError`."""
        try:
            response = await self._client.post(
                url,
                json=payload.model_dump(mode="json"),
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(f"webhook transport error: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise WebhookDeliveryError(
                f"webhook returned non-2xx status {response.status_code}",
                status_code=response.status_code,
            )

    @classmethod
    def create(cls, *, timeout_seconds: float) -> HttpxWebhookSender:
        """Build a sender with a bounded timeout and no redirect following."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )
        return cls(client)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
