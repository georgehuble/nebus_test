"""Canonicalization and fingerprint stability for idempotency."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.application.fingerprint import canonical_body, request_fingerprint
from app.domain.dto import PaymentCreateRequest, normalize_amount

pytestmark = pytest.mark.unit


def _request(**overrides: Any) -> PaymentCreateRequest:
    body: dict[str, Any] = {
        "amount": "10.50",
        "currency": "RUB",
        "description": "order",
        "metadata": {"b": 2, "a": 1},
        "webhook_url": "http://receiver.test/webhook",
    }
    body.update(overrides)
    return PaymentCreateRequest.model_validate(body)


def test_normalize_amount_fills_two_decimal_places() -> None:
    assert normalize_amount(Decimal("10.5")) == Decimal("10.50")
    assert normalize_amount(Decimal("10")) == Decimal("10.00")


def test_equivalent_decimal_spellings_share_a_fingerprint() -> None:
    assert request_fingerprint(_request(amount="10.5")) == request_fingerprint(_request(amount="10.50"))


def test_metadata_key_order_does_not_change_fingerprint() -> None:
    first = _request(metadata={"a": 1, "b": 2, "c": {"y": 1, "x": 2}})
    second = _request(metadata={"c": {"x": 2, "y": 1}, "b": 2, "a": 1})
    assert request_fingerprint(first) == request_fingerprint(second)


def test_documented_defaults_make_omitted_fields_equivalent() -> None:
    explicit = _request(description="", metadata={})
    omitted = PaymentCreateRequest.model_validate(
        {"amount": "10.50", "currency": "RUB", "webhook_url": "http://receiver.test/webhook"}
    )
    assert request_fingerprint(explicit) == request_fingerprint(omitted)


def test_different_amount_changes_fingerprint() -> None:
    assert request_fingerprint(_request(amount="10.50")) != request_fingerprint(_request(amount="10.51"))


def test_different_webhook_url_changes_fingerprint() -> None:
    assert request_fingerprint(_request()) != request_fingerprint(_request(webhook_url="http://other.test/hook"))


def test_canonical_body_is_compact_and_sorted() -> None:
    body = canonical_body(_request())
    assert list(body) == ["amount", "currency", "description", "metadata", "webhook_url"]
    assert body["amount"] == "10.50"
