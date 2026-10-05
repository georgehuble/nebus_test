"""API schemas. Request/response payment models live in the domain DTO module."""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.dto import PaymentAcceptedResponse, PaymentCreateRequest, PaymentView

__all__ = [
    "HealthResponse",
    "PaymentAcceptedResponse",
    "PaymentCreateRequest",
    "PaymentView",
]


class HealthResponse(BaseModel):
    """Key-protected readiness payload without any payment data."""

    status: str
    database: str
