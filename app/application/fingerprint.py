"""Canonical request fingerprint used for idempotent payment creation."""

from __future__ import annotations

import hashlib
from typing import Any

from app.domain.dto import PaymentCreateRequest, canonical_json_bytes, normalize_amount


def canonical_body(request: PaymentCreateRequest) -> dict[str, Any]:
    """Build the canonicalized body compared for idempotency.

    Documented defaults are applied, JSON object keys are sorted and the decimal
    amount is normalized so equivalent decimal spellings compare equal.
    """
    return {
        "amount": format(normalize_amount(request.amount), "f"),
        "currency": request.currency,
        "description": request.description,
        "metadata": request.metadata,
        "webhook_url": str(request.webhook_url),
    }


def request_fingerprint(request: PaymentCreateRequest) -> str:
    """Return the SHA-256 hex digest of the canonicalized request body."""
    return hashlib.sha256(canonical_json_bytes(canonical_body(request))).hexdigest()
