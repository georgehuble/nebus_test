"""FastAPI application factory for the HTTP API process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Security, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.api.security import require_api_key
from app.container import ApiContainer, build_api_container
from app.errors import IdempotencyConflict
from app.logging_config import configure_logging, get_logger
from app.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application. Configuration comes from the environment by default."""
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, service=resolved.service_name)
    log = get_logger("app.api")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        container: ApiContainer = build_api_container(resolved)
        application.state.container = container
        log.info("api started")
        try:
            yield
        finally:
            await container.database.dispose()
            log.info("api stopped")

    application = FastAPI(
        title="Async Payment Processing",
        version=__version__,
        description="Idempotent payment intake with asynchronous processing and webhook delivery.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Security(require_api_key)],
    )
    application.state.api_key_value = resolved.api_key.get_secret_value()
    application.include_router(router)
    application.add_exception_handler(IdempotencyConflict, _idempotency_conflict_handler)
    application.add_exception_handler(RequestValidationError, _validation_error_handler)
    return application


async def _idempotency_conflict_handler(_: Request, __: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "idempotency key reused with a different request body"},
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(errors)},
    )
