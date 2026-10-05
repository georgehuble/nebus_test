"""Atomic acceptance: payment and outbox commit together or not at all."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from app.application.fingerprint import request_fingerprint
from app.container import ApiContainer
from app.domain.dto import PaymentCreateRequest
from app.infrastructure.db import Database
from app.infrastructure.repositories import PaymentRepository
from pydantic import HttpUrl
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.infra import TEST_API_KEY

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": TEST_API_KEY}


async def _counts(database: Database) -> tuple[int, int]:
    async with database.session() as session:
        payments = (await session.execute(text("SELECT count(*) FROM payments"))).scalar_one()
        outbox = (await session.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
    return int(payments), int(outbox)


async def test_failed_creation_leaves_no_partial_state(database: Database) -> None:
    repository = PaymentRepository(database)
    # Bypass pydantic validation to violate the database CHECK constraint and
    # force a failure after the payment INSERT has been attempted.
    oversized = PaymentCreateRequest.model_construct(
        amount=Decimal("1.00"),
        currency="RUB",
        description="x" * 501,
        metadata={},
        webhook_url=HttpUrl("http://receiver.test/webhook"),
    )
    with pytest.raises(IntegrityError):
        await repository.create(
            idempotency_key="atomic-failure",
            request=oversized,
            fingerprint=request_fingerprint(oversized),
        )
    assert await _counts(database) == (0, 0)


async def test_accepted_payment_commits_with_its_outbox_event(
    api_client: tuple[httpx.AsyncClient, ApiContainer],
) -> None:
    client, container = api_client
    # Creation never touches the broker, so an unavailable broker cannot lose
    # an accepted payment: the row and its outbox event are committed first.
    response = await client.post(
        "/api/v1/payments",
        json={
            "amount": "5.00",
            "currency": "USD",
            "description": "",
            "metadata": {},
            "webhook_url": "http://receiver.test/webhook",
        },
        headers={**AUTH, "Idempotency-Key": "atomic-ok"},
    )
    assert response.status_code == 202
    assert await _counts(container.database) == (1, 1)
    async with container.database.session() as session:
        published = (await session.execute(text("SELECT count(*) FROM outbox WHERE published_at IS NULL"))).scalar_one()
    assert int(published) == 1


async def test_rollback_of_a_repeated_key_keeps_single_row(database: Database) -> None:
    repository = PaymentRepository(database)
    request = PaymentCreateRequest.model_validate(
        {
            "amount": "3.00",
            "currency": "EUR",
            "description": "",
            "metadata": {},
            "webhook_url": "http://receiver.test/webhook",
        }
    )
    fingerprint = request_fingerprint(request)
    first = await repository.create(idempotency_key="dup-key", request=request, fingerprint=fingerprint)
    second = await repository.create(idempotency_key="dup-key", request=request, fingerprint=fingerprint)
    assert first.created is True
    assert second.created is False
    assert first.payment.id == second.payment.id
    assert await _counts(database) == (1, 1)
