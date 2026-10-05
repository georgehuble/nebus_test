"""Money, currency, URL and timestamp validation and serialization."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.domain.dto import PaymentCreateRequest, PaymentView
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": "10.50",
        "currency": "RUB",
        "description": "test",
        "metadata": {"order": "42"},
        "webhook_url": "http://receiver.test/webhook",
    }
    body.update(overrides)
    return body


def _create(**overrides: Any) -> PaymentCreateRequest:
    return PaymentCreateRequest.model_validate(_payload(**overrides))


def test_valid_amount_is_accepted_and_serialized_as_string() -> None:
    request = _create()
    assert request.amount == Decimal("10.50")
    assert request.model_dump(mode="json")["amount"] == "10.50"


def test_maximum_amount_is_accepted() -> None:
    request = _create(amount="999999999.99")
    assert request.amount == Decimal("999999999.99")


@pytest.mark.parametrize("amount", ["0", "0.00", "-1", "-0.01"])
def test_non_positive_amount_is_rejected(amount: str) -> None:
    with pytest.raises(ValidationError):
        _create(amount=amount)


@pytest.mark.parametrize("amount", ["1.234", "10.5000", "0.001"])
def test_more_than_two_decimal_places_is_rejected(amount: str) -> None:
    with pytest.raises(ValidationError):
        _create(amount=amount)


def test_amount_above_bound_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _create(amount="1000000000.00")


def test_non_string_amount_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _create(amount=10.5)


@pytest.mark.parametrize("currency", ["GBP", "rub", "", "BTC"])
def test_unsupported_currency_is_rejected(currency: str) -> None:
    with pytest.raises(ValidationError):
        _create(currency=currency)


@pytest.mark.parametrize("url", ["not-a-url", "ftp://host/file", "mailto:a@b.test", ""])
def test_invalid_webhook_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        _create(webhook_url=url)


def test_omitted_optional_fields_receive_defaults() -> None:
    request = PaymentCreateRequest.model_validate(
        {"amount": "1.00", "currency": "USD", "webhook_url": "https://receiver.test/hook"}
    )
    assert request.description == ""
    assert request.metadata == {}


def test_payment_view_preserves_amount_and_utc_timestamps() -> None:
    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    view = PaymentView(
        payment_id=uuid4(),
        amount=Decimal("99.90"),
        currency="EUR",
        description="",
        metadata={},
        status="pending",
        webhook_url="http://receiver.test/webhook",
        created_at=created,
        processed_at=None,
    )
    dumped = view.model_dump(mode="json")
    assert dumped["amount"] == "99.90"
    assert dumped["processed_at"] is None
    assert datetime.fromisoformat(dumped["created_at"]) == created
