"""Consumer application: handler, relay, retry dispatcher and readiness endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Security, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.security import require_api_key
from app.consumer.runner import ConsumerRunner
from app.container import ConsumerContainer, build_consumer_container
from app.logging_config import configure_logging, get_logger
from app.settings import Settings, get_settings


class ConsumerRuntime:
    """Mutable holder for the running consumer tasks, used by readiness checks."""

    def __init__(self) -> None:
        self.container: ConsumerContainer | None = None
        self.runner: ConsumerRunner | None = None
        self.relay_task: asyncio.Task[None] | None = None
        self.retry_task: asyncio.Task[None] | None = None
        self.stop_event: asyncio.Event | None = None


def create_consumer_app(settings: Settings | None = None) -> FastAPI:
    """Build the consumer application; configuration comes from the environment."""
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, service=f"{resolved.service_name}-consumer")
    log = get_logger("app.consumer")
    runtime = ConsumerRuntime()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        container = build_consumer_container(resolved)
        runtime.container = container
        stop_event = asyncio.Event()
        runtime.stop_event = stop_event
        await container.broker.start()
        runner = ConsumerRunner(
            broker=container.broker,
            processor=container.processor,
            stop_event=stop_event,
            settings=resolved,
        )
        runtime.runner = runner
        await runner.start()
        runtime.relay_task = asyncio.create_task(container.relay.run(stop_event), name="outbox-relay")
        runtime.retry_task = asyncio.create_task(container.retry_dispatcher.run(stop_event), name="retry-dispatcher")
        log.info("consumer started")
        try:
            yield
        finally:
            stop_event.set()
            await runner.stop()
            for task in (runtime.relay_task, runtime.retry_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (runtime.relay_task, runtime.retry_task) if task is not None),
                return_exceptions=True,
            )
            await container.webhook_sender.aclose()
            await container.broker.close()
            await container.database.dispose()
            log.info("consumer stopped")

    application = FastAPI(
        title="Async Payment Processing Consumer",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Security(require_api_key)],
    )
    application.state.runtime = runtime
    application.state.api_key_value = resolved.api_key.get_secret_value()

    @application.get("/api/v1/health", include_in_schema=False)
    async def health(request: Request) -> JSONResponse:
        checks, ready = await _readiness(request)
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=checks,
        )

    return application


async def _readiness(request: Request) -> tuple[dict[str, str], bool]:
    runtime: ConsumerRuntime = request.app.state.runtime
    container = runtime.container
    checks = {
        "status": "ok",
        "database": "ok",
        "broker": "ok",
        "handler": "ok",
        "relay": "ok",
        "retry": "ok",
    }
    ready = True
    if container is None:
        return {key: "unavailable" for key in checks}, False
    try:
        async with container.database.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - readiness must never raise
        checks["database"] = "error"
        ready = False
    if not container.broker.is_ready():
        checks["broker"] = "error"
        ready = False
    if runtime.runner is None or not runtime.runner.is_running():
        checks["handler"] = "error"
        ready = False
    if runtime.relay_task is None or runtime.relay_task.done():
        checks["relay"] = "error"
        ready = False
    if runtime.retry_task is None or runtime.retry_task.done():
        checks["retry"] = "error"
        ready = False
    if not ready:
        checks["status"] = "error"
    return checks, ready
