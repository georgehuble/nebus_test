"""Request/response data transfer objects for the payment HTTP contract."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_serializer, field_validator

METADATA_MAX_BYTES = 16384
DESCRIPTION_MAX_CODE_POINTS = 500
AMOUNT_MAX = Decimal("999999999.99")

Currency = Literal["RUB", "USD", "EUR"]


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 JSON encoding used for size and fingerprint checks."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def normalize_amount(amount: Decimal) -> Decimal:
    """Normalize a decimal amount to exactly two fractional digits without rounding."""
    return amount.quantize(Decimal("0.01"))


class PaymentCreateRequest(BaseModel):
    """Validated body of ``POST /api/v1/payments``."""

    model_config = ConfigDict(extra="ignore")

    amount: Decimal
    currency: Currency
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    webhook_url: HttpUrl

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_must_be_string(cls, value: Any) -> Any:
        if not isinstance(value, str):
            msg = "amount must be a JSON string representing a decimal number"
            raise ValueError(msg)
        return value

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            msg = "amount must be a finite decimal number"
            raise ValueError(msg)
        if value <= 0:
            msg = "amount must be greater than zero"
            raise ValueError(msg)
        if value > AMOUNT_MAX:
            msg = f"amount must not exceed {AMOUNT_MAX}"
            raise ValueError(msg)
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            msg = "amount must not have more than two decimal places"
            raise ValueError(msg)
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        if len(value) > DESCRIPTION_MAX_CODE_POINTS:
            msg = f"description must not exceed {DESCRIPTION_MAX_CODE_POINTS} code points"
            raise ValueError(msg)
        return value

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        size = len(canonical_json_bytes(value))
        if size > METADATA_MAX_BYTES:
            msg = f"metadata must not exceed {METADATA_MAX_BYTES} bytes of canonical JSON"
            raise ValueError(msg)
        return value

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format(value, "f")


class PaymentAcceptedResponse(BaseModel):
    """Body of a ``202 Accepted`` creation response."""

    payment_id: UUID
    status: PaymentStatusLiteral = "pending"
    created_at: datetime


PaymentStatusLiteral = Literal["pending", "succeeded", "failed"]


class PaymentView(BaseModel):
    """Full payment representation returned by the read endpoint."""

    payment_id: UUID
    amount: Decimal
    currency: Currency
    description: str
    metadata: dict[str, Any]
    status: PaymentStatusLiteral
    webhook_url: str
    created_at: datetime
    processed_at: datetime | None

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format(value, "f")
