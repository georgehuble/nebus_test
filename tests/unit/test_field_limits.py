"""Boundary tests for description code points and metadata canonical JSON size."""

from __future__ import annotations

from typing import Any

import pytest
from app.domain.dto import DESCRIPTION_MAX_CODE_POINTS, METADATA_MAX_BYTES, PaymentCreateRequest, canonical_json_bytes
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _metadata_of_size(target: int) -> dict[str, str]:
    """Build a one-key metadata object whose canonical JSON is exactly ``target`` bytes."""
    payload = {"k": "x" * (target - 8)}
    assert len(canonical_json_bytes(payload)) == target
    return payload


def _request(**overrides: Any) -> PaymentCreateRequest:
    body: dict[str, Any] = {
        "amount": "1.00",
        "currency": "RUB",
        "webhook_url": "http://receiver.test/webhook",
    }
    body.update(overrides)
    return PaymentCreateRequest(**body)


def test_description_at_limit_is_accepted() -> None:
    request = _request(description="a" * DESCRIPTION_MAX_CODE_POINTS)
    assert len(request.description) == DESCRIPTION_MAX_CODE_POINTS


def test_description_above_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _request(description="a" * (DESCRIPTION_MAX_CODE_POINTS + 1))


def test_description_limit_counts_code_points_not_bytes() -> None:
    request = _request(description="\U0001f600" * DESCRIPTION_MAX_CODE_POINTS)
    assert len(request.description) == DESCRIPTION_MAX_CODE_POINTS
    with pytest.raises(ValidationError):
        _request(description="\U0001f600" * (DESCRIPTION_MAX_CODE_POINTS + 1))


def test_metadata_exactly_at_limit_is_accepted() -> None:
    request = _request(metadata=_metadata_of_size(METADATA_MAX_BYTES))
    assert len(canonical_json_bytes(request.metadata)) == METADATA_MAX_BYTES


def test_metadata_above_limit_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _request(metadata=_metadata_of_size(METADATA_MAX_BYTES + 1))


def test_metadata_limit_accounts_for_multibyte_unicode() -> None:
    # Each 'é' is two UTF-8 bytes, so fewer code points reach the byte limit.
    padding = "é" * ((METADATA_MAX_BYTES - 8) // 2)
    metadata = {"k": padding}
    size = len(canonical_json_bytes(metadata))
    assert size <= METADATA_MAX_BYTES
    assert len(padding) < METADATA_MAX_BYTES
