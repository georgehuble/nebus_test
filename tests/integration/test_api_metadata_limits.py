"""Metadata size boundaries measured on real PostgreSQL."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.container import ApiContainer
from app.domain.dto import METADATA_MAX_BYTES, canonical_json_bytes

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


def _metadata_exactly(target: int, pad_char: str = "x") -> dict[str, Any]:
    """Build metadata with multiple keys, numeric values and padding to ``target`` bytes."""
    metadata: dict[str, Any] = {"order": 42, "flags": [True, False], "unicode": "héllo", "pad": ""}
    needed = target - len(canonical_json_bytes(metadata))
    assert needed >= 0
    char_bytes = len(pad_char.encode("utf-8"))
    characters, remainder = divmod(needed, char_bytes)
    metadata["pad"] = pad_char * characters + "x" * remainder
    assert len(canonical_json_bytes(metadata)) == target
    return metadata


def _body(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "amount": "10.50",
        "currency": "RUB",
        "description": "limits",
        "metadata": metadata,
        "webhook_url": "http://receiver.test/webhook",
    }


async def test_metadata_exactly_at_limit_is_accepted_and_round_trips(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, _ = api_client
    metadata = _metadata_exactly(METADATA_MAX_BYTES)
    response = await client.post(
        "/api/v1/payments", json=_body(metadata), headers={**AUTH, "Idempotency-Key": "meta-limit"}
    )
    assert response.status_code == 202
    payment_id = response.json()["payment_id"]
    read = await client.get(f"/api/v1/payments/{payment_id}", headers=AUTH)
    stored = read.json()["metadata"]
    assert len(canonical_json_bytes(stored)) == METADATA_MAX_BYTES
    assert stored["order"] == 42
    assert stored["flags"] == [True, False]
    assert stored["unicode"] == "héllo"


async def test_metadata_above_limit_is_rejected(api_client: tuple[httpx.AsyncClient, ApiContainer]) -> None:
    client, _ = api_client
    metadata = _metadata_exactly(METADATA_MAX_BYTES + 1)
    response = await client.post(
        "/api/v1/payments", json=_body(metadata), headers={**AUTH, "Idempotency-Key": "meta-over"}
    )
    assert response.status_code == 422


async def test_metadata_limit_counts_utf8_bytes_with_multibyte_characters(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, _ = api_client
    # 'é' is two UTF-8 bytes, so half as many characters fit the same byte budget.
    metadata = _metadata_exactly(METADATA_MAX_BYTES, pad_char="é")
    response = await client.post(
        "/api/v1/payments", json=_body(metadata), headers={**AUTH, "Idempotency-Key": "meta-unicode"}
    )
    assert response.status_code == 202
    over = _metadata_exactly(METADATA_MAX_BYTES + 2, pad_char="é")
    rejected = await client.post(
        "/api/v1/payments", json=_body(over), headers={**AUTH, "Idempotency-Key": "meta-unicode-over"}
    )
    assert rejected.status_code == 422
