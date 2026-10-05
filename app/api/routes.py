"""Versioned HTTP routes: create/read payments and readiness."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import ContainerDep
from app.api.schemas import HealthResponse, PaymentAcceptedResponse, PaymentCreateRequest, PaymentView
from app.domain.dto import Currency

router = APIRouter(prefix="/api/v1")


@router.post(
    "/payments",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PaymentAcceptedResponse,
    summary="Create a payment",
    responses={
        401: {"description": "Missing or invalid API key"},
        409: {"description": "Idempotency key reused with a different body"},
        422: {"description": "Invalid input"},
    },
)
async def create_payment(
    container: ContainerDep,
    body: PaymentCreateRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> PaymentAcceptedResponse:
    """Accept a payment and durably persist it before responding ``202``."""
    accepted = await container.service.create(idempotency_key=idempotency_key, request=body)
    return PaymentAcceptedResponse(
        payment_id=accepted.payment_id,
        status=accepted.status.value,
        created_at=accepted.created_at,
    )


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentView,
    summary="Read a payment",
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Unknown payment identifier"},
        422: {"description": "Malformed payment identifier"},
    },
)
async def read_payment(payment_id: UUID, container: ContainerDep) -> PaymentView:
    """Return the full representation of a known payment."""
    record = await container.service.get(payment_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment not found")
    return PaymentView(
        payment_id=record.id,
        amount=record.amount,
        currency=cast(Currency, record.currency),
        description=record.description,
        metadata=record.metadata,
        status=record.status.value,
        webhook_url=record.webhook_url,
        created_at=record.created_at,
        processed_at=record.processed_at,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Readiness of the API process",
    responses={401: {"description": "Missing or invalid API key"}, 503: {"description": "Not ready"}},
)
async def health(container: ContainerDep) -> HealthResponse:
    """Report readiness of the running ASGI process and its database."""
    try:
        async with container.database.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable") from exc
    return HealthResponse(status="ok", database="ok")


@router.get("/openapi.json", include_in_schema=False)
async def openapi_document(request: Request) -> JSONResponse:
    """Serve the generated OpenAPI contract behind the same API key."""
    application = cast(FastAPI, request.app)
    return JSONResponse(application.openapi())
